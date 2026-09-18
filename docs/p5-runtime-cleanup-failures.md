# P5：Runtime 显式关闭流时的双重故障

## 问题与范围

Runtime 原来使用 aclosing 包裹 Loop 流。如果执行先抛出 LLMError，随后流的 aclose() 再抛出 RuntimeError，调用方会收到后一个错误，RunResult 也会误把主要失败归为 execution_error。取消与关闭失败同时发生时也会丢失取消语义。

本次小任务只修复 Runtime 管理的 Loop 流关闭边界，以及作用域退出时的错误交付。运行过程继续完整消费 AgentEvent；不恢复 RunState，不增加异常框架或新的事件类型。

## 交付规则

| 情况 | 最终结果 | 异常交付 |
|---|---|---|
| 执行异常，关闭正常 | 保持原失败原因 | 原异常 |
| 执行异常，关闭也失败 | 保持原失败原因，附 cleanup_error | 原异常，关闭异常作为显式 cause |
| 取消，关闭也失败 | cancelled，附 cleanup_error | CancelledError，关闭异常作为 cause |
| 非异常失败（例如 step_limit），关闭也失败 | 保持原结束原因，附 cleanup_error | 关闭异常 |
| 原本成功，关闭失败 | failed / execution_error，不交付成功历史 | 关闭异常 |
| 作用域主体异常，取消收尾时关闭也失败 | Run 取消结果附 cleanup_error | 主体原异常，关闭异常作为 cause |

正常提前退出原本只执行取消收尾；如果收尾失败，现在传播带关闭异常 cause 的取消异常，避免悄悄忽略清理错误。终态事件仍只在收尾后交付一次，与最终结果类别一致。

## 最小实现与理由

- 将 Runtime 的显式流关闭改为 finally 中单独捕获关闭异常，使它不替换正在传播的执行异常。
- RunResult 新增可选 cleanup_error 字符串摘要。该字段允许用于 failed 或 cancelled，禁止用于 succeeded；默认不进入 repr。
- Runtime 只写入固定安全摘要，不序列化原始异常消息或 traceback。需要原始对象时，调用方在异常边界检查 __cause__。
- 同步关闭失败只附加诊断，完整的关闭尝试完成后才发布最终结果。关闭失败不意味着资源一定已经释放。
- 显式异常链保留原异常对象；没有关闭故障时维持既有异常行为。

使用 Python 3.10 测试时，跨 Task 等待得到的 CancelledError 可能由 asyncio 重新生成，其 cause 不保证保留。已在 run 协程抛出位置验证原始链；跨任务查询可依赖 RunResult.cleanup_error 的安全摘要。

## 验证

先新增测试复现原模型错误被关闭错误替换，再完成修复。

python -m unittest discover -s tests -q：全部 152 项离线测试通过。本次新增 6 项 Runtime 测试和 2 项结果校验测试；Runtime 共 40 项，结果契约共 10 项。

覆盖模型/框架原异常身份、取消、非异常失败、成功后关闭失败、作用域主体异常、提前退出收尾失败、清理摘要校验与 JSON 往返。原有流式、步数、工具往返、重复取消和资源所有权测试继续通过。未调用真实模型服务。

## P5 验收复核

| 验收领域 | 证据与结论 |
|---|---|
| 独立 Run 与完整成功历史 | 已有隔离、工具失败后完成及模型续接测试通过 |
| 完整 AgentEvent 消费与单一终态 | 已有成功、失败、步数上限及取消事件测试通过 |
| 调用边界与步数限制 | 达到上限或取消后不启动下一模型/工具调用的测试通过 |
| 作用域收尾与资源所有权 | 提前退出、重复取消、等待关闭、借用模型不关闭的测试通过 |
| Runtime 显式关闭时双重故障 | 本次新增测试通过 |
| Loop 内部模型流关闭的双重故障 | 复核发现仍有缺口，P5 保持未完成 |

剩余缺口已通过独立离线脚本复现：模型流的 __anext__ 抛 LLMError，aclose 再抛 RuntimeError。AgentLoop 内部的 aclosing 将 RuntimeError 交给 Runtime，原 LLMError 只留在隐式 context；RunResult 原因成为 execution_error。

Runtime 不能仅凭任意异常链猜测上游发生了清理错误。本次没有修改模型适配器或 Loop 的模型流边界。下一小任务应在真正执行关闭的 Loop 边界保留原模型异常与取消，再复核工具收尾和 P5 整体验收。第三方组件在自身 finally 中覆盖错误的行为，也需要由该组件负责明确交付。
