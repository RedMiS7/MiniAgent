# P3 验收检查（2026-09-17）

## 结论与范围

本次按 ROADMAP.md 检查 P3 的整体数据契约，只检查实现、运行离线测试和无副作用构造探针。
P3.2 事件契约可以通过，P3 整体暂不通过。不因现有测试全部通过而将尚未覆盖的数据边界标记完成。
本次未修改功能代码，也未调整路线图完成状态。

## 已满足的部分

- 已有提供商无关的 Message、LLMRequest、LLMResponse、ToolCall、ContinuationState。
- ToolResult.to_message() 提供结果回传入口，LLMRequest 已检查工具调用 ID 配对。
- AgentEvent、LLMEvent、ToolEvent 的职责与运行关联已定义，当时由 RunEvent 提供运行和模型调用上下文（后续已移除，当前约定见 p3-2-event-contract.md）。
- 事件测试覆盖模拟失败结果回传、调用 ID 和续接状态保持；工具测试覆盖执行事件终态。
- python -m unittest discover -s tests -q：72 项测试通过，未调用真实模型服务。

## 未满足的部分与证据

### 核心数据校验不足

以下构造探针当前均被接受：

- ToolCall(123, "echo", "{}")：调用 ID 不是字符串。
- LLMResponse(Message("user", "hello"), "stop")：模型响应携带 user 消息。
- LLMResponse(Message("assistant", "hello"), "unknown")：结束原因不合法。
- ToolContent("text", 123)：文本内容不是字符串。
- ToolResult(True, error_code="execution_error")：成功状态与错误字段冲突。
- ToolResult(True, (ToolContent("json", object()),))：内容不能序列化；直到 to_message("call-1") 才抛出 TypeError。

这些缺口涉及 P3 的“非法数据被明确拒绝”，应通过核心类型边界校验与失败测试补齐。
ToolCall.arguments 保存原始 JSON 字符串是已有明确约定，不应因此提前拒绝无效或不完整参数；参数解析仍由工具执行器负责。

### Agent 最终结果和组件交接约定尚不完整

当前 miniagent/agent/ 只有事件契约，尚未明确 Agent 向调用方返回最终答案、结束原因和对话状态的具体结构。
完成事件表示运行状态，不能自动替代返回结果契约。可以明确复用现有类型，也可以按实际需要增加最小结果类型，不要求为统一而增加包装。

test_simulated_failure_round_trip 手动构造模型响应、工具结果和事件，验证了部分字段的关联，
但没有通过明确的 Agent 最终结果契约完成交付；各组件实际交接的组合验证仍需补充。

## 建议推进顺序与阶段边界

1. 先完成 P3.1 的核心类型边界校验，补充非法数据回归测试。
2. 明确 Loop、Model、Tools、Runtime 的输入输出、续接状态保留和异常交接约定。
3. 用假模型、模拟工具和已明确的 Agent 结果契约补齐一次完整往返的离线集成测试，再复验 P3。

不将自动执行循环、运行时步数控制、取消调度或 Session 实现作为 P3 验收前置条件：
这些分别属于 P4/P5/P6。P3 需要证明数据契约足以支撑这些后续实现。
