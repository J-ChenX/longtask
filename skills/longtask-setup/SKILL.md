---
name: longtask-setup
description: 为需 longtask 的全新或空项目建立架构与验收包；已有实现用 retrofit。
license: MIT
compatibility: Designed for Codex. Requires Python 3.14+; install the complete longtask plugin.
metadata:
  version: "3.0.0"
---

# Longtask 初始化

直接激活时简述 longtask-setup；从主入口转入无需重复宣告。复用[主技能](../../SKILL.md)，确认项目全新或为空；已有实现转 retrofit。

## 预期结果

建立可恢复的目标、最小架构和可独立验收工作包。保留当前授权边界，只有缺失信息会改变范围或关键合同时才询问。

按[初始化与交接](../../references/初始化与交接.md)先用 `begin` 建立检查点，再编写架构/模块文档。文档设计按需查[文档架构](../../文档架构.md)，工作包与交接用 `save` 在有价值的恢复边界保存。常规操作不输出完整 schema 或实现源码。

## 授权与出口

- **仅规划：** 明确合同、依赖和验收，核对目标后保存新鲜交接并报告。保留 planned 包和未完成检查点，不实施应用、不伪造阶段或验收，不运行 `finish`。
- **整体实施已授权：** 继续实现、验证、必要修复和审查，不再询问“是否开始”。选择交付切片时查[分段目标](../../references/任务推进.md#分段目标)，全部验收满足后按[完成合同](../../SKILL.md#变更与完成)收尾。

后续目标或结构变化通过状态工具协调，不覆盖旧任务。
