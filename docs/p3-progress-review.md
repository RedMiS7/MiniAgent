# P3 进度复查：类型与事件统一后

日期：2026-09-17。

## 范围与结论

本次对照 ROADMAP.md 复查核心类型、事件、组件交接约定及测试，仅新增检查记录，不修改功能代码或路线图勾选状态。
P3 整体尚未完成。已有测试通过证明现有边界可用，但不能替代尚未定义的 Agent 最终结果和组合交接验收。

## 已完成

- Message、LLMRequest、LLMResponse、ToolCall、ContinuationState、ToolDefinition、GenerationOptions、TokenUsage 以及 ToolContent、ToolResult 已采用 Pydantic，复用 ContractModel 的严格校验配置。
- 已覆盖角色与结束原因校验、工具调用 ID 配对、JSON 数据合法性、工具结果转消息和续接状态保留。早期 p3-acceptance-review.md 中列出的核心校验缺口已由迁移解决，其构造探针结论属于迁移前历史。
- AgentEvent、LLMEvent、ToolEvent 已统一为独立事件类型与固定 type 判别联合，13 种事件支持 JSON 往返校验。
- 模型流和工具执行器已产生各自事件；Agent 事件已有定义、顺序约定和测试。运行关联约定由上层回调或事件流负责，不恢复 RunEvent。

## 剩余工作

1. 明确 Agent 最终返回结果：最终回答、对话消息与续接状态如何交付，以及正常返回、失败和取消的边界。当前 miniagent/agent/ 只有事件及其导出；可以复用已有类型或增加最小结果类型，不预设必须新增包装。
2. 固化 Loop、Model、Tools、Runtime 的整体交接约定：已有模型和执行器接口，但完整消息组装、不同 finish_reason 的处理以及异常向运行层传播仍需形成统一约定。
3. 补齐组合契约测试：由假模型返回工具调用，通过 ToolExecutor 执行模拟工具，将 ToolResult 转为消息后再次请求模型，最终交付约定的 Agent 结果。覆盖成功及工具失败回传，并断言调用 ID、消息和续接状态保持一致。

现有 test_simulated_failure_round_trip 手工构造响应、结果和事件，验证字段关联；模型适配器与执行器也各有测试。它们尚未共同覆盖包含 Agent 最终结果的完整交接链。

## 验证与阶段边界

本次执行 python -m unittest discover -s tests -q：80 项测试通过，未调用真实模型服务。

P3 验收数据契约，可用测试中的显式调用步骤验证组合，无需提前实现生产 Agent Loop。自动循环属于 P4；运行限制、取消调度、事件汇集与运行隔离实现属于 P5；Session 与整合后的流式 CLI 属于 P6。这些尚未实现本身不构成 P3 的额外缺口。

建议下一个独立小任务是明确并验证 Agent 最终返回结果契约，再推进组件交接与组合验收。
