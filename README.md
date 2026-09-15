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

密钥从 OPENAI_API_KEY、DEEPSEEK_API_KEY 或 DASHSCOPE_API_KEY 读取，不接受密钥命令行参数，
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
    → OpenAIAdapter / DeepSeekAdapter / BailianAdapter
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

## 百炼：qwen3.7-plus 与 glm-5

两种模型均使用 provider=bailian，通过阿里云北京地域的 OpenAI 兼容 Chat Completions
接口调用，不是智谱官方直连接口。仅接入指定的两个模型 ID，不自动升级或切换。

```powershell
$credential = Get-Credential -UserName 'api' -Message '在密码框输入北京地域百炼 API Key'
$env:DASHSCOPE_API_KEY = $credential.GetNetworkCredential().Password
python cli.py --provider bailian --model qwen3.7-plus --prompt "你好"
python cli.py --provider bailian --model glm-5 --prompt "你好" --reasoning high --stream
```

当前使用仍受支持的北京域名 https://dashscope.aliyuncs.com/compatible-mode/v1，
无需提供业务空间 ID。需要使用北京地域有效的 API Key，真实请求会消耗额度。
不使用 Coding Plan 专用入口，不自动读取 .env 文件。

上层继续传相同的 LLMRequest / GenerationOptions：

| 统一选项 | qwen3.7-plus | glm-5 |
|---|---|---|
| reasoning 省略 | 使用服务默认模式 | 使用服务默认模式 |
| reasoning=none | enable_thinking=false | enable_thinking=false |
| 推理档位 | 文档没有等价档位映射，明确报 unsupported_feature | low/medium/high → high，xhigh/max → max |
| max_output_tokens | max_completion_tokens | max_completion_tokens |
| temperature | 原样传入 | 原样传入 |
| 历史续接状态 | reasoning_content + preserve_thinking=true | reasoning_content + clear_thinking=false |

输出上限使用包含推理与回答的 max_completion_tokens，避免旧 max_tokens 的语义差异；
服务文档说明实际 token 数可能有最多 10 个 token 的误差。
不把 Qwen 的 thinking_budget 自动等同于推理档位，不新增任意厂商参数透传。
如需要 Qwen 精确思考预算，应另行定义统一预算语义后扩展接口。
保存模型返回的原始 Message，适配器会自动转换其 continuation；跨模型状态会被拒绝。

新增 models/bailian_adapter.py 负责百炼参数规则；
models/chat_adapter.py 复用 DeepSeek 与百炼的 Chat 响应、工具和 SSE 转换。
OpenAI Responses 的转换保持独立。
新增 tests/test_bailian.py 覆盖两模型的协议模拟测试，尚未做真实 API 验证。

