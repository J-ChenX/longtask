---
name: longtask-review
description: Use when all implementation is complete and you need a multi-expert review — or when you explicitly want to review existing code and docs
---

# 长任务编排 — 审查

**角色：** 审查协调员
**场景：** 所有模块状态为 ✅，或用户明确要求审查

## 概述

启动多专家独立审查，检查代码正确性、文档同步性、架构完整性、安全性和业务规则。

**开始时声明：** "我正在使用 longtask-review 来执行多专家审查。"

## 入口

直接进入第五阶段：审查。加载主技能 `SKILL.md` 第五阶段定义。

## 审查流程

1. 加载 `docs/ARCHITECTURE.md` 和所有模块文档
2. 确定专家小组组成（最少 3 人，按需添加领域专家）：

| 角色 | 关注点 |
|------|--------|
| `@架构师` | 设计完整性、服务边界、接口稳定性、可扩展性 |
| `@业务分析师` | 业务规则完整性、不变量执行、规格漂移 |
| `@安全工程师` | 威胁模型、授权、数据暴露 |
| `@质量负责人` | 测试覆盖率、回归风险、可测试性 |
| `@领域专家` | 任务特定领域陷阱 |

3. 按模拟协议依次运行每位专家——每位独立审查，审查过程中不混合视角
4. 汇编发现到 `docs/tasks/{task-id}/review-log.md`
5. 确定总体状态：通过 / 有条件通过 / 不通过

**硬性规则：** 文档过时（与实际代码矛盾）= 阻断，与测试失败同等严重。

## 审查输出格式

参阅 `expert-roles.md` 获取完整审查协议和输出格式。

## 支撑文件

- 主技能：`SKILL.md`（第五阶段详细定义）
- 专家角色：`expert-roles.md`（审查协议、严重性级别）
