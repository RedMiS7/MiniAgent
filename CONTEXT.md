# MiniAgent

可复用的单 Agent 系统。以下术语区分一次任务执行、跨轮对话和可重复使用的运行入口。

## Language

**Run**:
一次有明确开始与结束的 Agent 执行；结束后不会因后续任务而重新变为运行中。
_Avoid_: Session、会话

**Agent Runtime**:
一次 Run 的生命周期管理者，负责运行状态、执行限制、取消和资源清理。
_Avoid_: 模型适配器、会话管理器

**Agent Harness**:
组合模型、工具、循环和 Runtime 的可复用运行入口；复用入口本身不表示复用上次任务的对话历史。
_Avoid_: 单次 Run、隐式会话

**Session**:
跨多次 Run 保留对话消息与模型续接状态的会话；共享会话需要显式指定。
_Avoid_: Run、单次执行
