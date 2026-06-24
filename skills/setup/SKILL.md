---
name: longtask-setup
description: Use when starting a brand new project from scratch — no docs exist yet, you need to go through full discovery, architecture layering, documentation, execution, and review
---

# 长任务编排 — 文档建立

**角色：** 项目启动向导
**场景：** 全新项目，无 `docs/ARCHITECTURE.md`

## 概述

为全新项目建立三层模块化文档体系，完整走第一至五阶段工作流。

**开始时声明：** "我正在使用 longtask-setup 来启动新项目。"

## 入口

直接进入第一阶段：发现。不执行状态检测——本场景假设项目为全新状态。

## 工作流

加载主技能 `SKILL.md`，按以下阶段顺序执行：

1. **第一阶段：发现** — 单问规则，逐一覆盖业务意图、技术约束、安全风险、边界情况、集成面
2. **第二阶段：架构分层** — 评估体量，创建 `docs/ARCHITECTURE.md`（模块地图 + 构建顺序 + 状态表）
3. **第三阶段：文档化** — 为每个模块创建 `docs/modules/{module}.md`（默认单文件）或 `docs/modules/{module}/README.md`（展开目录），定义工作包
4. **第四阶段：执行** — 按工作包实施，同步更新文档代码指针
5. **第五阶段：审查** — 多专家独立审查，文档过时 = 阻断

每个阶段门控前执行预审，获得用户批准后进入下一阶段。

## 支撑文件

- 主技能：`SKILL.md`（五阶段详细定义）
- 文档模板：`doc-architecture.md`
- 专家角色：`expert-roles.md`
