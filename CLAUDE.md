# CLAUDE.md

## 项目身份

`longtask` 是一个 Claude Code 技能，用于编排跨多个上下文窗口的长任务。

**核心理念：** 文档即记忆——结构化的领域文档让任何 AI 智能体都能从任意节点恢复工作，无需依赖对话历史。

**关键约束：** 文档指向代码——永不复制实现。仅使用代码指针（文件:行号）和接口签名。

## 目录结构

```
SKILL.md                  ← 主技能：状态检测 + 五阶段工作流 + 恢复节点 + 常见错误
doc-architecture.md       ← 三层文档模板定义
expert-roles.md           ← 专家角色 + 审查协议 + 预审 Agent + 文档记录员
README.md                 ← 用户面向的项目说明
.claude-plugin/
  plugin.json             ← 插件注册（含 6 个子技能声明）
  marketplace.json        ← 插件市场元数据
skills/
  setup/SKILL.md          ← 子技能：文档建立（完整第一至五阶段）
  continue/SKILL.md       ← 子技能：继续任务（状态检测 + 恢复节点）
  review/SKILL.md         ← 子技能：多专家审查
  modify/SKILL.md         ← 子技能：框架修改（影响分析 + 轻量审查）
  retrofit/SKILL.md       ← 子技能：既有项目建档（逆向分析）
docs/
  ARCHITECTURE.md         ← 本项目的三层文档骨架
  modules/
    workflow/             ← 工作流引擎模块文档
    document-architecture/← 文档架构模块文档
    expert-system/        ← 专家系统模块文档
  appendix/
    global-concerns.md    ← 全局关注点
```

## 架构概览

三个核心模块 + 一个分支层：

| 模块 | 文件 | 职责 |
|------|------|------|
| 工作流引擎 | SKILL.md | 五阶段编排、阶段门控、状态检测路由、恢复节点 |
| 文档架构 | doc-architecture.md | 三层模板（ARCHITECTURE.md + overview/architecture/internals） |
| 专家系统 | expert-roles.md | 7 个专家角色、审查协议、严重性级别、预审机制 |
| 分支技能 | skills/*/SKILL.md | 5 个场景化直达入口，绕过状态检测 |

## 五阶段工作流

发现 → 架构分层 → 文档化 → 执行 → 审查

每个阶段有：角色、输入、输出、预审步骤、阶段门控、恢复节点、无人值守策略。

## 修改指南

- 修改工作流行为 → 编辑 `SKILL.md` 对应阶段章节
- 新增专家角色 → 编辑 `expert-roles.md`，同步更新 `docs/modules/expert-system/`
- 修改文档模板 → 编辑 `doc-architecture.md`，同步更新 `docs/modules/document-architecture/`
- 新增子技能 → 在 `skills/` 下创建目录和 SKILL.md，更新 `plugin.json` 和 `marketplace.json`
- 修改子技能行为 → 编辑 `skills/{name}/SKILL.md`

**每次修改后必须：**
1. 同步更新 `docs/ARCHITECTURE.md` 及相关模块文档
2. 修正所有受影响的代码指针（文件:行号）
3. 检查 SKILL.md / doc-architecture.md / expert-roles.md 之间的交叉引用一致性

## 本项目自身的文档

本项目使用自己的三层文档体系记录自身：

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — 项目骨架（5 分钟了解全貌）
- [docs/modules/workflow/](docs/modules/workflow/) — 工作流引擎详细文档
- [docs/modules/document-architecture/](docs/modules/document-architecture/) — 文档架构详细文档
- [docs/modules/expert-system/](docs/modules/expert-system/) — 专家系统详细文档

## 设计哲学

- 文档即记忆 — 文档弥补跨会话无持久上下文的不足
- 代码优先真相 — 文档与代码冲突时，更新文档
- 全局先于局部 — 先建骨架再填血肉
- 预审先于确认 — 重要文档在用户看到前先经独立审查
- 模块为主，视角为辅 — 文档按模块组织，模块内按视角分文档