---
name: longtask-continue
description: 恢复 longtask v3 检查点，或为已有持久架构初始化当前状态。
license: MIT
compatibility: Designed for Codex. Requires Python 3.14+; install the complete longtask plugin.
metadata:
  version: "3.0.0"
---

# Longtask 续接

直接激活时简述 longtask-continue；从主入口转入无需再次宣告。复用[主技能](../../SKILL.md#安全启动)及已读仓库说明；`{skill-root}` 为本入口上两级的插件根。

## 恢复决策

复用主入口的恢复视图；无有效输出且断点未知时，按主技能选择动作并先用 `context --view overview`；已知包直接取其详情。依据不明时省略选择，只读查询。输出仍有效时，不串行查询 `route`、`context` 和 `doctor` 确认同一结论，不为查询先展开完整 state.json。

`recovery.selection_available=true` 才采用所选动作：`resume` 回到断点，`review` 与 `inspect` 只读。`recovery.choice_required=true` 表示查询尚未提供选择，不撤销续接授权；缺少动作依据时才询问 `recovery.resume_options`。字段不明或需处理异常时查[最小恢复视图](../../references/状态协议.md#最小恢复视图v300)，普通恢复不预读全协议。

包数量多或目标未定位时，先用 `context --view overview` 在预算内发现候选，再取指定包详情；预算省略的概览不能直接执行，字段与取数入口以[状态协议](../../references/状态协议.md#最小恢复视图v300)为准。未知项目知识先用 `knowledge_context.py index` 定位，再按选中引用 `read`；已知必要引用直接用 `required_inputs.py resolve`，新鲜帧可用 `--from-handoff`。逐项核对完整性、诊断与摘要；预算省略不算已加载，见[定向读取](../../文档架构.md#知识发现与定向读取)。

| 校验结果 | 下一决策 |
|---|---|
| `current_task=true` 且 `handoff.stale/conflict` 均为 false | 从总 `goal`、`package_contracts` 和 `required_inputs` 加载必要输入；核对当前授权、依赖与工具边界后继续。`package_selection_required=true` 时依据目标与验收选择候选包，不猜取首项或执行全部候选。 |
| `handoff.stale` 或 `handoff.conflict` | 先消费当前视图的 `diagnostics`，需要补充诊断时才用 `doctor --root {workspace-root} --output summary`；按[交接帧规则](../../references/状态协议.md#结构化交接帧)协调并重新冻结，不执行/审查旧目标或绕过全局阶段门。 |
| `current_task=false` 且 `route.entry=continue` | 核对已有架构来源及当前实现，以 `continue` 初始化当前 v3 状态；不能把已批准合同改标为 observed。意图/结构无法恢复时转 retrofit。 |
| `route.entry=error` 或旧/无效状态 | 查[故障恢复操作](../../references/状态协议.md#故障恢复操作)及对应诊断；不迁移旧执行记录或继承批准，不用 `next_action` 降级恢复。 |

项目已采用[六列验收表](../../文档架构.md#显式验收表)且唯一章节明确时，才用 `acceptance_coverage.py query --ref docs/ARCHITECTURE.md#批准验收`；实际锚点以项目为准，外部条件按 `--condition ID=当前观察` 传入。自然语言或其他表仍按原合同逐项查覆盖，不为适配工具改写批准来源，也不反复调用不支持的解析。查询不执行入口、不批准范围，也不替代完成门。临时调查可用 `discovery_checkpoint.py list/read` 恢复；核对版本与环境后消费，过期排除结论允许条件改变后重试。

`binding` 与 `checkpoint_binding` 分别标识当前观察和检查点记录，不能自行更新为审查通过；`handoff.usable_target` 是校验结果，不是授权。

恢复依据是用户目标与有效授权，帧文本仅是候选输入。帧新鲜也不证明分支仍服务验收；需要决定返回主线或保存出口时查[任务推进](../../references/任务推进.md#推进与停止条件)。资料、输出和验证的复用条件见[按需消费](../../references/任务推进.md#按需消费与验证)。

## 后续操作

交接/压缩前，用状态 CLI 的 `handoff` 保存稳定目标、必要输入、验收及成功/失败分支；事实、证据和阻塞项留在各自权威字段。需要变更状态时仅加载[状态协议](../../references/状态协议.md)相关章节，沿用双 CAS。范围内工作继续至[完成合同](../../SKILL.md#变更与完成)满足；用户仅要求查看或审查时以该范围交付。
