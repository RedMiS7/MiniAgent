# P6 Harness 多任务与适配器复用验收

## 要解决的问题

已有组合入口、审批和事件回调，但需要从真实调用方角度证明 Harness 能复用。
只替换两个假模型不足以验证提供商协议边界，所以本次同时使用 OpenAIAdapter 与
DeepSeekAdapter 的真实实现，通过 SDK 的 HTTP 模拟传输进行端到端离线验收。

## 范围和取舍

不修改生产执行代码。新增 examples/echo_extension.py，按现有 register 协议注册
一个无副作用的 echo 工具，不借用系统命令，也不默认加入所有任务的工具集合。
examples/harness_reuse.py 用同一个 Harness 实例运行文件摘要与 echo，传入各自独立
用户消息。文件任务经过列目录和读文件两轮工具调用，文本任务经过一轮 echo。
只暴露所需的三个工具。模型借用，示例调用方关闭模型；不引入会话历史或工具热替换。

可运行示例使用脚本化模型和临时文件，避免学习者必须先配置真实 API。它不验证生成
质量；摘要文本是预设的。额外 echo-only 注册表测试证明工具组合可以变化，无需修改
Loop、Runtime、Harness 或工具执行器。

## 验证证据

新增 4 项测试：两个真实适配器各一项、echo-only 工具组合一项、离线示例一项。
真实适配器测试使用 AsyncOpenAI + httpx.MockTransport，对 Responses API 和 Chat
Completions 分别提供模拟 SSE 数据，不 mock 统一模型接口。每个适配器完成两个独立
任务、五次模型请求，验证：

- list_files/read_file/echo 的真实工具结果进入下一次模型请求。
- Tool Calls 和结果的调用 ID 完整配对。
- OpenAI encrypted reasoning 与 DeepSeek reasoning_content 保留到下一轮。
- 第二个 Run 的首轮请求仅包含新任务消息，不串历史或续接信息。
- 回调关联两个独立 Run，工具执行事件恰好对应三次调用。
- 所有 HTTP 响应流关闭；Harness 不误关借用客户端，调用方可以最后关闭它。

已运行 python -m examples.harness_reuse，两个任务均显示 succeeded / stop。
运行 .venv/Scripts/python.exe -m unittest discover -s tests -q：全部 220 项通过。
所有验证离线进行，不证明真实服务、账号或模型任务效果。

## 路线图关系

标记 P6“不同任务验证 Harness 复用，编写使用示例”完成；仍不标记整体 P6 完成。
审批、取消、事件汇集和首版复用已经具备；路线图中的自动重试/恢复与之前“首版失败后
结束、不自动重试”的约定仍需分清后续范围。本次不擅自增加恢复点或副作用重放机制。
