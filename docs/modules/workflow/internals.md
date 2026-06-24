# 工作流引擎 — 实现细节

**最后更新：** 2026-06-24

## 文件结构

| 文件 | 用途 |
|------|------|
| [SKILL.md](../../../SKILL.md) | 主入口、状态检测、五阶段流程、平台适配、变更分级 |
| [skills/setup/SKILL.md](../../../skills/setup/SKILL.md) | 新项目完整五阶段入口 |
| [skills/continue/SKILL.md](../../../skills/continue/SKILL.md) | 中断任务恢复入口 |
| [skills/review/SKILL.md](../../../skills/review/SKILL.md) | 多专家审查入口 |
| [skills/modify/SKILL.md](../../../skills/modify/SKILL.md) | 架构变更入口 |
| [skills/retrofit/SKILL.md](../../../skills/retrofit/SKILL.md) | 既有项目建档入口 |

## 配置与约定

| 项 | 约定 |
|----|------|
| 子技能命名 | 使用 `longtask-*`，避免通用名冲突 |
| 当前任务指针 | `docs/tasks/_ACTIVE.md` |
| 并行策略 | 优先平台原生 subagents；不可用时按 Codex 降级协议顺序执行 |
| 外部技能依赖 | 无 |

## 测试

| 命令 | 覆盖场景 |
|------|---------|
| `scripts/validate_longtask.py` | 命名、旧引用、外部技能依赖、模块 README、插件注册 |
