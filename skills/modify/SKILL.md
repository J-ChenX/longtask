---
name: modify
description: Use when modifying an existing project's architecture — adding/removing/merging modules, changing build order, updating tech selections, or fixing issues found during review
---

# 长任务编排 — 框架修改

**角色：** 架构变更管理员
**场景：** ARCHITECTURE.md 存在，需要修改模块划分、依赖关系、构建顺序或技术选型

## 概述

定位变更影响范围，更新骨架文档，同步受影响的模块文档，执行变更，审查结果。不是线性走第一至五阶段——只更新受影响的部分。

**开始时声明：** "我正在使用 longtask:modify 来管理架构变更。"

## 触发条件

- 添加、删除或合并模块
- 调整模块依赖关系或构建顺序
- 变更全局技术选型
- 审查发现问题需要返工修复
- 体量评估升级（扁平→2 层，2 层→3 层）

## 修改流程

### 第一步：定位影响范围

1. 加载 `docs/ARCHITECTURE.md`
2. 分析变更请求，在模块地图上标注影响的模块
3. 列出受影响文档清单（ARCHITECTURE.md + 受影响模块的 overview/architecture/internals）
4. 向用户报告影响范围，确认后执行

### 第二步：更新骨架

1. 修改 ARCHITECTURE.md 中的模块清单、依赖关系、构建顺序
2. 受影响模块状态降级：
   - 架构变更 → 骨架 + 血肉 + 神经都可能需要更新
   - 仅依赖变更 → 血肉 + 神经需复查
   - 仅技术选型变更 → 神经层按需更新
3. 更新模块依赖图

### 第三步：同步模块文档

1. 逐一更新受影响模块的 overview.md 和 architecture.md
2. 新增模块按第三阶段流程创建文档
3. 删除模块时移除对应目录和所有交叉引用

### 第四步：执行变更

1. 按工作包实施代码变更
2. 同步更新文档代码指针
3. 文档同步门控：工作包完成 = 文档已更新

### 第五步：审查

1. 对变更范围启动轻量审查（最少 `@架构师` + 一个领域专家）
2. 若变更跨 3+ 模块，启动完整第五阶段审查

## 支撑文件

- 主技能：`SKILL.md`（各阶段详细定义）
- 文档模板：`doc-architecture.md`
- 专家角色：`expert-roles.md`