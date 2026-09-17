# P4：流式 Agent Loop 与交互 CLI

## 问题与范围

模型和工具已有统一接口，但用户仍需手动完成工具调用与结果回传。本次实现一个可独立验收的闭环：
终端输入一项任务，模型通过工具获取信息，再交付最终回答；用户可以看到模型轮次、工具开始、成功、失败及耗时。

按用户明确要求，将原计划 P6 的 CLI 流式展示提前接入。没有引入 Session、服务端用户认证或完整 Runtime。

## 方案与取舍

- AgentLoop 注入 LLM 和 ToolExecutor。每次 stream 调用独立保存消息、调用 ID 集合和工具事件队列。
- 直接复用 LLMRequest、ResponseCompleted、ToolResult.to_message；保留 response.message 即保留 continuation。
- 对外 LoopEvent 是 AgentEvent、LLMEvent、ToolEvent 的判别联合，不恢复 RunEvent，不新增 AgentResult。
- 模型流完整结束并获得唯一 ResponseCompleted 后才执行工具；重复调用 ID 在工具产生副作用前拒绝。
- 工具逐个执行。execute 新增关键字 on_event，每次调用单独绑定，不改写实例回调；原观察者仍收到事件，观察者异常独立隔离。
- 工具任务通过局部队列转发事件，让等待中的工具可以及时显示 ToolStarted。完成哨兵用于唤醒消费者；关闭或取消时取消并等待工具任务。
- 新增 agent_cli.py，保留 cli.py 的单次模型验证用途。CLI 负责组装依赖和关闭自己创建的模型客户端。
- AgentProgress 增加 step 和 phase：model、tools、completed。界面展示轮次和阶段，不默认展示完整参数或工具结果。
- 为使交互入口不会无限循环，提供 max_steps，默认 12 次模型调用。最后一轮如果仍要求工具，返回 step_limit，且不启动这些工具，避免没有模型轮次消费结果时继续产生副作用。这只是运行上限，不是完整 P5 Runtime。

## 数据与结束语义

完整消息历史包括 user、assistant 工具调用及对应 tool 结果。失败工具结果仍追加并回传模型。
ResponseCompleted 表示一轮模型结束；只有 finish_reason=stop 时发出 AgentCompleted。
length、refusal、content_filter 和 step_limit 发出 AgentFailed 并结束，CLI 返回 1。
模型异常发出 AgentFailed 后重新抛出，取消发出 AgentCancelled 后重新抛出。
消费者主动关闭生成器时不再向其发送终态；调用方提前结束迭代必须使用 aclosing。
Loop 关闭本次模型流及工具任务，不关闭注入的共享模型客户端。
流中模型文本可能是中间说明，调用方必须结合 Agent 终态判断任务是否完成。

## 使用

配置对应提供商的 API Key 环境变量后：

```powershell
python agent_cli.py --provider deepseek --model deepseek-flash --workspace .
```

在提示符输入：

```text
先列出当前目录，再读取 README.md，用中文总结这个项目的用途。
```

可用 --prompt 直接提供任务；--max-steps 调整轮数；--allow-commands 明确启用现有命令工具。
本入口一次执行一个任务，不保存跨次会话。文件工具沿用既有读写权限和工作目录限制。

## 验证

基线 80 项离线测试通过。新增假模型 + 真实 ToolExecutor 的测试，覆盖纯文本、两轮文件工具往返、
完整历史与续接、多调用配对、工具失败、非法参数、重复 ID、非成功结束、缺少最终响应、
模型异常、轮数上限、提前关闭、运行中取消、观察者异常隔离和并发工作区隔离。
CLI 测试模拟 input 和模型，检查步骤展示、回答不重复、退出码和模型客户端关闭。
python -m unittest discover -s tests -q：96 项离线测试全部通过（原有 80 项，新增 16 项）。
python agent_cli.py --help 和 git diff --check 通过；没有调用真实模型服务。

## 限制与后续

- 并发测试验证运行数据和事件通道隔离，不代表已经具备多租户权限系统。服务端应按用户创建 ToolContext 与执行器，不允许客户端任意指定工作目录。
- 同一 Session 的排队/冲突控制、Run 管理、资源所有权和授权汇集留给 P5/P6。
- 现有同步文件操作可能短暂阻塞事件循环，工具开始提示不承诺在同步文件操作前已经渲染；未在本任务重构文件 I/O。
- 默认展示工具名称和调用 ID，不展示敏感参数及工具输出。工具副作用不会因取消回滚。
- 真实模型效果、账号权限及网络可用性尚未验证。
