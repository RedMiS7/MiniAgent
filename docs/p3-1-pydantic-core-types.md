# P3.1：核心数据类型迁移到 Pydantic

## 要解决的问题

P3 验收发现，类型注解未阻止非字符串调用 ID、非法模型响应和工具结果内容进入调用链。
本次按用户选择使用 Pydantic v2，将模型核心类型与工具内容、结果迁移到 BaseModel。
目的不是引入新的模型协议，而是让已有协议在构造边界执行一致的校验。

## 范围与选择理由

- models/types.py 中的 ContinuationState、ToolDefinition、ToolCall、Message、GenerationOptions、
  LLMRequest、TokenUsage、LLMResponse 迁移到 BaseModel。
- tools/base.py 中的 ToolContent、ToolResult 同步迁移，ToolContext 保留 dataclass。
- miniagent/_validation.py 提供只有统一配置的 ContractModel，供模型与工具复用；
  不放在 agent/ 下，避免基础组件反向依赖 Agent 层。
- 事件类型、配置类和图片专用类型保留原样；这是一次核心数据契约迁移，不继续下一项事件迁移。
- 直接声明 pydantic==2.13.4，使用环境中已有版本，没有安装或升级全局包。

统一配置启用 strict、frozen、extra="forbid"、validate_default、禁止非有限浮点数，
并设置 hide_input_in_errors，减少错误字符串包含原始输入的风险。
frozen 与此前 frozen dataclass 一样不提供字典、列表的深层不可变保证。
调用方仍不应修改已构造对象内部的 JSON 数据，也不应默认记录完整 model_dump 或 ValidationError.errors()。

## 校验与兼容性

基础类型和 Literal 由 Pydantic 校验，非空 ID、数值边界通过 Field 约束。
field_validator 处理 JSON 和 list 到 tuple 的显式规范化；
model_validator 保留消息角色、工具调用配对等业务规则，并补充响应角色、结束原因和工具结果状态检查。

继续允许 messages、tools、tool_calls、content 输入列表并规范化为 tuple。
续接状态原样保留，不由上层解析。工具参数仍是原始字符串，非法或不完整 JSON 仍交给执行器处理。
ToolContent.value 限制为 JSON 值；text 内容还必须是字符串。
成功工具结果不能附带错误字段，失败结果仍允许省略诊断字段以保持兼容。

LLMResponse 必须包含 assistant 消息；tool_calls 结束原因必须有调用，
stop 不能同时带工具调用；length 等非正常结果仍允许保留部分调用，不提前执行。
ToolResult.to_message 保持 success/content/error 的原有 JSON 结构，不改为直接 dump 整个对象。

## 公共接口变化

构造使用关键字参数，例如：

```python
Message(role="user", content="你好")
ToolResult(success=True, content=(ToolContent(type="text", value="完成"),))
```

适配器、extensions、CLI、测试与 README 示例已同步修改。
tools_cli.py 使用 model_dump() 替换 dataclasses.asdict()。

构造非法核心对象抛出 pydantic.ValidationError，不再抛 LLMError。
适配器既有 sdk_errors() 捕获 ValueError（包含 ValidationError），继续向上抛出
LLMError("invalid_response", ...)；新增测试确认原始输入不出现在转换后的错误中。
CLI 沿用 ValueError 配置错误路径，非法生成选项在创建模型客户端前被拒绝。
工具执行中构造非法结果所产生的异常沿用执行器的 execution_error 处理。

## 验证

先加入迁移测试，确认旧实现未拒绝非法数据，且未实现 BaseModel 接口。
完成迁移后执行：

```powershell
python -m unittest discover -s tests -q
python tools_cli.py --list
git diff --check
```

81 项离线测试通过（原有 72 项与新增 9 项），工具列表能序列化出 6 个合法对象，diff 检查无空白错误。
新增覆盖严格类型、非法角色与结束原因、重复/缺失工具结果、JSON 值类型、
工具回传格式、续接状态、冻结字段、敏感字段 repr、SDK 异常转换和 CLI 配置错误。
模型服务使用模拟响应，没有验证真实接口。

## 与路线图的关系及剩余限制

本次解决 P3 核心类型校验缺口，沿用已有模型和工具协议。
P3 整体仍需明确 Agent 最终返回结果和组件交接约定，并完成相应完整模拟调用链验收。
事件契约已存在，但事件的 Pydantic 迁移没有纳入本次范围。
没有实现 P4 执行循环或 P5 Runtime，也没有新增自动重试、工具授权或提供商切换。
