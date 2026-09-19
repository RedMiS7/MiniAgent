# P6 指定工具逐次人工审批

## 问题与本次范围

Brave Search MCP 已能通过 extension 注册，但尚不能验证执行前人工审批。
本次仅实现指定工具逐次审批，以 brave_web_search 为真实接入对象；原有轻量工具不增加
审批配置。保留首版失败后结束 Run 的约定，不自动重试、不新增 RunState、不推进恢复策略。

## 为什么这样划分

Harness 配置 approval_required 工具名集合及异步 approve(call, arguments) 回调，
不把终端交互或工具实现放进 Harness。集合外的注册工具维持原行为，不引入完整权限系统。
配置受保护工具却没有回调、或工具名不存在时立即拒绝构造，避免拼写错误导致审批遗漏。

现有 ToolExecutor 缺少“参数有效但尚未执行”的边界，因此增加按次传入的可选 approve
回调。先解析并校验参数，向审批方交付副本，再执行原工具。Harness 使用局部包装委托给
原执行器，不修改共享执行器，也不复制注册表和参数验证逻辑。MCP extension、Loop 和
Runtime 均保持不变。审批是这次增加的执行控制能力，不是 MCP 工具注册的特殊分支。

拒绝返回 approval_denied 工具结果，模型可以继续回应。回调故障或返回非布尔值抛出
ToolApprovalError，并发布安全的 approval_error 工具事件，Runtime 记录 execution_error。
原始异常作为异常链保留，不把诊断正文写进事件。ToolStarted 仍表示调用处理开始，
不代表批准或副作用已经发生。

审批等待通过独立任务和 shield 隔开取消传播：收到取消时取消审批任务并等待它收尾，
即使回调吞掉取消并返回 True，外层仍传播取消，工具不会启动。非合作回调仍可能阻塞收尾，
不承诺强制终止。Run 提前退出同样走现有 Runtime/Loop 清理，不建立第二套运行状态。

## 使用与验证

新增 harness_cli.py，组合 Brave MCP 连接、模型与 Harness，完整展示已有事件。
每次搜索展示参数，只接受明确批准；非 TTY 默认拒绝。Windows 用键盘轮询响应 Ctrl+C，
不创建阻塞 input 线程；POSIX 用 select 等待一行终端输入。SDK 模型重试固定为 0。
原 brave_mcp_cli.py 保留为显式诊断入口，直接调用不受 Harness 策略保护。

新增 14 项离线测试，使用真实 MCP extension、假 session 和假模型，覆盖批准、拒绝后
再次审批、异常/非法决定、无副作用等待、迟到批准、非法参数、审批参数隔离、配置遗漏、
未保护工具、原目录限制、CLI 关闭资源、非交互拒绝、提前退出与复用、共享执行器隔离、
终端等待取消。运行 .venv/Scripts/python.exe -m unittest discover -s tests -q：
全部 206 项离线测试通过，harness_cli.py --help 检查通过。未调用真实模型或搜索 API。

P6 的事件回调汇集、重试与恢复仍待后续；本次不勾选整个策略条目完成。
