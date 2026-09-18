# P5：Runtime 生命周期与结果保存

## 问题与本次范围

上一个小任务定义了 RunResult，但没有执行入口保存结果。本次新增单次使用的 AgentRuntime，在内部完整消费现有 AgentLoop，并保存最终结果。运行过程由 AgentEvent 表达，调用方需要进度时完整消费事件。

这是 P5 的生命周期小任务，不等于完成整个 Runtime。流式作用域、主动取消、启动前取消、重复取消以及完整清理故障契约仍留到后续任务；不实现 Harness 或修改 CLI。

## 使用方式

```python
from miniagent.agent import AgentLoop, AgentRuntime

runtime = AgentRuntime(AgentLoop(model, executor))
assert runtime.result is None

result = await runtime.run(messages, options, max_steps=12)
assert runtime.result is result
# result.status 可能为 succeeded 或 failed；非异常失败正常返回。
```

model 和 executor 由调用方创建并管理。另一次独立任务需创建新的 AgentRuntime，可以借用同一个 Loop；这不代表共享依赖已经支持并发。

run 是协程入口，内部完整消费事件并交付最终结果。需要运行进度的调用方使用后续增加的 stream / events 接口，完整消费 AgentEvent；示例见 p5-runtime-stream-cancellation.md。

模型或执行框架异常会原样抛出；调用方捕获异常后仍能查询 runtime.result。进入运行后的调用方任务取消会继续抛出 asyncio.CancelledError，待 Loop 现有清理路径退出后保存 cancelled 结果。此行为不等于已实现 runtime.cancel()。

## 生命周期和结果规则

- AgentStarted、AgentProgress 与 AgentCompleted / AgentFailed / AgentCancelled 表达运行生命周期。
- result 为只读属性；收尾前为 None，不另提供公开状态查询属性。
- 同一实例只启动一次。运行中或结束后再次调用 run 均抛 RuntimeError，不影响原运行或覆盖原结果。
- 收到 Loop 终态事件时只暂存候选结果，继续消费到流结束并关闭流，随后发布结果。这样能够接收 AgentFailed 之后抛出的原始异常，也不会在清理失败时提前发布成功。
- Loop 缺少终态事件、终态后仍发出事件或成功结果不符合已有数据契约时，按执行故障结束。
- 模型异常保存 model_error 和原有统一错误码；普通执行异常保存 execution_error。结果中的错误摘要使用固定安全文本，不复制异常原文；原异常仍向调用方传播。
- step_limit、length、refusal、content_filter 等非异常失败正常返回 failed 结果。
- 单个工具错误仍交回模型，允许最终成功。只有成功结果携带完整历史和模型续接信息。

本次直接转交 max_steps 给 AgentLoop，不增加另一套计数。参数检查也沿用 Loop；例如 max_steps=0 在运行开始后抛 ValueError，保存 execution_error，且没有模型调用。调用方应新建实例再提交修正后的任务。

## 为什么采用这个最小方案

Runtime 只负责一次 Run 的执行控制与最终结果，借用已注入的 Loop；循环调度、消息累积、工具执行以及现有清理能力仍留在原组件。不引入后台调度、事件队列或重试，避免生命周期工作扩展为完整 Harness。

内部使用 aclosing 关闭 Loop 流，复用 Loop 对模型流和工具任务的清理。不会调用 model.aclose()，因而共享模型可以用于后续独立 Run。终态只发布一次依靠单次启动约束和无异步间隙的结果赋值，不依赖界面事件回调。

## 验证

先新增行为测试，确认因缺少 AgentRuntime 导出而失败，再实现入口：

- python -m unittest discover -s tests -p test_agent_runtime.py -v：14 项通过。
- python -m unittest discover -s tests -q：全部 124 项通过。
- 覆盖成功历史与续接信息、非异常失败、步数限制不启动工具、工具失败后成功、模型与框架异常、重复启动、运行隔离、等待清理才发布结果、清理失败不误报成功，以及调用方取消后可查询终态。
- 全部使用本地假模型和模拟 Loop，没有调用真实模型服务。

## 剩余限制

- 尚无 events()、异步运行作用域或主动取消接口；当前入口会消费完整运行。下一小任务再接入作用域与取消控制。
- 协程执行前就取消外部 Task，不会进入 run，实例尚未启动；启动前取消尚未实现。
- 尚未承诺重复取消、清理期间再次取消以及工具不配合取消时的强制终止。取消不能撤销已发生的工具副作用。
- 执行故障与清理故障同时发生时，仍沿用 Python / Loop 的异常传播与异常链；结果只摘要最终传播出的异常，完整双重故障交付需后续明确。
- 不处理进程强制终止，不提供持久化、重试或恢复，也不验证共享依赖的并发安全。
- P5 的整体验收尚未完成，ROADMAP 仅记录局部进展，不勾选整项。
