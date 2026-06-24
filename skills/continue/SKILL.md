---
name: longtask-continue
description: Use when resuming interrupted work — docs/ARCHITECTURE.md exists, you need to pick up from where you left off
---

# 长任务编排 — 继续任务

**角色：** 任务恢复员
**场景：** `docs/ARCHITECTURE.md` 存在，工作在某阶段中断

## 概述

检测项目当前状态，从上次中断的位置恢复工作。

**开始时声明：** "我正在使用 longtask-continue 来恢复工作。"

## 状态检测

1. 优先加载 `docs/tasks/_ACTIVE.md`；若存在，按其中的 `task_id`、`phase`、`active_module`、`next_action` 恢复
2. 若 `_ACTIVE.md` 不存在或已标记完成，加载 `docs/ARCHITECTURE.md`
3. 解析模块状态表（骨架/血肉/神经列）
4. 根据以下矩阵判定当前阶段：

| 检测信号 | 当前阶段 | 入口 |
|---------|---------|------|
| 骨架有 ⬜ | 第二阶段 | 第二阶段恢复节点 |
| 血肉有 ⬜ | 第三阶段 | 第三阶段恢复节点 |
| 神经有 ⬜ | 第四阶段 | 第四阶段恢复节点 |

5. 向用户报告：当前处于第 N 阶段，骨架 X/Y ✅，血肉 X/Y ✅，神经 X/Y ✅
6. 用户确认后，从对应阶段的恢复节点继续

## 恢复节点

各阶段恢复节点定义在 `SKILL.md` 中：

- **第二阶段恢复节点：** 加载 ARCHITECTURE.md → 检查模块骨架状态 → 继续定义缺失模块
- **第三阶段恢复节点：** 加载 ARCHITECTURE.md + 已有模块文档 → 为 ⬜ 模块创建默认单文件文档或展开目录 README.md
- **第四阶段恢复节点：** 加载 ARCHITECTURE.md + 对应模块文档（单文件或 README.md）+ 按需加载 architecture.md → 按工作包执行

从恢复节点继续后，按正常流程走完后续阶段。

## 支撑文件

- 主技能：`SKILL.md`（五阶段详细定义、恢复节点）
- 文档模板：`doc-architecture.md`
- 专家角色：`expert-roles.md`
