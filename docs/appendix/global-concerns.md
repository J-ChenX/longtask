# 全局关注点

## 文件清单

```
longtask/
  SKILL.md                        ← 主技能定义（状态检测 + 五阶段工作流）
  doc-architecture.md             ← 三层文档模板
  expert-roles.md                 ← 专家角色 + 审查协议
  README.md                       ← 项目说明 + 安装指引
  .claude-plugin/
    plugin.json                   ← Claude Code 插件注册（含 6 个子技能）
    marketplace.json              ← 插件市场元数据
  skills/
    setup/SKILL.md                ← longtask:setup（文档建立）
    continue/SKILL.md             ← longtask:continue（继续任务）
    review/SKILL.md               ← longtask:review（审查）
    modify/SKILL.md               ← longtask:modify（框架修改）
    retrofit/SKILL.md             ← longtask:retrofit（既有项目建档）
  docs/
    ARCHITECTURE.md               ← 项目骨架
    modules/
      workflow/                   ← 工作流引擎文档
      document-architecture/      ← 文档架构文档
      expert-system/              ← 专家系统文档
    appendix/
      global-concerns.md          ← 本文件
```

## 安装方式

**Claude Code 个人技能：**
```bash
# macOS / Linux
cp -r longtask ~/.claude/skills/

# Windows
xcopy /E /I longtask %USERPROFILE%\.claude\skills\longtask
```

**插件安装：** 遵循 [agentskills.io](https://agentskills.io/specification) 规范。

## 技能触发条件

满足以下任一条件时触发：
- 预计超过 3 个独立工作包，或工作量 > 4 小时
- 架构决策必须在各阶段保持一致
- 任务涉及 2+ 独立领域
- 工作可能在中途移交给全新的智能体

不触发：
- 单次对话可完成的任务（< 3 个工作包，< 4 小时）

## 集成技能

| 技能 | 时机 |
|------|------|
| `superpowers:brainstorming` | 第一阶段前（需求不明确） |
| `superpowers:writing-plans` | 第四阶段（正式实施计划） |
| `superpowers:subagent-driven-development` | 第四阶段（并行工作包） |
| `superpowers:requesting-code-review` | 第五阶段（正式代码审查） |
| `superpowers:finishing-a-development-branch` | 第五阶段批准后 |

## 分支技能

本技能提供 5 个场景化子技能作为直达入口：

| 子技能 | 场景 | 触发条件 |
|--------|------|---------|
| `longtask:setup` | 文档建立 | 全新项目，无 ARCHITECTURE.md |
| `longtask:continue` | 继续任务 | 工作中断，需恢复 |
| `longtask:review` | 审查 | 全部 ✅ 或明确要求审查 |
| `longtask:modify` | 框架修改 | 改架构/增减模块/调整依赖 |
| `longtask:retrofit` | 既有项目建档 | 代码存在但无 longtask 文档 |

各分支详细定义见 `skills/{name}/SKILL.md`。若不指定子技能，`longtask` 主入口自动检测项目状态后路由。

## 版本历史

| 版本 | 日期 | 变更 |
|------|------|------|
| 2.1 | 2026-05-15 | 重命名为 longtask + 状态感知路由 + 5 个场景化分支入口 |
| 2.0 | 2026-05-15 | 五阶段工作流 + 三层模块化文档 + 预审机制 + 文档记录员 |
| 1.0 | 2026-05-14 | 初始版本：四阶段工作流 + L1/L2 领域文档 |