参考：[百炼 Chat Completions API](https://help.aliyun.com/zh/model-studio/qwen-api-via-openai-chat-completions)。

## Tools 与 extensions（第一版）

本版提供工具注册与执行层，尚未加入 Agent 自动循环，也没有 MCP 客户端。
模型层和原有 cli.py 保持原来的职责；tools_cli.py 可独立测试工具，无需 API Key。

- tools/base.py：异步 Tool 接口、ToolContext、ToolResult、文本/JSON 内容。
- tools/registry.py：注册、重名检查、导出已有 ToolDefinition 和 JSON Schema 校验器。
- tools/executor.py：解析 ToolCall、校验参数、执行、统一结果和生命周期事件。
- extensions/files.py：list_files、read_file、write_file、edit_file。
- extensions/shell.py：run_command，使用 argv 数组启动进程。
- bootstrap.create_tools()：显式加载内置 extensions。

安装依赖后试用：

```powershell
python tools_cli.py --list
'{"path":"."}' | Set-Content -Encoding UTF8 tool-args.json
python tools_cli.py --workspace . --tool list_files --arguments-file tool-args.json
```

tools_cli 的 stderr 显示 started/completed/failed/cancelled、工具名和耗时，
stdout 输出本次工具的 JSON 结果。成功退出 0，执行失败 1，CLI 参数错误 2，取消 130。
参数文件可放任意位置，由用户明确指定；其内容经过工具 Schema 校验。

文件工具仅支持工作目录内的 UTF-8 文本。read_file 默认读 200 行并返回 sha256，
write_file 只新建、不覆盖，edit_file 必须提供 read_file 返回的 expected_sha256，
且 old_text 恰好匹配一次。修改结果包含有长度限制的 diff。父目录必须已存在。
文件大小默认上限 1 MiB，展示结果默认上限 16384 字符，目录最多列出 200 项。
绝对路径、目录越界、Windows ADS 和指向工作区外的符号链接会被拒绝。
路径与 hash 校验不是操作系统沙箱，也不提供面对恶意并发路径替换、硬链接或崩溃时的事务保证。

命令工具默认禁用，试用必须传 --allow-commands：

```powershell
'{"argv":["python","--version"]}' | Set-Content -Encoding UTF8 tool-args.json
python tools_cli.py --tool run_command --arguments-file tool-args.json --allow-commands
```

命令采用 argv 分离传参，不自动拼接 shell 字符串；需要 shell 时必须显式调用对应程序。
不打开交互窗口，stdin 关闭。默认超时 30 秒，stdout/stderr 各最多捕获 16384 字节，
超出部分继续排空并标记 truncated。返回 exit_code、stdout、stderr。
超时或取消会尝试终止进程树：Windows 使用 taskkill /T，POSIX 使用进程组。
Windows 父进程提前退出、脱离进程树的子进程可能无法清理；不承诺强进程隔离。

命令是受信任的本地执行，继承宿主权限和环境，cwd 不是安全边界；
它可以访问工作区外文件和网络。启用前应在应用层确认任务授权。
默认事件不包含完整参数、文件内容或命令输出；若 UI 需要显示完整命令，应按敏感信息策略处理。
工具执行结果会包含内容，但不会自动写日志。事件回调故障不影响实际工具结果，避免诱发重复写入。

上层使用示例：

```python
from pathlib import Path
from miniagent.bootstrap import create_tools
from miniagent.models import LLMRequest, Message
from miniagent.tools import ToolContext, ToolExecutor

registry = create_tools()
executor = ToolExecutor(registry, ToolContext(Path(".")), on_event=print)
request = LLMRequest([Message("user", "查看目录")], tools=registry.definitions())
response = await llm.generate(request)
if response.finish_reason == "tool_calls":
    messages = [*request.messages, response.message]
    for call in response.message.tool_calls:
        result = await executor.execute(call)
        messages.append(result.to_message(call.id))
    # messages 可用于下一次模型调用；完整 Agent 循环在后续实现。
```

新增 extension 只需实现异步 Tool，并暴露 register(registry)：

```python
from miniagent.models import ToolDefinition
from miniagent.tools import ToolContent, ToolResult

class EchoTool:
    definition = ToolDefinition("echo", "返回文本", {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
        "additionalProperties": False,
    })

    async def execute(self, arguments, context):
        return ToolResult(True, (ToolContent("text", arguments["text"]),))

def register(registry):
    registry.register(EchoTool())
```

将模块加入 create_tools() 的显式加载列表即可。Extension 是受信任 Python 代码，
不是权限隔离单元；不做动态扫描、热加载或任意路径插件加载。
MCP 后续可通过代理工具接入同一接口，本版内容类型只有 text/json，
图片和资源需要明确扩展类型与模型转换，不会静默压成文本。

参数使用 jsonschema Draft 2020-12 校验；注册时拒绝远程 Schema 引用。
工具 Schema、参数、路径和命令授权都由程序检查，不能由模型声明跳过。
新增测试在临时目录执行文件操作，只运行测试用 Python 命令，不调用真实模型。

## Image extension

image 工具有 inspect（识别）和 generate（生成）两种操作。
工具名称、描述及参数 Schema 均位于 extensions/image.py 的 ImageTool 类内。
工具只使用 ImageBackend 协议，模型能力和厂商接口转换集中在 models/image_adapter.py。

默认使用创建工具时注入的当前 ModelConfig，不另选模型：
```python
current = ModelConfig.from_env("bailian", "qwen3.7-plus")
registry = create_tools(
    current,
    image_models={
        "drawing": ModelConfig.from_env("bailian", "qwen-image-plus"),
    },
)
```

ToolCall 的 model 参数省略或为 current 时，绑定 current 配置。
显式 model="drawing" 只选择本次图片调用的后端，不修改主对话模型。
当前模型发生改变时，入口应使用新的配置重新创建工具注册表。

能力未接入、当前模型未配置，或指定模型不在配置列表中时，返回：
```json
{
  "success": false,
  "content": [{"type": "json", "value": {
    "selected_model": "current",
    "available_models": ["drawing"]
  }}],
  "error": {"code": "unsupported_feature", "message": "..."}
}
```
available_models 只包含已配置且该操作有适配能力的备选模型，不保证账号权限。
Agent 可明确指定其中一个重试，或向用户解释无法完成。工具绝不自动回退。
鉴权、限流、超时等错误也会统一返回；不会把所有失败都误报成模型能力不足。
当前没有 Agent 自动循环，只实现了供 Agent 决策的工具结果和显式选择入口。

当前适配能力（按精确 provider/model ID 校验，未知组合保守拒绝）：

| provider | model | inspect | generate |
|---|---|---|---|
| openai | gpt-6-astra | 支持 | 支持 Responses 原生 image_generation 工具 |
| openai | gpt-image-1.5 | 未接入 | 支持 Images API |
| bailian | qwen3.7-plus | 支持 | 未接入 |
| bailian | glm-5 | 未接入 | 未接入 |
| bailian | qwen-image-plus | 未接入 | 支持百炼原生同步生图 API |
| deepseek | deepseek-flash | 支持 | 未接入 |

“未接入”表示本项目当前适配边界，不是对厂商全部产品能力的断言。
OpenAI Responses 生图由所选聊天模型调用服务端的图片生成工具，
底层图片模型由该 API 默认配置决定；这不是将主模型回退为其他聊天模型。
能力表根据已查阅文档建立，不通过带费用的请求自动探测模型能力。

图片操作的输入与输出路径必须在工作区内：
- inspect：传 path 和 prompt；接受 PNG/JPEG/WebP，仅进行文件头格式识别，不完整解码校验。
- generate：传 output_path 和 prompt；输出必须为新的 .png，父目录需已存在。
- 图片读写上限默认 20 MiB，由 ToolContext.max_image_bytes 配置。
- 识别文字受 max_output_chars 限制；结果不包含原始图片字节、Base64 或签名下载 URL。
- 生成使用独立的单次请求，禁用自动重试，避免请求状态不确定时重复计费。
- 百炼返回的图片只从 HTTPS 阿里云对象存储域名下载，不附带 API Key、不跟随重定向。
- 取消本地请求不保证服务端停止生成或取消计费；已有输出文件不会被覆盖。
- 成功结果返回相对路径、MIME 类型和文件字节数；CLI 不自动渲染图片。

CLI 识别示例（已有 DASHSCOPE_API_KEY）：
```powershell
'{"action":"inspect","path":"photo.png","prompt":"描述这张图片"}' | Set-Content -Encoding UTF8 image-args.json
python tools_cli.py --provider bailian --model qwen3.7-plus --tool image --arguments-file image-args.json
```

CLI 显式选择已配置生图模型：
```powershell
'{"action":"generate","prompt":"一只在窗边晒太阳的猫","output_path":"cat.png","model":"bailian:qwen-image-plus"}' | Set-Content -Encoding UTF8 image-args.json
python tools_cli.py --provider bailian --model qwen3.7-plus --image-model bailian:qwen-image-plus --tool image --arguments-file image-args.json
```
--image-model 可重复传入 PROVIDER:MODEL，CLI 使用该完整字符串作为可选模型名称。
每个提供商使用对应环境变量；工具不接受模型传入的 API Key 或任意服务地址。
上述请求调用真实服务并产生用量；本次验证仅使用模拟 HTTP，没有实际发送图片或生成图片。

参考：[OpenAI 图片生成](https://developers.openai.com/api/docs/guides/image-generation)、
[百炼视觉理解](https://help.aliyun.com/zh/model-studio/vision)、
[百炼 Qwen-Image](https://help.aliyun.com/zh/model-studio/qwen-image-api)、
[DeepSeek 视觉](https://api-docs.deepseek.com/guides/vision/)。
