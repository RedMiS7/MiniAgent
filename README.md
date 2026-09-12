# MiniAgent

统一的模型调用层。上层只使用项目自己的 LLMRequest、LLMResponse、Message、
ToolDefinition、ToolCall、LLMEvent 和 LLMError，不使用 SDK 对象或厂商参数。

## 安装

需要 Python 3.10+，在项目根目录执行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

## 真实 API 测试

密钥从 OPENAI_API_KEY 或 DEEPSEEK_API_KEY 读取，不接受密钥命令行参数，
不自动加载 .env。CLI 默认发送“你好”，真实请求会产生服务用量。

PowerShell 隐藏输入密钥，避免把真实密钥字面值写入命令历史：

```powershell
$credential = Get-Credential -UserName 'api' -Message '在密码框输入 DeepSeek API Key'
$env:DEEPSEEK_API_KEY = $credential.GetNetworkCredential().Password
.\.venv\Scripts\python.exe cli.py --provider deepseek --model deepseek-flash --prompt "你好"
```

OpenAI 使用相同参数：

```powershell
$credential = Get-Credential -UserName 'api' -Message '在密码框输入 OpenAI API Key'
$env:OPENAI_API_KEY = $credential.GetNetworkCredential().Password
.\.venv\Scripts\python.exe cli.py --provider openai --model gpt-6-astra --prompt "你好"
```

模型 ID 必须对你的账号可用。OpenAI 模型需要支持 Responses API，
DeepSeek 模型需要支持 Chat Completions。示例模型没有经过真实账号验证。

两家共用的 CLI 参数：

- `--stream`：逐段显示文本。
- `--reasoning none|low|medium|high|xhigh|max`：推理选项；省略时使用服务默认值。
- `--max-output-tokens 4096`：本次最大生成 token 数，包含模型推理消耗，非纯文本长度保证。
- `--temperature 0.5`：采样温度，仅在底层支持时接受。
- `--system "请用中文简短回答"`：system 消息。
- `--timeout 60`：SDK 超时秒数，不是包含重试的整体调用时限。
- `--max-retries 0`：关闭 SDK 重试，默认 2；业务层不重复重试。

成功调用返回 0；模型错误返回 1；CLI 参数或配置错误返回 2；用户中断返回 130。
拒绝、截断、内容过滤属于生成结果，CLI 会在 stderr 提示 finish_reason，
而不是把它伪装成完整回答。流式失败前已经输出的文本只是部分结果。
CLI 不执行工具；工具协议由上层 Agent / 工具执行器使用。

## 统一协议

```text
Agent / CLI
    → LLM.generate(LLMRequest) 或 LLM.stream(LLMRequest)
    → OpenAIAdapter / DeepSeekAdapter
    → OpenAI SDK
    → LLMResponse / LLMEvent，失败则抛出 LLMError
```

- `models/types.py`：请求、响应、消息、工具、用量、续接状态。
- `models/base.py`：LLM.generate、stream、aclose 协议。
- `models/events.py`：TextDelta、ToolCallStarted、ToolArgumentsDelta、ResponseCompleted。
- `models/errors.py`：LLMError，包括 code、message（str(error)）和 retryable。
- `models/openai_adapter.py`：Responses API 的转换。
- `models/deepseek_adapter.py`：Chat Completions 的转换。
- `models/adapter_utils.py`：适配层共用的 SDK 错误和解析辅助。
- `config.py`、`bootstrap.py`：配置校验、密钥读取和依赖组装。

公共接口已从 Model.generate(messages) 迁移到 LLM.generate(LLMRequest)；
ModelResponse / ModelError 替换为 LLMResponse / LLMError，不保留两套协议。

入口负责创建模型并把 LLM 实例注入上层；以下业务函数无需知道提供商：

```python
from miniagent.models import LLM, LLMRequest, Message, GenerationOptions

async def ask(llm: LLM):
    request = LLMRequest(
        messages=[Message("user", "你好")],
        options=GenerationOptions(reasoning="high", max_output_tokens=4096),
    )
    response = await llm.generate(request)
    return response.text
```

程序入口：

```python
from miniagent.bootstrap import create_model
from miniagent.config import ModelConfig

async def main():
    llm = create_model(ModelConfig.from_env("deepseek", "deepseek-flash"))
    try:
        return await ask(llm)
    finally:
        await llm.aclose()
```

## 差异处理

