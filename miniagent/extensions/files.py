import difflib
import hashlib
import os

from miniagent.models import ToolDefinition
from miniagent.tools import ToolContent, ToolError, ToolResult


def _read(path, context):
    with path.open("rb") as source:
        data = source.read(context.max_file_bytes + 1)
    if len(data) > context.max_file_bytes:
        raise ToolError("file_too_large", "File exceeds the size limit.")
    return data, data.decode("utf-8")


def _result(data):
    return ToolResult(True, (ToolContent("json", data),))


class FileTool:
    definition: ToolDefinition

    async def execute(self, arguments, context):
        path = context.path(arguments["path"])
        name = self.definition.name
        if name == "list_files":
            entries = []
            used = 0
            truncated = False
            with os.scandir(path) as items:
                for item in items:
                    used += len(item.name)
                    if len(entries) >= 200 or used > context.max_output_chars:
                        truncated = True
                        break
                    entries.append(item.name)
            return _result({"entries": sorted(entries), "truncated": truncated})

        if name == "read_file":
            data, text = _read(path, context)
            start = arguments.get("start_line", 1)
            count = arguments.get("line_count", 200)
            lines = text.splitlines(keepends=True)
            selected = "".join(lines[start - 1:start - 1 + count])
            return _result({
                "text": selected[:context.max_output_chars], "start_line": start,
                "total_lines": len(lines), "sha256": hashlib.sha256(data).hexdigest(),
                "truncated": len(selected) > context.max_output_chars or start - 1 + count < len(lines),
            })

        if name == "write_file":
            text = arguments["content"]
            data = text.encode("utf-8")
            if len(data) > context.max_file_bytes:
                raise ToolError("file_too_large", "Content exceeds the size limit.")
            try:
                with path.open("xb") as target:
                    target.write(data)
            except FileExistsError:
                raise ToolError("already_exists", "Use edit_file to modify an existing file.") from None
            before = ""
        else:
            before_data, before = _read(path, context)
            if hashlib.sha256(before_data).hexdigest() != arguments["expected_sha256"]:
                raise ToolError("conflict", "File changed since it was read.")
            if before.count(arguments["old_text"]) != 1:
                raise ToolError("conflict", "old_text must match exactly once.")
            text = before.replace(arguments["old_text"], arguments["new_text"], 1)
            data = text.encode("utf-8")
            if len(data) > context.max_file_bytes:
                raise ToolError("file_too_large", "Edited content exceeds the size limit.")
            # Re-check through the same opened file before writing.
            with path.open("r+b") as target:
                current = target.read(context.max_file_bytes + 1)
                if current != before_data:
                    raise ToolError("conflict", "File changed before writing.")
                target.seek(0)
                target.write(data)
                target.truncate()
        diff = "".join(difflib.unified_diff(
            before.splitlines(keepends=True), text.splitlines(keepends=True),
            fromfile=arguments["path"], tofile=arguments["path"],
        ))
        return _result({
            "path": arguments["path"], "sha256": hashlib.sha256(data).hexdigest(),
            "diff": diff[:context.max_output_chars],
            "truncated": len(diff) > context.max_output_chars,
        })


class ListFilesTool(FileTool):
    definition = ToolDefinition(
        name="list_files",
        description="List a workspace directory.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Workspace-relative directory path to list; use . for the root.", "minLength": 1},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    )


class ReadFileTool(FileTool):
    definition = ToolDefinition(
        name="read_file",
        description="Read UTF-8 text and get its SHA-256.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Workspace-relative path to the UTF-8 file to read.", "minLength": 1},
                "start_line": {"type": "integer", "description": "First line to read, numbered from 1. Defaults to 1.", "minimum": 1},
                "line_count": {"type": "integer", "description": "Maximum number of lines to read. Defaults to 200.", "minimum": 1, "maximum": 1000},
            },
            "required": ["path"], "additionalProperties": False,
        },
    )


class WriteFileTool(FileTool):
    definition = ToolDefinition(
        name="write_file",
        description="Create a UTF-8 file; never overwrite.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Workspace-relative path to the new file to write. Parent directory must exist.", "minLength": 1},
                "content": {"type": "string", "description": "Full text content to write as UTF-8."},
            },
            "required": ["path", "content"], "additionalProperties": False,
        },
    )


class EditFileTool(FileTool):
    definition = ToolDefinition(
        name="edit_file",
        description="Replace one exact match if the file hash still matches.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Workspace-relative path to the existing UTF-8 file to edit.", "minLength": 1},
                "old_text": {"type": "string", "description": "Exact text to replace; must occur exactly once in the file.", "minLength": 1},
                "new_text": {"type": "string", "description": "Replacement text; use an empty string to remove old_text."},
                "expected_sha256": {"type": "string", "description": "SHA-256 returned by read_file, used to detect changes since reading.", "pattern": "^[0-9a-f]{64}$"},
            },
            "required": ["path", "old_text", "new_text", "expected_sha256"],
            "additionalProperties": False,
        },
    )


def register(registry):
    registry.register(ListFilesTool())
    registry.register(ReadFileTool())
    registry.register(WriteFileTool())
    registry.register(EditFileTool())
