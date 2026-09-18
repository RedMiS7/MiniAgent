# P5：Run 结果契约

## 问题与范围

现有 AgentLoop 通过 AgentCompleted 交付成功历史，通过 AgentFailed 或异常报告失败，但尚无独立的 Run 最终结果类型。本次先统一结果的数据契约，为 Runtime 保存和查询运行事实提供基础，避免展示层自行解释成功、失败和取消。

本次属于 ROADMAP 的 P5 第一个小任务，仅新增类型、公开导出及离线测试。尚未实现 Runtime，不修改 Loop、CLI 或事件协议，不标记 P5 完成。

## 契约与使用

从 miniagent.agent 导入 RunResult，其 status 只接受 succeeded、failed、cancelled 三种最终结果类别。运行过程通过完整消费 AgentEvent 判断，不另定义公开运行状态类型。

RunResult 仅交付收尾后的结果，不表达待启动或运行中阶段。本次只验证结果数据结构；启动、进度与结束由既有 AgentEvent 表达。

```python
from miniagent.agent import RunResult

result = RunResult(
    status="succeeded",
    reason=completed.reason,
    messages=completed.messages,
)
```

上述 completed 是现有 AgentCompleted 事件；这只是显式交接示例，不表示 Loop 已自动生成或保存 RunResult。

| 字段 | 约定 |
|---|---|
| status | succeeded、failed、cancelled |
| reason | 非空结束原因；成功只能为 stop，取消只能为 cancelled |
| messages | 成功历史，复用现有 Message；失败和取消只能为空 |
| error_code / error_message | 可选错误码和摘要，必须同时提供，只允许在 failed 中使用 |

失败原因沿用 AgentFailed 的非空字符串形式，以容纳模型非成功结束原因与执行故障，不额外维护一套完整枚举；拒绝 stop、cancelled 和中间阶段 tool_calls。step_limit、length、refusal、content_filter 可不带异常摘要；model_error 和 execution_error 可附错误码与摘要。只有失败事件而没有异常详情时，也能表达失败结果。

RunResult 使用现有严格、冻结的 ContractModel；字段赋值修改、未知字段及错误类型均被拒绝。messages 接受列表并归一化为元组，与现有模型类型保持一致。冻结结果对象不替代 Runtime 的终态锁定，也不保证调用方通过低层复制或绕过校验接口构造的数据有效。

## 为什么这样设计

- status 只表达最终结果类别，reason 表达结束原因。例如 step_limit 对应 failed；运行过程不新增一套状态定义。
- 成功结果必须包含非空、工具调用与结果配对完整的历史，并以不带工具调用的 assistant 消息结束。复用 LLMRequest 的历史校验，避免复制工具配对规则。
- 保留 Message 中不透明的模型续接状态，不解析提供商数据。工具错误结果可以出现在成功历史中，因为单工具失败不等于 Run 失败。
- 失败与取消不暴露可续接历史，避免把包含未完成工具调用的数据传给下一轮。部分历史诊断和恢复点不在本次范围。
- 错误使用可序列化的码和摘要，不在结果中保存异常对象或 traceback。异常传播方式、原始异常与清理异常如何同时保留，留待 Runtime 收尾实现确定。
- messages 和 error_message 不进入默认 repr；显式序列化仍包含这些字段，因此 repr 隐藏不是脱敏。后续 Runtime 应提供安全摘要，调用方不应默认记录完整序列化结果。

RunResult 是运行事实而非事件，不新增 RunEvent 包装，不给底层事件增加 run_id。运行关联、终态保存及事件消费中断处理仍待 Runtime / Harness 接入。

## 验证

- python -m unittest discover -s tests -p test_run_types.py -v：8 项测试通过。
- python -m unittest discover -s tests -q：全部 110 项测试通过。
- 覆盖终态限定、矛盾状态与原因、错误详情配对、非法成功历史、工具失败后的成功交付、续接状态与 JSON 往返、冻结字段及敏感字段 repr 隐藏。
- 验证使用本地 Python 与离线模拟数据，未调用真实模型服务。

## 剩余限制与下一步

尚无启动、取消、步数控制、资源清理或独立查询入口，也不保证终态只确定一次；这些是 Runtime 执行行为，不能由一个冻结结果类型代替。下一小任务再实现生命周期与结果保存，并在假模型下验证状态转换和异常后的结果查询。
