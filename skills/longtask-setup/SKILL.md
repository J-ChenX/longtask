---
name: longtask-setup
description: 为需 longtask 的全新或空项目建立架构与验收包；已有实现用 retrofit。
license: MIT
compatibility: Designed for Codex. Requires Python 3.14+; install the complete longtask plugin.
metadata:
  version: "4.0.0"
---

# Longtask 初始化

直接激活时简述 longtask-setup；从主入口转入无需重复宣告。复用[主技能](../../SKILL.md)，确认项目全新或为空；已有实现转 retrofit。

## 预期结果

产出围绕业务目标、可供后续实现消费的设计文档和独立验收工作包，保留可恢复入口。

检查点与工作包保存按[初始化与交接](../../references/初始化与交接.md)操作；设计产物按[新项目输出合同](../../文档架构.md#新项目输出合同)定位，只加载当前缺口涉及的章节。规划是否足以交付见[规划充分性与出口](../../references/初始化与交接.md#规划充分性与出口)。常规操作不输出完整 schema 或实现源码。

## 授权与出口

- **仅规划：** 达到本次规划出口后保存新鲜交接并报告产物及未决项。保留 planned 包和未完成检查点，不实施应用、不伪造阶段或验收，不运行 `finish`。
- **整体实施已授权：** 继续实现、验证、必要修复和审查，不再询问“是否开始”。选择交付切片时查[分段目标](../../references/任务推进.md#分段目标)，全部验收满足后按[完成合同](../../SKILL.md#变更与完成)收尾。

后续目标或结构变化通过状态工具协调，不覆盖旧任务。
