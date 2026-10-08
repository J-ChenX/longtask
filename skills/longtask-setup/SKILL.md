---
name: longtask-setup
description: 为需 longtask 的全新或空项目保存目标、建立架构与可验收工作包；已有实现用 retrofit。
license: MIT
compatibility: Designed for Codex. Requires Python 3.14+; install the complete longtask plugin.
metadata:
  version: "3.0.0"
---

# Longtask 初始化

激活时说明正在使用 longtask-setup。复用[主技能](../../SKILL.md)，确认是全新/空项目；已有实现转 retrofit。

## 预期结果

建立可恢复的目标、当前最小架构和可独立验收工作包，保留当前授权边界。只有会改变范围或关键合同的缺失信息才询问。

按[初始化与交接](../../references/初始化与交接.md)先 `begin` 建立检查点，再生成架构/模块文档。文档决策加载[文档架构](../../文档架构.md)相关章节；包与交接使用 `save`，在有价值的恢复边界保存。常规命令无需输出全 schema 或实现源码。

## 授权与出口

用户仅授权规划：形成可续接边界、合同、依赖和验收，核对目标后保存新鲜交接并报告；保留 planned 包与未完成检查点，不实施应用、不伪造阶段/验收、不运行 `finish`。

用户已授权整体实施：无需再问“是否开始”，按总目标的未满足验收继续实现、验证、必要修复与审查。需要选择交付切片时查[分段目标](../../references/任务推进.md#分段目标)，结束时遵循[完成合同](../../SKILL.md#变更与完成)。后续目标或结构变化使用状态工具协调，不覆盖旧任务。
