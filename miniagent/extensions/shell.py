import asyncio
import os
import signal
import subprocess

from miniagent.models import ToolDefinition
from miniagent.tools import ToolContent, ToolError, ToolResult


async def _stop(process):
    if os.name == "nt":
        if process.returncode is None:
            killer = await asyncio.create_subprocess_exec(
                "taskkill", "/PID", str(process.pid), "/T", "/F",
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            await killer.wait()
            if process.returncode is None:
                process.kill()
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    await process.wait()


async def _capture(reader, limit):
    data = bytearray()
    truncated = False
    while chunk := await reader.read(4096):
        remaining = max(0, limit - len(data))
        data.extend(chunk[:remaining])
        truncated |= len(chunk) > remaining
    return data.decode("utf-8", errors="replace"), truncated


class CommandTool:
    definition = ToolDefinition("run_command", "Run an executable with arguments; no implicit shell.", {
        "type": "object",
        "properties": {
            "argv": {"type": "array", "minItems": 1, "maxItems": 128,
                     "items": {"type": "string", "minLength": 1}},
            "cwd": {"type": "string", "minLength": 1},
        },
        "required": ["argv"], "additionalProperties": False,
    })

    async def execute(self, arguments, context):
        if not context.allow_commands:
            raise ToolError("access_denied", "Command execution is disabled.")
        cwd = context.path(arguments.get("cwd", "."))
        kwargs = ({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt"
                  else {"start_new_session": True})
        process = await asyncio.create_subprocess_exec(
            *arguments["argv"], cwd=cwd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, **kwargs,
        )
        readers = [
            asyncio.create_task(_capture(process.stdout, context.max_output_chars)),
            asyncio.create_task(_capture(process.stderr, context.max_output_chars)),
        ]
        async def collect():
            output = await asyncio.gather(*readers)
            await process.wait()
            return output
        try:
            output = await asyncio.wait_for(collect(), timeout=context.command_timeout)
        except asyncio.TimeoutError:
            await _stop(process)
            raise ToolError("timeout", "Command exceeded its timeout.") from None
        except asyncio.CancelledError:
            await _stop(process)
            raise
        finally:
            for task in readers:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*readers, return_exceptions=True)
        data = {
            "exit_code": process.returncode,
            "stdout": output[0][0], "stderr": output[1][0],
            "truncated": output[0][1] or output[1][1],
        }
        return ToolResult(
            process.returncode == 0, (ToolContent("json", data),),
            error_code="command_failed" if process.returncode else None,
            error_message="Command exited with a nonzero status." if process.returncode else None,
        )


def register(registry):
    registry.register(CommandTool())
