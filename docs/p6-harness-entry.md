# P6 Harness 最小运行入口

## 问题与范围

现有 CLI 各自组织 Model、Tools、Loop 和 Runtime，缺少可连续执行独立任务的共用入口。
本次只完成 P6 的组件组合、顺序复用和模型资源所有权，不将整个 P6 作为一次任务。

## 方案与取舍

AgentHarness 接收统一模型和 ToolExecutor，每次运行构建新的 Loop 与 Runtime。
运行作用域直接交付 Runtime，事件、取消和结果使用已有接口，不重新定义 RunState。
Harness 不保存上次对话；需要延续历史时由调用方显式传入，Session 留在 P7。

首版只允许顺序复用，活动运行作用域未退出前拒绝新的运行和关闭操作，即使终态已产生。
运行作用域退出后才释放占用，确保取消和清理等待期间不能复用共享依赖。
借助 Runtime 的作用域收尾，普通失败、取消或非法运行参数不会永久占用 Harness。

初始化仍集中于 bootstrap。create_harness 使用已有 extension 注册方式创建工具和模型，
并明确将模型所有权交给 Harness。直接构造默认借用依赖；owns_model=True 表示显式移交模型。
每次 Run 结束不关闭共享模型，Harness 关闭时才关闭自有模型。工具执行器仍是借用依赖。
关闭前先退出活动 Run；关闭一旦开始不再允许新运行，也不自动重复关闭失败的模型。
如果作用域主体与模型关闭同时失败，保留主体异常并通过异常链附带关闭故障。

## 改动与使用

新增 miniagent/agent/harness.py，公开 AgentHarness，新增 bootstrap.create_harness。
README 提供完整作用域示例。未更改 Loop、Runtime 或现有 CLI。

## 验证与限制

新增 10 项离线测试覆盖：独立消息与事件及选项传递、活动运行冲突、关闭冲突、
失败后复用、提前退出取消、清理完成前拒绝复用、借用模型不误关、自有模型只关闭一次、
关闭故障保留主体异常、非法参数后复用，以及 bootstrap 的工具组装与模型所有权。
运行 python -m unittest discover -s tests -q：全部 179 项离线测试通过。未调用真实模型服务。

本次不实现工具审批、事件展示回调、重试或恢复，不进行 Session 管理。
运行关联目前通过各自 Runtime 的独立事件流表达；多来源事件汇集留待后续子任务。
路线图仅勾选 P6 的组装入口，不能据此宣称 P6 全部验收通过。