| 统一选项 / 行为 | OpenAI 适配器 | DeepSeek 适配器 |
|---|---|---|
| 请求入口 | Responses | Chat Completions |
| max_output_tokens | max_output_tokens | max_tokens |
| reasoning | reasoning.effort | thinking 开关及 reasoning_effort |
| medium / xhigh | 原样传入 | 按官方兼容规则映射为 high |
| none | GPT-6 系列本地拒绝 | 关闭 thinking |
| temperature | GPT-6 系列本地拒绝 | 只有显式 reasoning=none 才接受，避免被服务忽略 |
| 工具定义 | 扁平 function 字段 | 嵌套 function 字段 |
| 工具结果 | function_call_output + call_id | tool 消息 + tool_call_id |
| 续接状态 | 加密 reasoning items，store=False | reasoning_content |
| 流式事件 | Responses 事件转换 | Chat chunks 拼接转换 |

相同推理级别不保证不同模型具有相同质量或消耗。当前不维护所有模型的完整能力表：
已知不支持的组合本地抛出 unsupported_feature；其他模型限制由服务校验并映射为统一错误。
不自动切换提供商，不提供 extra_body 或任意厂商参数透传。
当前不支持图片、音频、结构化输出选项、内置托管工具或 Agent 执行循环。

## 工具调用与续接

```python
from miniagent.models import LLMRequest, Message, ToolDefinition

messages = [Message("user", "北京天气如何？")]
tools = [ToolDefinition(
    name="weather",
    description="查询天气",
    parameters={
        "type": "object",
        "properties": {"city": {"type": "string"}},
        "required": ["city"],
    },
)]
response = await llm.generate(LLMRequest(messages, tools))
if response.finish_reason == "tool_calls":
    messages.append(response.message)  # 原样保存，包括 continuation
    # 工具执行器先解析 arguments JSON、校验 schema 和权限，再执行工具。
    for call in response.message.tool_calls:
        result_text = await execute_validated_tool(call)
        messages.append(Message("tool", result_text, tool_call_id=call.id))
    response = await llm.generate(LLMRequest(messages, tools))
```

示例中的 execute_validated_tool 是上层应用需要实现的函数，本项目不执行工具。
ToolCall.arguments 保留原始 JSON 字符串，不保证其合法性。所有工具结果必须关联原 ID，
并在下一次请求前全部回传；截断结果中的工具参数不可执行。

Message.continuation 是可序列化的 opaque JSON 状态，上层只保存和回传，不解释、不打印。
适配器校验 provider、model 和格式版本，防止跨提供商 / 模型复用。
DeepSeek 思考模式下携带 tools 的历史 assistant 消息必须保留原始续接状态。
普通纯文本调用中丢弃过状态后，不能保证可以中途加入工具模式。
OpenAI 返回的 reasoning items 以加密内容续接；上层仍使用同一个 Message 类型。

## 流式调用

```python
from contextlib import aclosing
from miniagent.models import TextDelta, ResponseCompleted

async with aclosing(llm.stream(request)) as events:
    async for event in events:
        if isinstance(event, TextDelta):
            print(event.text, end="", flush=True)
        elif isinstance(event, ResponseCompleted):
            response = event.response
```

成功流最后产生一次 ResponseCompleted，包含完整消息、工具参数、续接状态及用量。
工具参数增量按 ToolCallStarted / ToolArgumentsDelta 的 index 分别拼接；index 是流内标识。
拒绝信息和推理状态通过最终响应返回，不作为普通文本增量展示。
调用失败抛出 LLMError，不再额外发送错误事件，也不在已经输出部分内容后自动重放。
提前退出时使用 aclosing 关闭异步生成器；程序结束时再关闭模型实例。

finish_reason 为 stop、tool_calls、length、refusal 或 content_filter。
usage 缺失时是 None，不能视为零消耗。SDK 和 HTTP 错误不会携带原始服务错误正文。

## 离线验证

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

测试通过 httpx.MockTransport 经过实际 SDK 的请求序列化、响应解析及 SSE 解析，
验证两家的统一请求、工具续接、参数差异、流式拼接、清理和错误处理。
不访问真实模型，不证明账号权限、服务可用性或实际模型行为。

协议参考：[OpenAI Responses](https://developers.openai.com/api/docs/guides/reasoning)、
[GPT-6 Astra](https://developers.openai.com/api/docs/guides/latest-model?model=gpt-6-astra)、
[DeepSeek 思考与工具调用](https://api-docs.deepseek.com/guides/thinking_mode/)。
