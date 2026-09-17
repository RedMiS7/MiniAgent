# DeepSeek 思考模式调用检查

## 问题与范围

检查用户命令 `python agent_cli.py --provider deepseek --model deepseek-flash --workspace .` 为何看不到思考输出。本次只做调用层检查，不修改默认生成行为。

## 结论与理由

CLI 未提供 --reasoning 时 GenerationOptions.reasoning=None。DeepSeekAdapter 省略 thinking 和 reasoning_effort，使用服务端默认设置，并未发送 disabled。
2026-09-17 查阅 DeepSeek 官方思考模式文档：当前默认开启思考，默认 effort=high。
因此不能根据控制台缺少思考文字判断未开启，也不能在未查看真实响应的情况下断言这次请求一定产生了思考内容。

ChatCompletionsAdapter 已接收并累积 reasoning_content，最终保存在 Message.continuation 内，供工具往返和多轮历史续接使用。
LLMEvent 没有思考增量事件，CLI 只展示正文 TextDelta、响应结束和执行状态。这是展示通道缺失，不是已经证实的思考开关故障。

## 验证

- tests/test_bailian.py：9 项离线测试通过。
- tests/test_models.py：26 项离线测试通过。
- 无网络请求构造探针确认：省略 reasoning 不发送开关；high 发送 thinking.type=enabled 与 reasoning_effort=high；none 发送 thinking.type=disabled。
- 未调用真实服务、未读取密钥、未输出真实对话或续接状态。

可明确指定：`python agent_cli.py --provider deepseek --model deepseek-flash --workspace . --reasoning high`。
此参数控制请求，不会使当前 CLI 自动展示 reasoning_content。

## 阶段关系与限制

本次检查 P1 适配层与 P4/P6 展示之间的边界。若后续要求展示服务端返回的思考文本，应在模型层定义独立事件，经 Loop 转发后由 CLI 显示；不让 Agent 解析厂商续接 payload。
现有百炼 qwen3.7-plus 的 effort 处理不同，不应将 DeepSeek 配置直接推广到所有模型。

官方来源：https://api-docs.deepseek.com/guides/thinking_mode/
