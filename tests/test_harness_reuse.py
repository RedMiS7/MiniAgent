import json
from pathlib import Path
import tempfile
import unittest

import httpx
from openai import AsyncOpenAI

from examples import echo_extension
from examples.harness_reuse import CALLS, TASKS, ScriptedModel, run_tasks
from miniagent.agent import AgentHarness
from miniagent.models import Message
from miniagent.models.deepseek_adapter import DeepSeekAdapter
from miniagent.models.openai_adapter import OpenAIAdapter
from miniagent.tools import ToolContext, ToolExecutor, ToolRegistry
from test_models import chunk, response, sse


class HarnessReuseTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name)
        (self.workspace / "note.txt").write_text("fixture document", encoding="utf-8")

    async def verify_adapter(self, provider):
        requests, responses, events = [], [], []
        script = [CALLS[0], CALLS[1], "File summary", CALLS[2], "Hello Harness"]

        def handler(request):
            body = json.loads(request.content)
            requests.append(body)
            item = script[len(requests) - 1]
            self.assertTrue(body["stream"])
            if provider == "openai":
                self.assertEqual(request.url.path, "/v1/responses")
                if isinstance(item, str):
                    result = sse([{"type":"response.completed", "response":response(text=item)}])
                else:
                    output = [{"type":"reasoning", "id":"reason", "summary":[], "encrypted_content":"opaque"},
                              {"type":"function_call", "id":"fc_" + item.id, "call_id":item.id,
                               "name":item.name, "arguments":item.arguments, "status":"completed"}]
                    result = sse([{"type":"response.completed", "response":response(output=output)}])
            else:
                self.assertEqual(request.url.path, "/v1/chat/completions")
                if isinstance(item, str):
                    result = sse([chunk({"content":item, "reasoning_content":"opaque"}, "stop")])
                else:
                    result = sse([chunk({"reasoning_content":"opaque", "tool_calls":[
                        {"index":0, "id":item.id, "type":"function", "function":{
                            "name":item.name, "arguments":item.arguments}}]}, "tool_calls")])
            responses.append(result)
            return result

        client = AsyncOpenAI(api_key="test-only", base_url="https://example.invalid/v1", max_retries=0,
                             http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        model = (OpenAIAdapter if provider == "openai" else DeepSeekAdapter)(client, "test-model")
        try:
            results = await run_tasks(model, self.workspace, on_event=lambda run, event: events.append((run, event)))
            self.assertFalse(client.is_closed())  # borrowed model survives Harness exit
            self.assertEqual([r.status for r in results], ["succeeded", "succeeded"])
            self.assertEqual([r.messages[-1].content for r in results], ["File summary", "Hello Harness"])
            self.assertEqual(len(requests), 5)
            self.assertTrue(all(r.is_closed for r in responses))
            completed = [run for run, event in events if event.type == "agent_completed"]
            self.assertEqual(len(completed), 2)
            self.assertIsNot(completed[0], completed[1])
            self.assertEqual([e.tool_name for _, e in events if e.type == "tool_completed"],
                             ["list_files", "read_file", "echo"])
            for body in requests:
                names = {tool.get("name", tool.get("function", {}).get("name")) for tool in body["tools"]}
                self.assertEqual(names, {"list_files", "read_file", "echo"})
            if provider == "openai":
                self.assertEqual(requests[3]["input"], [{"role":"user", "content":TASKS[1]}])
                history = requests[2]["input"]
                outputs = [i for i in history if i.get("type") == "function_call_output"]
                self.assertEqual([i["call_id"] for i in outputs], ["list", "read"])
                self.assertEqual(json.loads(outputs[1]["output"])["content"][0]["value"]["text"], "fixture document")
                self.assertEqual([i["encrypted_content"] for i in history if i.get("type") == "reasoning"], ["opaque", "opaque"])
                echo = next(i for i in requests[4]["input"] if i.get("type") == "function_call_output")
                self.assertEqual(echo["call_id"], "echo")
                output = json.loads(echo["output"])
            else:
                self.assertEqual(requests[3]["messages"], [{"role":"user", "content":TASKS[1]}])
                history = requests[2]["messages"]
                outputs = [i for i in history if i["role"] == "tool"]
                self.assertEqual([i["tool_call_id"] for i in outputs], ["list", "read"])
                self.assertEqual(json.loads(outputs[1]["content"])["content"][0]["value"]["text"], "fixture document")
                self.assertEqual([i["reasoning_content"] for i in history if i["role"] == "assistant"], ["opaque", "opaque"])
                echo = requests[4]["messages"][-1]
                self.assertEqual(echo["tool_call_id"], "echo")
                output = json.loads(echo["content"])
            self.assertEqual(output["content"][0]["value"], "Hello Harness")
        finally:
            await model.aclose()
        self.assertTrue(client.is_closed())

    async def test_openai_real_adapter_tools_and_independent_runs(self):
        await self.verify_adapter("openai")

    async def test_deepseek_real_adapter_tools_and_independent_runs(self):
        await self.verify_adapter("deepseek")

    async def test_echo_only_registry_works_without_core_changes(self):
        registry = ToolRegistry()
        echo_extension.register(registry)
        model = ScriptedModel()
        # This task only exposes echo; the same Harness implementation is reused.
        model.responses = iter((CALLS[2], "Hello Harness"))
        async with AgentHarness(model, ToolExecutor(registry, ToolContext(self.workspace))) as harness:
            async with harness.run([Message(role="user", content=TASKS[1])]) as run:
                _ = [e async for e in run.events()]
        self.assertEqual(run.result.status, "succeeded")
        self.assertEqual([d.name for d in registry.definitions()], ["echo"])
        self.assertEqual(json.loads(run.result.messages[-2].content)["content"][0]["value"], "Hello Harness")

    async def test_offline_example_runs_actual_file_tools(self):
        results = await run_tasks(ScriptedModel(), self.workspace)
        self.assertEqual([r.status for r in results], ["succeeded", "succeeded"])
        payload = json.loads(next(m.content for m in results[0].messages if m.tool_call_id == "read"))
        self.assertEqual(payload["content"][0]["value"]["text"], "fixture document")
        self.assertEqual(results[1].messages[0].content, TASKS[1])
