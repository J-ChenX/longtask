# 全局关注点

## 文件清单

```
longtask/
  SKILL.md                        ← 主技能定义（状态检测 + 五阶段工作流）
  doc-architecture.md             ← 三层文档模板
  expert-roles.md                 ← 专家角色 + 审查协议
  README.md                       ← 项目说明 + 安装指引
  agents/
    openai.yaml                   ← Codex UI 元数据
  .claude-plugin/
    plugin.json                   ← Claude Code 插件注册（含 1 个主入口 + 5 个子技能）
    marketplace.json              ← 插件市场元数据
  scripts/
    validate_longtask.py          ← longtask 约定自检脚本
  skills/
    setup/SKILL.md                ← longtask-setup（文档建立）
    continue/SKILL.md             ← longtask-continue（继续任务）
    review/SKILL.md               ← longtask-review（审查）
    modify/SKILL.md               ← longtask-modify（框架修改）
    retrofit/SKILL.md             ← longtask-retrofit（既有项目建档）
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

## 内置执行模式

longtask 不依赖外部技能调用。以下模式直接内置在本技能的阶段协议中：

| 模式 | 行为 | 应用阶段 |
|------|------|---------|
| 单问发现 | 每次只问一个问题；技术和安全问题可给 2-4 个带权衡的选项 | 第一阶段 |
| 工作包计划 | 先映射文件结构再分解任务；每个工作包保持 2-4 小时粒度 | 第三/第四阶段 |
| 独立执行 | 每个工作包只加载必要模块文档和代码指针；平台支持时可用原生 subagents 并行 | 第四阶段 |
| 多专家审查 | 各专家独立得出结论后再汇总；按阻断/文档过时/警告/建议分级 | 第五阶段 |
| 完成门控 | 测试和文档同步通过后才标记完成；破坏性操作需用户显式确认 | 第五阶段 |

## 分支技能

本技能提供 5 个场景化子技能作为直达入口：

| 子技能 | 场景 | 触发条件 |
|--------|------|---------|
| `longtask-setup` | 文档建立 | 全新项目，无 ARCHITECTURE.md |
| `longtask-continue` | 继续任务 | 工作中断，需恢复 |
| `longtask-review` | 审查 | 全部 ✅ 或明确要求审查 |
| `longtask-modify` | 框架修改 | 改架构/增减模块/调整依赖 |
| `longtask-retrofit` | 既有项目建档 | 代码存在但无 longtask 文档 |

各分支详细定义见 `skills/{name}/SKILL.md`。若不指定子技能，`longtask` 主入口自动检测项目状态后路由。
