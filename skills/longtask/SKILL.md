---
name: longtask
description: 编排跨上下文或独立验收包的编码任务，在授权内分工多窗口并递进交接；小改动、解释和普通审查不启用。
license: MIT
compatibility: Designed for Codex. Requires Python 3.14+; install the complete longtask plugin.
metadata:
  version: 4.6.0
---

# Longtask 入口

这是完整插件的发现入口。读取[根技能](../../SKILL.md)选择工作流并遵循共享规则；已加载且仍有效时直接复用。相对路径以插件根为准，不另建工作流或重复查询状态。

首次拆分独立验收包或用户要求多窗口时，加载[执行方式选择](../../references/平行任务协作.md#首次分工与执行方式)，不能只建包或启动内部子智能体便称多窗口已派发。跨会话实施的会话出口及历史回查由[任务推进](../../references/任务推进.md#会话预算与递进交接)定义，首次确定当前片段时加载该章节；宿主压缩恢复信号由[平台桥](../../references/平台适配器.md#内置压缩信号桥)引导该出口。
