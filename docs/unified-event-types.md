# 统一事件为独立 Pydantic 类型

## 为什么修改

此前 LLMEvent 是多个事件类的联合，ToolEvent 与 AgentEvent 则用单个类的 type 字段分支。
这导致判断方式、校验方式和不同状态可用字段不一致。
本次按用户要求统一为“每种事件独立类型 + 固定 Literal 标签 + 按标签区分的联合类型”。

## 选择与改动

复用已有 ContractModel 配置，不另建事件基类体系。
LLM 事件保留原类名；工具拆为 ToolStarted、ToolCompleted、ToolFailed、ToolCancelled，
Agent 拆为 AgentStarted、AgentProgress、AgentCompleted、AgentFailed、AgentCancelled。
所有构造使用关键字参数，联合类型只用于注解和 TypeAdapter 解析。

标签分别采用 llm_、tool_、agent_ 前缀，避免模型生成工具调用与工具实际执行混淆。
工具事件从 executor.py 移至 tools/events.py，与模型和 Agent 的事件模块结构一致。
执行器、适配器、CLI、公共导出和测试同步更新。只有失败工具事件允许错误码；
开始事件没有耗时，CLI 对此明确处理。

这是公共接口变更：旧 ToolEvent("started", ...) 与 AgentEvent("started") 构造不可用，
应改用 ToolStarted(...) 与 AgentStarted()。LLM 事件也改为关键字参数。
模型/工具执行顺序、错误回传格式、取消传播和观察者异常隔离保持原有行为。

## 验证

先添加新类型的验收测试，确认因新事件类尚未定义而失败。
实现后 80 项离线测试全部通过，验证所有 13 种事件的标签、类型、JSON 往返和非法数据处理。
原有模拟适配器与工具执行测试通过。CLI 另验证 list_files（path 为 .）成功及未知工具失败，
分别输出 tool_started → tool_completed / tool_failed，结果 JSON 与退出码正确。

## 与 P3 的关系

本次统一已有事件契约及其调用方，不恢复 RunEvent，也不将运行 ID 加入底层事件。
运行关联仍由上层负责；Agent 的最终结果契约、实际 Loop 和 Runtime 未纳入本次任务。
使用模拟模型验证，不代表真实模型服务已验证。
