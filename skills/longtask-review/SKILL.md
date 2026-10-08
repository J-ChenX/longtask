---
name: longtask-review
description: 对 longtask 项目的指定实现或文档做独立、版本绑定审查；默认只读。
license: MIT
compatibility: Designed for Codex. Requires Python 3.14+; install the complete longtask plugin.
metadata:
  version: "3.0.0"
---

# Longtask 审查

激活时说明正在使用 longtask-review。复用[主技能](../../SKILL.md)，读取[专家审查协议](../../专家审查协议.md)中本次风险、独立性与结论所需章节。

## 冻结被审对象

有检查点时，校验 v3 状态及 review 帧的摘要、HEAD、证据代际，以 `target`、`required_inputs` 和验收定位对象。无检查点时可对用户指定范围现场冻结摘要，不为只读审查制造任务档案。目标不明确时说明缺口，不猜测“上一步”或伪造批准。

按风险选择独立审查者，仅提供最小权威输入；子智能体使用遵循宿主能力及用户授权。运行与主张相关的确定性检查，按[审查收敛](../../专家审查协议.md#审查收敛)综合根因、分歧及覆盖缺口，向用户交付可核验结论。状态更新获授权时只写临时结构化证据。

## 结论与修复边界

仅审查请求保持代码/文档只读。失败 review evidence 生成指向 `check_id` 与 finding ID 的 `remediate` 交接；修复同时或后续获授权时，保留原始发现、修复版本及独立复验证据，在新摘要生成新 review 交接。

全部阻断项独立关闭且覆盖完整、审查摘要等于当前摘要时，才能批准。必要结论按[知识合并与清理](../../文档架构.md#知识合并与清理)进入当前项目知识，不形成长期已修复问题清单或审查日志。可选建议不延迟符合当前合同的交付。
