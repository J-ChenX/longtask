---
name: longtask-modify
description: 修改现有 longtask 的架构、模块图、状态协议、工作流或关键技术，同时保留证据和可恢复性。
metadata:
  version: "3.0.0"
---

# Longtask 架构修改

说明：“我正在使用 longtask-modify 处理这项架构级变更。”

1. 从[主技能](../../SKILL.md)和当前目标确定改动边界，复用已加载的仓库说明、检查点及架构；仅补读受影响的模块与决策。
2. 明确请求的变更、受影响依赖闭包、写集、批准、审查证据、迁移和回滚路径。
3. 没有检查点时，从当前项目文档与用户目标初始化 `modify` 检查点；有检查点时运行 `python3 {skill-root}/scripts/longtask_state.py modify --root {workspace-root} --expected-task-id {id} --expected-revision {n} --reason {reason} --package-id {affected-id}`；多个受影响包重复传入 `--package-id`，没有包时可省略。该命令记录 L4 事件、进入 modify/architecture，并冻结指定包及传递消费者，保留其他包。
4. 冻结包仅在当前检查点中保留供依赖校验，批准新架构后显式 supersede 并用新 ID 重建合同。仅当输入与写集独立时，不受影响包才可继续；不要用修改 goal 或重新 init 绕过模式切换。
5. 先更新唯一权威的架构/决策来源，再更新受影响模块文档；不要复制政策摘要。
6. 按主技能的[推进与停止条件](../../SKILL.md#推进与停止条件)实现受影响工作包，并提供明确验收证据。满足当前合同与回滚义务后，删除已取代路径。
7. 运行按风险选择的独立审查，只把批准重新绑定到集成后的版本。

收尾按[文档架构·知识合并与清理](../../文档架构.md)更新当前项目内容并清除检查点。

状态/并发规则见[状态协议](../../references/状态协议.md)，文档变更规则见[文档架构](../../文档架构.md)。
