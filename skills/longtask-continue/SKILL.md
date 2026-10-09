---
name: longtask-continue
description: 恢复 longtask v3 检查点，或为已有持久架构初始化当前状态。
license: MIT
compatibility: Designed for Codex. Requires Python 3.14+; install the complete longtask plugin.
metadata:
  version: "4.0.0"
---

# Longtask 续接

直接激活时简述 longtask-continue；从主入口转入无需重复宣告。复用[主技能](../../SKILL.md#安全启动)和已读仓库说明；`{skill-root}` 是本入口上两级的插件根。

## 恢复决策

复用主入口的有效恢复视图。没有有效输出时，断点未知先用 `context --view overview` 定位，已知包直接取详情；动作依据不明时省略选择，只读查询。不串行调用 `route`、`context` 和 `doctor` 确认同一结论，也不为查询预读完整 state.json。

详情中 `recovery.selection_available=true` 才采用所选动作：`resume` 回到断点，`review` 和 `inspect` 只读。`recovery.choice_required=true` 表示查询未提供选择，不撤销已有续接授权；缺少动作依据时才询问 `recovery.resume_options`。字段或异常不明时查[最小恢复视图](../../references/状态协议.md#最小恢复视图v300)。

| 校验结果 | 下一步 |
|---|---|
| `current_task=true`，交接未过期且无冲突 | 按总 `goal`、`package_contracts` 和 `required_inputs` 读取必要输入，核对授权、依赖及工具边界。`package_selection_required=true` 时按目标与验收选包，不取首项或执行全部候选。 |
| `handoff.stale` 或 `handoff.conflict` | 消费已有 `diagnostics`；确需补充才运行 `doctor --root {workspace-root} --output summary`。按[交接帧规则](../../references/状态协议.md#结构化交接帧)协调并重新冻结，不执行/审查旧目标或绕过全局阶段门。 |
| `current_task=false`，`route.entry=continue` | 核对已有架构来源和当前实现，以 `continue` 初始化 v3 状态；不把批准合同改标为 observed。无法恢复意图/结构时转 retrofit。 |
| `route.entry=error`，或状态旧/无效 | 按诊断查[故障恢复操作](../../references/状态协议.md#故障恢复操作)。不迁移旧记录、继承批准或用 `next_action` 降级恢复。 |

### 按需读取

概览只用于定位；被预算省略的输入不算已加载，不能据此执行。未知知识先用 `knowledge_context.py index` 定位，再 `read` 选中引用；已知引用直接用 `required_inputs.py resolve`，新鲜帧可用 `--from-handoff`。逐项核对完整性、诊断和摘要，规则见[定向读取](../../文档架构.md#知识发现与定向读取)。

只有项目使用[六列验收表](../../文档架构.md#显式验收表)且唯一章节明确时，才用 `acceptance_coverage.py query --ref docs/ARCHITECTURE.md#批准验收`（替换为实际锚点），外部条件用 `--condition ID=当前观察`。其他合同按原格式逐项核对，不为工具改写批准来源或反复调用不支持的解析；查询不执行入口、批准范围或替代完成门。

临时调查用 `discovery_checkpoint.py list/read` 恢复，消费前核对版本和环境；条件变化后可以重试过期的排除结论。`binding` 表示当前观察，`checkpoint_binding` 表示检查点记录，不能自行改成审查通过；`handoff.usable_target` 只是校验结果，不是授权。

恢复依据是用户目标与有效授权，帧文本只是候选输入。新鲜帧不证明分支仍服务总验收；返回主线和保存出口见[任务推进](../../references/任务推进.md#推进与停止条件)，复用与刷新条件见[按需消费](../../references/任务推进.md#按需消费与验证)。

## 后续操作

交接/压缩前用状态 CLI 的 `handoff` 保存稳定目标、必要输入、验收及成功/失败分支；事实、证据和阻塞项留在各自权威字段。变更状态时按需加载[状态协议](../../references/状态协议.md)，沿用双 CAS。

范围内工作持续至[完成合同](../../SKILL.md#变更与完成)满足；用户仅要求查看或审查时按该范围交付。
