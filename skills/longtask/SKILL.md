---
name: longtask
description: 编排需跨上下文恢复或协调独立验收包的编码任务；小改动、解释和普通审查不启用。
license: MIT
compatibility: Designed for Codex. Requires Python 3.14+; install the complete longtask plugin.
metadata:
  version: 4.2.0
---

# Longtask 入口

这是完整插件的发现入口。读取[根技能](../../SKILL.md)选择工作流并遵循共享规则；已加载且仍有效时直接复用。相对路径以插件根为准，不另建工作流或重复查询状态。

跨会话实施的会话出口及历史回查由[任务推进](../../references/任务推进.md#会话预算与递进交接)定义，首次确定当前片段时加载该章节；宿主压缩恢复信号由[平台桥](../../references/平台适配器.md#内置压缩信号桥)引导该出口。
