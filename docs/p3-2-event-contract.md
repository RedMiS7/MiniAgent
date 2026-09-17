# P3.2：Agent 与组件事件契约

## 问题与方案

已有 LLMEvent 描述模型生成过程，ToolEvent 描述工具执行状态，但缺少 Agent 运行事件和运行级关联。
本次新增 miniagent/agent/，集中放置 Agent 层代码；事件契约位于 agent/events.py。
模型事件仍归模型层，工具事件仍归工具层，避免上层运行概念侵入可独立使用的组件。

RunEvent 为现有事件添加 run_id 和 model_call_id，不复制事件载荷，也不改变模型和工具调用接口。
选择包装而不是逐个修改适配器，是因为 Run 的生命周期属于上层，模型适配器不应生成或管理运行 ID。

## 类型与字段

| 类型 | 字段 | 职责 |
|---|---|---|
| AgentEvent | type、reason | 单次运行开始、进度、成功、失败和取消 |
| RunEvent | run_id、event、model_call_id | 将 Agent、LLM、Tool 事件关联到运行及模型调用 |
| LLMEvent | 沿用 TextDelta、ToolCallStarted、ToolArgumentsDelta、ResponseCompleted | 模型增量与完整响应 |
| ToolEvent | 沿用 type、call_id、tool_name、elapsed_seconds、error_code | 工具实际执行状态 |

AgentEvent 的 started / progress 不携带 reason；progress 表示完成一次模型调用或工具执行。
completed 的 reason 为 stop，cancelled 的 reason 为 cancelled。
failed 必须携带非空原因代码（如 model_error、length、refusal、content_filter、step_limit），不能使用 stop 或 cancelled。
原因字段用于代码，不应放入原始异常或敏感内容。具体结束策略由 P4/P5 实现。

RunEvent 中的 AgentEvent 只属于 Run，model_call_id 必须为空。
LLMEvent 和 ToolEvent 必须携带 model_call_id；工具事件关联的是产生该工具调用的模型调用。
run_id 与 model_call_id 由未来的上层运行代码生成，不依赖厂商请求 ID。
同一 Run 内模型调用 ID 应唯一；不同 Run 可以复用局部模型调用 ID。

ToolCallStarted.index 与 ToolArgumentsDelta.index 在同一模型调用内关联。
ToolCallStarted.id、最终 ToolCall.id、ToolEvent.call_id 和工具结果消息的 tool_call_id 必须一致。
RunEvent 只检查上下文字段合法性，无法单凭单个事件验证这些跨对象关系。

## 顺序与异常约定

- Agent：started → 零个或多个 progress / 组件事件 → 一个 completed、failed 或 cancelled；终止后不得继续发出该 Run 的事件。
- 模型：正常流由增量事件组成，并以一个 ResponseCompleted 结束；没有增量的正常响应也允许直接完成。
- 工具参数增量需关联此前的 ToolCallStarted；多个工具调用可以交错输出，通过 index 区分。
- 模型生成工具调用不代表工具已执行；完整响应中的调用才可交给执行器校验。
- 模型调用失败继续通过 LLMError 抛出，取消继续传播取消异常；不伪造 ResponseCompleted。
- 工具：started → completed / failed / cancelled，终态事件只发出一次。失败事件的 error_code 可为空，以兼容现有 ToolResult。
- 工具失败通过 ToolResult.to_message(call.id) 进入下一轮模型请求，ToolEvent 仅用于观察执行状态。
- 模型响应完成不等于 Agent 成功；工具失败后 Agent 仍可继续。模型异常、截断等非正常终止不能作为 Agent 成功。
- 工具事件回调失败继续沿用现有隔离行为，不能因此重复执行工具。

本阶段定义顺序约定并测试模拟序列，不新增事件总线、序列状态机或 Runtime。
事件数据类检查单个对象；真实运行中的唯一 ID、先后顺序和恰好一次终态由后续 Loop/Runtime 保证。

## 用法

```python
from miniagent.agent import AgentEvent, RunEvent
from miniagent.models import TextDelta
from miniagent.tools import ToolEvent

started = RunEvent("run-1", AgentEvent("started"))
delta = RunEvent("run-1", TextDelta("正在读取"), "model-1")
tool = RunEvent(
    "run-1",
    ToolEvent("failed", "call-1", "read_file", 0.1, "io_error"),
    "model-1",
)
finished = RunEvent("run-1", AgentEvent("completed", "stop"))
```

RunEvent 的 repr 隐藏载荷，避免默认打印运行事件时暴露模型文本。
这不等于脱敏或访问控制：event 仍可读取，底层模型事件也仍包含原始增量和响应，调用方不得默认完整记录。

## 验证与限制

先添加契约测试，确认因缺少 miniagent.agent 而失败，再补充实现。
使用现有 Python 执行：

```powershell
python -m unittest discover -s tests -v
```

72 项离线测试全部通过，包含本次新增的 8 项测试。
覆盖非法事件字段、终止原因与状态组合、不同运行和模型调用的区分、
模拟工具失败结果回传时的调用 ID 与续接状态，以及真实 ToolExecutor 的成功、失败、异常和取消事件顺序。
已有模型适配器和工具测试无回归，未验证真实模型服务。

本次完成 P3 的事件职责、字段和关联契约，不代表整个 P3 完成。
没有实现 Agent 自动循环、事件汇集、取消控制或资源清理；这些分别属于 P4/P5。
