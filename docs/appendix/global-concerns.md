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
- 需要为长任务前期构建文档框架
- 任务进行中需要补充文档内容
- 文档完整构建

不触发：
- 单次对话可完成的任务（< 3 个工作包，< 4 小时）

## 内化的思维模式

longtask 从以下技能中学习并内化了其核心思维模式，无需外部调用：

| 来源 | 内化的思维模式 | 应用阶段 |
|------|--------------|---------|
| brainstorming | 单问题澄清；提出 2-3 方案含权衡；逐段呈现设计并确认；HARD-GATE——未获批准不进入实施 | 第一阶段 |
| writing-plans | 先映射文件结构再分解任务；2-5 分钟粒度；步骤无占位符；自审三项（覆盖率、占位符、类型一致性） | 第四阶段 |
| subagent-driven-development | 每工作包注入精准上下文而非继承历史；双阶段审查（规格合规 → 代码质量）；四状态处理（DONE / CONCERNS / NEEDS_CONTEXT / BLOCKED） | 第四阶段 |
| requesting-code-review | 早审查、常审查；按严重性分级处理（Critical 立即修复 / Important 前进前修复 / Minor 记录） | 第五阶段 |
| finishing-a-development-branch | 测试通过才呈现选项；给出四个结构化选项（合并/PR/保留/丢弃）；破坏性操作需显式确认 | 第五阶段 |

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
