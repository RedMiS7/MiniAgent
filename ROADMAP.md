# MiniAgent 路线图

目标：构建一个用于学习的最小单 Agent 项目，保持 Model、Tools 可扩展，Agent Harness 可复用。

本项目使用以下术语划分职责：

| 术语 | 职责 |
|---|---|
| Model Adapter | 将提供商协议转换为统一的模型接口。 |
| Tools | 提供可注册的操作能力，由 Tool Executor 校验并执行。 |
| Agent Loop | 决定下一步是调用模型、执行工具还是返回回答。 |
| Agent Runtime | 管理一次 Run 的生命周期、执行限制、取消和资源清理。 |
| Agent Harness | 组合 Model、Tools、Agent Loop 和 Runtime，提供可复用的 Agent 运行入口；后续接入 Session。 |
| Session | 保存多轮对话的 Messages 和模型续接状态。 |
| Events | 向调用方报告 Agent、LLM 和 Tool 的执行过程。 |

Harness 用于让不同任务通过配置和依赖组合运行，无需重复编写模型接入、工具接入与运行控制代码。
这些是职责边界，不要求每个术语都对应一个独立类或框架。

按 P0 → P7 推进，P4 的 CLI 闭环已实现，P3 整体完成状态待单独复查；P6 完成后即可交付最小学习版，P8 按需探索。
已勾选表示已有实现，未勾选表示待完成；本次文档调整不改变完成状态。
各阶段默认使用离线模拟测试验收，真实接口验证单独记录。

## P0：项目基础

- [x] 明确架构与模块职责。
- [x] 建立开发规范和使用文档。
- [x] 建立基础测试能力。

**验收标准**

- 能按 README 完成环境准备并运行离线测试。
- 文档明确模型、工具和 Agent 的职责及扩展边界。

## P1：Model Adapters

- [x] 接入不同提供商的模型。
- [x] 支持文本生成、流式输出和生成选项。
- [x] 支持工具调用与上下文续接。

**验收标准**

- 上层通过同一接口调用不同模型，无需处理厂商 SDK 或协议差异。
- 离线测试覆盖文本、流式、工具往返、续接和错误处理；不支持的能力明确报错。

## P2：Tools 与 Extensions

- [x] 支持工具注册、参数校验和执行。
- [x] 提供文件、命令和图片工具。
- [x] 提供访问限制和执行状态反馈。

**验收标准**

- 新工具能通过 extension 注册，无需修改工具执行器。
- 各类工具的成功、失败路径通过离线测试；非法参数、越界文件访问和未授权命令被拒绝。

## P3：Core Types 与 Events

- [ ] 统一单 Agent 内部的消息、请求、响应和工具结果结构。
- [x] 定义 AgentEvent、LLMEvent、ToolEvent 的职责、字段和关联方式（P3.2，见 docs/p3-2-event-contract.md）。
- [ ] 明确 Agent Loop、Model、Tools 与 Runtime 之间的数据传递约定。
- [ ] 复用现有类型，补齐必要的数据校验与转换。

**验收标准**

- 用模拟数据串联一次模型请求、工具调用、结果回传和 Agent 结果，各组件通过约定类型交换数据，无需解析日志或依赖厂商对象。
- AgentEvent 表达运行进度与结束，LLMEvent 表达模型增量与完成，ToolEvent 表达工具执行状态；事件能正确关联到对应运行和调用。
- 消息、调用 ID、工具结果与模型续接状态在传递中保持一致，非法数据被明确拒绝，已有模型和工具测试继续通过。

本阶段验收数据契约；实际执行循环在 P4 实现。运行关联由上层回调或事件流负责，具体汇集机制在 P6 实现，不要求底层事件携带运行 ID。

P3.1 进展：模型核心类型与工具结果已迁移到 Pydantic，并通过迁移验收；事件现已统一为独立 Pydantic 类型和带 type 判别字段的联合，当前 80 项离线测试通过。Agent 最终结果与组件交接约定仍待完成，见 docs/p3-1-pydantic-core-types.md。

## P4：Agent Loop

