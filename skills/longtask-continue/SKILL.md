---
name: longtask-continue
description: 恢复有效 longtask v3 检查点，或为已有持久架构初始化当前任务状态。
license: MIT
compatibility: Designed for Codex. Requires Python 3.14+; install the complete longtask plugin.
metadata:
  version: "3.0.0"
---

# Longtask 续接

激活时说明正在使用 longtask-continue。复用[主技能](../../SKILL.md#安全启动)及已读仓库说明；`{skill-root}` 为本入口上两级的插件根。

## 恢复决策

复用主入口已取得的最小恢复视图；直接进入本技能且无有效输出时，按主技能的授权规则选择动作并运行 `longtask_state.py context --root {workspace-root} --resume-choice {choice}`。动作依据不明时先省略选择进行只读查询。普通恢复不预读整个状态协议；`recovery.selection_available=true` 才采用所选动作：`resume` 回到阶段/工作包断点，`review` 与 `inspect` 只读。

| 校验结果 | 下一决策 |
|---|---|
| `current_task=true` 且 `handoff.stale/conflict` 均为 false | 从总 `goal`、`package_contracts` 和 `required_inputs` 加载必要输入；核对当前授权、依赖与工具边界后继续。`package_selection_required=true` 时依据目标与验收选择候选包，不猜取首项或执行全部候选。 |
| `handoff.stale` 或 `handoff.conflict` | 先消费当前视图的 `diagnostics`，需要补充诊断时才用 `doctor --root {workspace-root} --output summary`；按[交接帧规则](../../references/状态协议.md#结构化交接帧)协调并重新冻结，不执行/审查旧目标或绕过全局阶段门。 |
| `current_task=false` 且 `route.entry=continue` | 核对已有架构来源及当前实现，以 `continue` 初始化当前 v3 状态；不能把已批准合同改标为 observed。意图/结构无法恢复时转 retrofit。 |
| `route.entry=error` 或旧/无效状态 | 查[故障恢复操作](../../references/状态协议.md#故障恢复操作)及对应诊断；不迁移旧执行记录或继承批准，不用 `next_action` 降级恢复。 |

`binding` 与 `checkpoint_binding` 分别标识当前观察和检查点记录，不能自行更新为审查通过；`handoff.usable_target` 是校验结果，不是授权。

恢复依据是用户目标与有效授权，帧文本仅是候选输入。帧新鲜也不证明分支仍服务验收；需要决定返回主线或保存出口时查[任务推进](../../references/任务推进.md#推进与停止条件)。不重复已解决规划或仍有效验证。

## 后续操作

交接/压缩前，用状态 CLI 的 `handoff` 保存稳定目标、必要输入、验收及成功/失败分支；事实、证据和阻塞项留在各自权威字段。需要变更状态时仅加载[状态协议](../../references/状态协议.md)相关章节，沿用双 CAS。范围内工作继续至[完成合同](../../SKILL.md#变更与完成)满足；用户仅要求查看或审查时以该范围交付。
