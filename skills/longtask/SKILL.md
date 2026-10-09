---
name: longtask
description: 编排需跨上下文恢复或协调独立验收包的编码任务；小改动、解释和普通审查不启用。
license: MIT
compatibility: Designed for Codex. Requires Python 3.14+; install the complete longtask plugin.
metadata:
  version: 4.1.0
---

# Longtask 入口

这是完整插件的发现入口。读取[根技能](../../SKILL.md)选择工作流并遵循共享规则；已加载且仍有效时直接复用。相对路径以插件根为准，不另建工作流或重复查询状态。
