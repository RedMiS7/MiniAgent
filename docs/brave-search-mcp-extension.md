# Brave Search MCP extension 接入

## 问题与范围

需要用一个真实 MCP 工具验证现有 extensions 是否容易扩展。用户明确要求先接入
MCP，再推进 Harness 工具策略。本次只提供 Brave 搜索的连接、注册和执行，不加审批框架。
这是 P8 中按实际需求提前选择的具体接入任务，服务于后续 P6 审批验证，不标记 P6 完成。

## 选择与边界

采用 Brave 官方服务的 brave_web_search，保留真实名称，不重命名为 web_serach。
使用固定版本 2.1.4 的 npm 服务和 Python MCP SDK 1.26.0 的 STDIO 客户端。
官方来源：
- https://github.com/modelcontextprotocol/servers#archived
- https://github.com/brave/brave-search-mcp-server
- https://github.com/modelcontextprotocol/python-sdk/tree/v1.26.0

连接配置集中在 bootstrap.connect_brave_search；使用现有 Node/npx 拉起服务，
仅传入 Brave 凭证及 SDK 默认基础环境，使用临时 cwd 避免服务自动读项目 .env。
连接、初始化、发现、使用和关闭保持在同一个异步作用域中，不能跨任务进出 SDK 作用域。
连接方拥有 session 和服务进程；extension 只借用 session，不关闭共享资源。
MCP 请求读取超时为 60 秒；不自动重试搜索，也不自动切换服务。

## 验证扩展能力

新增 extensions/brave_search.py，通过远端 tools/list 发现 schema，处理分页并只注册
brave_web_search。现有 Registry 继续校验 schema、重复名称和调用参数。
工具通过 session.call_tool 发起 MCP 请求，文本及 structuredContent 转换为 ToolResult。
isError 不会被当作成功；非文本内容明确拒绝，传输异常交给现有执行器转换，取消继续传播。
无需修改 ToolRegistry、ToolExecutor、AgentLoop、AgentRuntime 或 AgentHarness。

单独 brave_mcp_cli.py 默认只发现工具，--query 是用户显式调用测试。不接入模型自动
调用路径，避免在尚未完成审批阶段把它默认开放给现有 CLI。后续 Harness 需要在发送
远端调用前落实人工审批；本次不能宣称此能力已完成。

## 验证与限制

13 项新增离线测试覆盖 schema 注册、工具筛选、参数拒绝、文本/JSON、远端错误、
传输错误、非文本结果、取消、分页、缺失工具、重复注册、连接关闭和 CLI 调用边界。
假模型通过现有 Harness 完成搜索工具往返，检查调用 ID 和模型接收到的工具结果。
使用 .venv/Scripts/python.exe -m unittest discover -s tests -q：全部 192 项通过。
CLI --help 检查通过。依赖通过 uv 安装到项目虚拟环境，未安装全局包。

已启动官方 2.1.4 服务，使用占位凭证完成真实 STDIO initialize 与 tools/list，成功发现
brave_web_search 和必填 query，连接作用域正常关闭。未发送任何真实搜索请求。
真实搜索、有效账号及配额仍需用户配置 BRAVE_API_KEY 后验证，不以协议握手代替搜索验收。
本次只支持 Brave 搜索的文本/JSON，不扩展其他 MCP 工具、资源、提示模板或 HTTP 传输。
