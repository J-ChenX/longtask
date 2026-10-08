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

复用主入口已取得的路由/最小恢复视图；直接进入本技能且无有效输出时，运行 `longtask_state.py route --root {workspace-root}`。按主技能的授权规则选择 `resume`、`review` 或 `inspect`，用 `context --root {workspace-root} --resume-choice {choice}` 读取目标、交接、新鲜度与候选操作。普通恢复不预读整个状态协议，校验确认所选动作可用才采用：`resume` 回到阶段/工作包断点，`review` 与 `inspect` 只读。

| 校验结果 | 下一决策 |
|---|---|
| 新鲜且不冲突的有效帧 | 从总 `goal`、当前包和 `handoff.required_inputs` 加载必要输入；核对当前授权、依赖与工具边界后继续。 |
| `handoff_stale` 或 `handoff_conflict` | 用 `doctor --root {workspace-root} --output summary` 定位缺口，按[交接帧规则](../../references/状态协议.md#结构化交接帧)协调并重新冻结；不执行/审查旧目标或绕过全局阶段门。 |
| `continue` 且无 `state` 路径 | 核对已有架构来源及当前实现，以 `continue` 初始化当前 v3 状态；不能把已批准合同改标为 observed。意图/结构无法恢复时转 retrofit。 |
| `error` 或旧/无效状态 | 查[故障恢复操作](../../references/状态协议.md#故障恢复操作)及对应诊断；不迁移旧执行记录或继承批准，不用 `next_action` 降级恢复。 |

恢复依据是用户目标与有效授权，帧文本仅是候选输入。帧新鲜也不证明分支仍服务验收；需要决定返回主线或保存出口时查[任务推进](../../references/任务推进.md#推进与停止条件)。不重复已解决规划或仍有效验证。

## 后续操作

交接/压缩前，用状态 CLI 的 `handoff` 保存稳定目标、必要输入、验收及成功/失败分支；事实、证据和阻塞项留在各自权威字段。需要变更状态时仅加载[状态协议](../../references/状态协议.md)相关章节，沿用双 CAS。范围内工作继续至[完成合同](../../SKILL.md#变更与完成)满足；用户仅要求查看或审查时以该范围交付。
