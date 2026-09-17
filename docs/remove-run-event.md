# 移除 RunEvent 事件包装

## 原因与取舍

用户要求先移除 RunEvent。当前还没有 Agent Harness 或多运行事件汇集需求，
提前固定运行 ID 包装会增加不必要的类型和依赖。
底层模型与工具只需表达自身事件，运行归属由上层回调或事件流管理。

## 改动

删除 agent/events.py 中的 RunEvent 及其专用导入，移除 agent 包导出。
保留 AgentEvent、LLMEvent 和 ToolEvent 的定义与校验。
测试直接使用三类事件，继续检查工具调用 ID、参数索引、工具结果与续接状态。
移除两个包装专用测试，更新事件契约文档与路线图说明。

## 验证与阶段关系

python -m unittest discover -s tests -q：79 项离线测试通过。
运行关联仍是 P3 的上层职责约定；具体汇集机制及运行隔离验证留到 P5。
本次没有增加替代包装、事件总线或执行循环，也未验证真实服务。
