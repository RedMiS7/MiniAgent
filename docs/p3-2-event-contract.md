# P3.2：Agent 与组件事件契约

## 设计

每种事件使用独立 Pydantic 类型，继承共享 ContractModel，携带固定的 type: Literal 标识。
AgentEvent、LLMEvent、ToolEvent 都是按 type 区分的联合类型，用于注解及解析，不直接构造。
调用方使用具体类构造事件，通过 isinstance 或 type 判断事件种类。

Agent 层代码位于 miniagent/agent/，模型事件位于 models/events.py，工具事件位于 tools/events.py。
底层不携带 Agent 运行 ID，运行关联由上层所属回调或事件流负责。已删除的 RunEvent 不恢复。

## 事件与字段

| 联合类型 | 具体类型 | type 标识 | 载荷 |
|---|---|---|---|
| LLMEvent | TextDelta | llm_text_delta | text |
| LLMEvent | ToolCallStarted | llm_tool_call_started | index、id、name |
| LLMEvent | ToolArgumentsDelta | llm_tool_arguments_delta | index、delta |
| LLMEvent | ResponseCompleted | llm_response_completed | response: LLMResponse |
| ToolEvent | ToolStarted | tool_started | call_id、tool_name |
| ToolEvent | ToolCompleted | tool_completed | call_id、tool_name、elapsed_seconds |
| ToolEvent | ToolFailed | tool_failed | call_id、tool_name、elapsed_seconds、error_code |
| ToolEvent | ToolCancelled | tool_cancelled | call_id、tool_name、elapsed_seconds |
| AgentEvent | AgentStarted | agent_started | 无 |
| AgentEvent | AgentProgress | agent_progress | step、phase |
| AgentEvent | AgentCompleted | agent_completed | reason 固定为 stop；messages 为完整成功历史 |
| AgentEvent | AgentFailed | agent_failed | reason |
| AgentEvent | AgentCancelled | agent_cancelled | reason 固定为 cancelled |

type 有默认值，构造时无需填写；不允许填入其他事件标签。
AgentProgress 的 step 是从 1 开始的模型轮次，phase 为 model（调用模型前）、tools（执行本轮工具前）或 completed（本轮工具结果已追加）。为兼容已有构造，默认 step=1、phase=completed。AgentFailed.reason 必须非空白且不能是 stop 或 cancelled，
应使用 model_error、length、step_limit 等原因代码，不放原始异常文本。
ToolStarted 不带耗时，工具终态事件耗时默认 0 且必须有限非负；只有 ToolFailed 允许 error_code，
它仍可为空以兼容没有诊断字段的失败 ToolResult。

字段禁止重新赋值，非法数据抛出 ValidationError。
模型、工具和 Agent 的具体执行策略不由事件类型实现。

## 关联与顺序

- 上层通过回调或事件流识别所属运行和模型调用；汇集多个来源时，必须保留这些上下文。
- ToolCallStarted.index 与 ToolArgumentsDelta.index 仅在同一模型调用内关联。
- ToolCallStarted.id、完整 ToolCall.id、ToolEvent 的 call_id 和工具结果消息的 tool_call_id 保持一致。
- AgentStarted → 零个或多个进度/组件事件 → 一个 AgentCompleted、AgentFailed 或 AgentCancelled。
- 模型正常流以一个 ResponseCompleted 结束；失败抛出 LLMError，取消传播取消异常，不伪造完成事件。
- ToolStarted → 一个 ToolCompleted、ToolFailed 或 ToolCancelled。取消事件发出后继续抛出取消异常。
- 模型生成工具调用不代表工具已执行。必须等待完整响应，再由执行器解析、校验参数并执行。
- 工具失败通过 ToolResult.to_message(call.id) 回传模型，事件仅通知观察者。
- 回调失败沿用现有隔离行为，不能因此重复执行工具。
- 模型完成不等于 Agent 完成，工具失败也不必终止整个任务。

P4 的 AgentLoop 已实现单次运行内的顺序和统一 LoopEvent 流；多 Run 管理与服务端关联由后续 Runtime/Harness 负责。

## 使用方式

```python
from pydantic import TypeAdapter
from miniagent.agent import AgentStarted, AgentCompleted
from miniagent.models import TextDelta
from miniagent.tools import ToolEvent, ToolFailed

started = AgentStarted()
delta = TextDelta(text="正在读取")
tool = ToolFailed(
    call_id="call-1", tool_name="read_file",
    elapsed_seconds=0.1, error_code="io_error",
)
finished = AgentCompleted()

restored = TypeAdapter(ToolEvent).validate_json(tool.model_dump_json())
assert isinstance(restored, ToolFailed)
assert restored.type == "tool_failed"
```

工具 CLI 的 stderr 展示 tool_started/tool_completed/tool_failed/tool_cancelled，
开始事件不显示耗时，终态显示耗时。stdout 的工具结果 JSON 保持不变。
事件可能包含敏感模型文本或完整响应，不应默认记录完整 repr/model_dump。

## 验证与限制

python -m unittest discover -s tests -q：80 项离线测试通过。
覆盖 13 种具体事件通过各自联合类型进行 JSON 往返，非法标签/多余字段/类型/结束原因被拒绝，
以及现有流式模型调用、工具成功/失败/异常/取消和观察者故障隔离。
工具 CLI 成功与失败状态展示通过单独检查。没有调用真实模型服务。
以上 80 项是 P3.2 时的历史验证结果。P4 已由实际 AgentLoop 发出事件，新增验证见 p4-agent-loop.md；P3 整体验收状态仍待单独复查。

## P4 交接补充

LoopEvent 是三类事件的判别联合。ResponseCompleted 交付本轮完整响应，AgentCompleted 确认整个任务成功；不额外引入 AgentResult。
完整 response.message（含 continuation）追加历史，工具结果通过 to_message(call.id) 追加，下一轮发送完整历史。
length/refusal/content_filter/step_limit 发出 AgentFailed 后正常结束流；模型异常发出 AgentFailed 后重抛，取消发出 AgentCancelled 后重抛。
消费者提前关闭流时不能保证收到终态，必须使用 aclosing 清理流。Loop 不关闭调用方拥有的模型客户端。
工具 execute 的每次调用 on_event 与执行器原观察者并存，分别隔离回调异常，不临时替换共享回调。

## CLI 多轮历史交付

AgentCompleted.messages 是 Message 元组，包含本次运行完整历史（含工具结果和 continuation），供调用方追加下一条用户消息。
字段默认空元组以兼容旧事件构造，实际 AgentLoop 成功结束始终填入完整历史；repr 不展开该字段，但显式序列化会包含历史，不应默认记录。
失败或取消不提供可直接续接的成功历史；CLI 结束会话并提示重启。详见 cli-multiturn-conversation.md。
