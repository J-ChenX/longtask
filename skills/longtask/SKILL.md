---
name: longtask
description: 编排跨上下文或独立验收包的编码任务，增强会话连续性并核验交付；在授权内分工和接续，小改动、解释和普通审查不启用。
license: MIT
compatibility: Designed for Codex. Requires Python 3.14+; install the complete longtask plugin.
metadata:
  version: 4.6.2
---

# Longtask 入口

这是完整插件的发现入口。读取[根技能](../../SKILL.md)选择工作流并遵循共享规则；已加载且仍有效时直接复用。相对路径以插件根为准，不另建工作流或重复查询状态。

首次多包分工按[执行方式选择](../../references/平行任务协作.md#首次分工与执行方式)加载；跨会话片段按[会话预算与递进交接](../../references/任务推进.md#会话预算与递进交接)加载。其余路由与共享约束使用已读根技能，不重复展开。
