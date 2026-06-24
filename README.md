# longtask

一个 Claude Code / Codex 双平台技能，用于编排跨多个上下文窗口的长任务，或在多个智能体会话间需要保持一致架构决策的场景。

## 解决的问题

长任务在会话间丢失上下文时会失败。下一个智能体从零开始，做出不一致的决策，重复已回答的问题。会话 1 中做出的架构选择对会话 5 中的智能体不可见。

## 解决方案

**文档即手册架构：** 结构化的领域文档让任何 AI 智能体都能从任意节点恢复工作，无需依赖对话历史。

> 文档即记忆。代码即真相。文档指向代码——永不复制实现。

## 分支技能

longtask 提供 1 个主入口和 5 个场景化子技能。根据当前状态选择正确的入口：

### `longtask`（主入口）

从 `docs/ARCHITECTURE.md` 状态表自动检测项目状态，路由到对应场景。

```
longtask/
  SKILL.md              ← longtask（主入口：状态检测 + 路由）
  skills/
    setup/SKILL.md      ← longtask-setup     文档建立
    continue/SKILL.md   ← longtask-continue  继续任务
    review/SKILL.md     ← longtask-review    审查
    modify/SKILL.md     ← longtask-modify    框架修改
    retrofit/SKILL.md   ← longtask-retrofit  既有项目建档
```

### `longtask-setup` — 文档建立

**时机：** 全新项目从零开始。不存在 `docs/ARCHITECTURE.md`。

**流程：** 发现 → 架构分层 → 文档化 → 执行 → 审查（完整五阶段流程）。

### `longtask-continue` — 继续任务

**时机：** 工作中途中断。`docs/ARCHITECTURE.md` 存在但模块状态有未完成项。

**流程：** 状态检测 → 定位当前阶段恢复节点 → 继续走完后续阶段。

### `longtask-review` — 审查

**时机：** 所有模块已 ✅，或明确要求审查既有代码和文档。

**流程：** 加载全部文档 → 组建专家小组（最少 3 人）→ 各专家独立审查 → 汇编发现 → 报告状态（通过 / 有条件通过 / 不通过）。

### `longtask-modify` — 框架修改

**时机：** 增删合并模块、变更依赖关系、更新技术选型、修复审查发现的问题。

**流程：** 影响分析 → 更新 ARCHITECTURE.md 骨架 → 同步受影响模块文档 → 执行变更 → 轻量审查。

### `longtask-retrofit` — 既有项目建档

**时机：** 既有代码库尚无 longtask 文档，需要建立三层文档体系。

**流程：** 代码扫描 → 推断模块边界 → 创建 ARCHITECTURE.md → 创建模块文档 → 质量抽查。跳过发现（代码就是规格）和执行（代码已存在）。

## 五阶段工作流

| 阶段 | 输入 | 输出 | 门控 |
|------|------|------|------|
| 第一阶段：发现 | 用户描述 | `_INDEX.md` 发现部分 | 用户批准发现记录 |
| 第二阶段：架构分层 | 发现记录 | `ARCHITECTURE.md`（模块地图 + 构建顺序 + 状态表） | 用户批准分层架构 |
| 第三阶段：文档化 | 发现记录 + ARCHITECTURE.md | 模块文档（默认单文件，按需展开目录）+ 工作包 | 用户批准文档结构 |
| 第四阶段：执行 | 模块文档 + 代码库 | 实现代码 + 更新后的代码指针 | 每个工作包文档更新后方可标记完成 |
| 第五阶段：审查 | 代码 + ARCHITECTURE.md + 模块文档 | `review-log.md` | 所有阻断项已解决 |

每个阶段可在独立的上下文窗口中运行。智能体通过加载文档而非对话历史来恢复工作。

每个阶段门控包含独立的 Agent 预审后才提交用户确认。

## 安装方式

### Claude Code / Codex 个人技能

```bash
# macOS / Linux
cp -r longtask ~/.claude/skills/

# Windows
xcopy /E /I longtask %USERPROFILE%\.claude\skills\longtask
```

### 插件安装

遵循 [agentskills.io 规范](https://agentskills.io/specification) 进行插件部署。

## 文件说明

| 文件 | 用途 |
|------|------|
| [`SKILL.md`](SKILL.md) | 主技能定义——状态检测、五阶段工作流、恢复节点、常见错误 |
| [`doc-architecture.md`](doc-architecture.md) | 三层模块化文档模板（ARCHITECTURE.md + 模块视角文档） |
| [`expert-roles.md`](expert-roles.md) | 专家定义、文档记录员角色、预审 Agent、审查协议 |
| [`skills/setup/SKILL.md`](skills/setup/SKILL.md) | 新项目文档建立入口 |
| [`skills/continue/SKILL.md`](skills/continue/SKILL.md) | 中断工作恢复入口 |
| [`skills/review/SKILL.md`](skills/review/SKILL.md) | 独立多专家审查入口 |
| [`skills/modify/SKILL.md`](skills/modify/SKILL.md) | 架构修改入口 |
| [`skills/retrofit/SKILL.md`](skills/retrofit/SKILL.md) | 既有项目建档入口 |
| [`agents/openai.yaml`](agents/openai.yaml) | Codex UI 元数据 |
| [`scripts/validate_longtask.py`](scripts/validate_longtask.py) | longtask 约定自检脚本 |

## 三层模块化文档

技能激活后，在项目下创建以下结构：

```
docs/
  ARCHITECTURE.md                 ← L0：项目骨架（模块地图 + 构建顺序 + 状态）
  modules/
    {模块名}.md                   ← 默认：单文件模块文档
    {复杂模块名}/
      README.md                   ← 展开后：业务视角（职责、流程、规则）
      architecture.md             ← 展开后：架构视角（接口、组件、数据流）
      internals.md                ← 展开后：实现细节（文件结构、配置、已知坑）
      security.md                 ← 展开后：安全细节（权限、数据分类）
      algorithm.md                ← 展开后：算法细节（按需，仅非平凡算法）
  appendix/
    global-concerns.md            ← 跨模块关注点（基础设施、部署、监控）
```

**L0 是项目入口。** 5 分钟读完 `ARCHITECTURE.md` 即可理解项目全貌。
**L1 是模块血肉。** 简单模块读 `modules/{name}.md`；复杂模块读 `modules/{name}/README.md`。
**L2 是模块神经。** 需要实现细节时才深入 `internals.md`。

文档按**模块**组织（而非按领域）。简单模块先保持单文件；复杂模块再展开目录，并通过 markdown 交叉引用连接各视角文件。

## 许可协议

MIT
