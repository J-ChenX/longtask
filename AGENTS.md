# AGENTS.md

## 项目身份

`longtask` 是一个 AI 编码助手技能（支持 Claude Code 和 Codex），用于编排跨多个上下文窗口的长任务。

**核心理念：** 文档即记忆——结构化的领域文档让任何 AI 智能体都能从任意节点恢复工作，无需依赖对话历史。

**关键约束：** 文档指向代码——永不复制实现。仅使用代码指针（文件:行号）和接口签名。

## 主要技能文件

| 文件 | 职责 |
|------|------|
| `SKILL.md` | 主技能：状态检测 + 五阶段工作流 + 变更分级 + 平台适配 |
| `doc-architecture.md` | 渐进式文档架构模板 + 文档延续协议 |
| `expert-roles.md` | 专家角色 + 审查协议 + 预审步骤（含 Codex 执行方式） |
| `skills/*/SKILL.md` | 5 个场景化子技能直达入口 |
| `agents/openai.yaml` | Codex UI 元数据 |
| `scripts/validate_longtask.py` | longtask 约定自检脚本 |

## 使用方式（Codex）

用户请求 longtask 技能时：
1. 读取 `SKILL.md` — 执行状态检测，确定当前阶段
2. 子技能直达：`longtask-setup` / `longtask-continue` / `longtask-review` / `longtask-modify` / `longtask-retrofit`

## Codex 平台执行规范

### 预审步骤
- **优先：** 显式请求 Codex 生成独立 subagent 执行预审（`spawn a subagent to review this document`）
- **降级：** 不可用时，在同一上下文内切换角色提示词，声明"忽略前文对话，仅从文档审查员视角审查"

### 多专家审查
- **优先：** 每位专家生成一个 subagent，并行执行（max 6 threads）
- **降级：** 顺序执行，每位专家前使用 `---` 分隔线 + 角色声明

### 自定义专家 Agent（可选）
在 `.codex/agents/` 下创建 TOML 文件，将 `expert-roles.md` 中的角色提示词作为 `developer_instructions`。

## 核心规则

**文档延续优先（见 `doc-architecture.md` 文档延续协议）：**
- 先搜索已有文档，再决定操作：更新指针 → 追加条目 → 新增 section → 展开文件 → 新建文档
- 不要为每个小变更新建文档

**变更影响分级（每次代码修改后执行）：**
- L0 仅格式化/注释 → 无需更新文档
- L1 行号漂移/重命名 → grep 替换代码指针
- L2 签名变更 → 更新接口描述
- L3 新增公开接口/组件 → 追加文档条目
- L4 新增/移除模块 → 执行 `longtask-modify`

**文档结构（渐进式展开）：**
- 默认单文件：`docs/modules/{name}.md`
- 超过 300 行或需多个视角文件时展开为 `docs/modules/{name}/` 目录

**硬性规则：**
- 工作包完成 = 文档已更新，无例外
- 文档过时 = 阻断，与测试失败同等严重
- 仅使用代码指针（文件:行号），绝不粘贴实现代码

## 修改本项目

| 修改目标 | 操作 |
|---------|------|
| 工作流行为 | 编辑 `SKILL.md` 对应阶段章节 |
| 专家角色 | 编辑 `expert-roles.md` |
| 文档模板 | 编辑 `doc-architecture.md` |
| 新增子技能 | 在 `skills/` 下创建目录 + SKILL.md |
| Codex Agent 配置 | 编辑 `.codex/agents/{role}.toml` |

**每次修改后：** 同步更新 `docs/ARCHITECTURE.md` + 修正受影响的代码指针 + 检查三个核心文件间的交叉引用一致性。
同时运行 `scripts/validate_longtask.py`，确保没有旧入口、旧模块主文档命名或外部技能依赖残留。