- [x] 提供流式消费
- [x] Loop每一步的提示信息
- [x] tool 调用 Loop
- [x] Agent event

**验收标准**

- 假模型能完成“列目录 → 读文件 → 总结”，至少经过两轮工具往返；纯文本任务能直接结束。
- Tool Results 与 Tool Calls 正确配对，下一轮模型请求包含完整 Messages 和续接状态。
- 完整工具调用才交给 Tool Executor；工具失败能回传，模型异常和取消能向调用方传递，非正常结束不被当作成功。

P4 已实现 AgentLoop 与 agent_cli.py，按用户要求提前提供 CLI 单任务输入与流式过程展示；含最小轮数上限，不代表 P5/P6 已完成。设计与验证见 docs/p4-agent-loop.md。

## P5：Agent Runtime

- [ ] 实现 Runtime，管理 Run 状态、步数限制、取消和资源清理。
- [ ] 实现 Runtime 层的审批等待、中断、重试和错误恢复控制；具体策略由上层提供。

**验收标准**

- 独立 Run 的状态、结果和结束原因准确，成功历史可交付，失败或取消不会被当作成功。
- 达到步数上限、取消或中断后不再启动新调用，Run 自有资源能够清理，外部资源不会被误关。
- 模拟测试覆盖批准、拒绝、中断以及可重试和不可重试失败；未批准的调用不可执行，已完成的副作用不会被 Runtime 自动重放。

## P6：Agent Harness

- [ ] 组装 Harness，注入 Model、Tools 和 Agent Loop，提供统一运行入口。
- [ ] 在 Harness 中配置工具授权并汇集 Events。
- [ ] 用不同任务验证 Harness 复用，编写使用示例。

**验收标准**

- 同一 Harness 可运行文件摘要和 echo 文本处理任务，仅调整指令和 Tools，独立 Run 不串用状态。
- Harness 创建的共享资源按所有权关闭，外部注入资源不会被误关。
- 未授权 Tools 不可执行，Events 能关联到对应 Run，事件回调失败不会导致重复执行。
- 至少两种现有 Model Adapters 通过模拟工具往返测试；替换 Model 或注册新 Tool 无需修改 Agent Loop 和 Runtime。

## P7：Session 与 Streaming

CLI 多轮子任务已完成：agent_cli.py 在单进程内保留成功历史及续接状态，支持追问和退出；失败结束会话。完整 Session/Harness 接入仍待完成，见 docs/cli-multiturn-conversation.md。

- [ ] 用 Session 管理内存中的多轮 Messages 与续接状态。
- [ ] 将 Session 接入 Harness，支持继续对话和新建会话。
- [ ] 接入 Streaming，在 CLI 展示输出与 Events。

**验收标准**

- 同一 Session 连续两轮对话能使用历史，不同 Session 的状态相互隔离。
- Streaming 能逐段展示输出，仅在完整模型响应后执行 Tool Calls；中断通过 Runtime 清理资源。
- 含未完成 Tool Calls 的 Session 不会直接续接，CLI 明确提示能否继续；默认日志不暴露敏感内容。

## P8：按需探索

- [ ] Session Persistence：会话保存与恢复。
- [ ] Context Compaction 与 Memory：上下文压缩与长期记忆。
- [ ] Retrieval 与 MCP Tools：检索和外部服务接入。
- [ ] Planning 与 Evaluation：任务规划和结果检查。
- [ ] Human-in-the-loop：执行中的逐次操作授权。

**验收标准**

- 每次只选择一个有具体需求的方向，先定义失败案例和预期结果。
- 扩展后原失败案例通过，已有核心测试无回归，并记录收益和限制。
- 本阶段按选定能力逐项验收，不要求全部实现。

## 推进约定

- 每次只完成一个可独立验证的小任务，完成后在 docs/ 记录方案理由和验证结果。
- 复用现有类型与组件，模型差异留在适配层，不自动切换提供商或扩大工具授权。
- 工具访问限制不等于操作系统沙箱；步数限制不等于时间或费用上限。
- 阶段验收通过后更新进度；离线测试不代表真实服务可用或模型任务效果已经验证。
