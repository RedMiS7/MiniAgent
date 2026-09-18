# Runtime 与 Harness 阶段拆分

## 为什么拆分

原 P5 同时包含单次 Run 的生命周期控制和可复用 Agent 的组件装配，范围过大，难以按项目约定作为一个可独立验收的小阶段推进。
本次只调整路线图，不实现 Runtime 或 Harness。

## 阶段边界

P5 只实现 Agent Runtime，负责 Run 状态、结果、执行限制、取消、资源清理，以及审批等待、中断、重试和错误恢复所需的运行控制与事实状态。具体审批和重试策略由上层提供，Runtime 不自行判断工具风险，也不自动重放已完成的副作用。

P6 实现 Agent Harness，负责注入 Model、Tools、Agent Loop 和 Runtime，配置工具授权，汇集关联到 Run 的 Events，并管理 Harness 持有的共享资源。

原 P6 Session 与 Streaming 顺延为 P7，原 P7 按需探索顺延为 P8。最小学习版的交付点相应从原 P5 调整为 P6，即 Runtime 和 Harness 均完成后交付。

## 验证与限制

已检查 P0 至 P8 的阶段顺序、P3 的事件汇集引用，以及 Runtime 与 Harness 的任务和验收标准。此次没有修改完成状态、功能代码或测试；历史任务文档保留当时的阶段编号，不作为当前路线图状态来源。