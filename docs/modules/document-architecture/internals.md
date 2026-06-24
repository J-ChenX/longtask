# 文档架构 — 实现细节

**最后更新：** 2026-06-24

## 文件结构

| 文件 | 用途 |
|------|------|
| [doc-architecture.md](../../../doc-architecture.md) | 三层文档模板、展开规则、文档延续协议、生命周期规则 |
| [docs/ARCHITECTURE.md](../../ARCHITECTURE.md) | 本项目的 L0 架构全景和模块状态表 |
| [docs/modules/](../) | 本项目的模块文档实例 |
| `docs/tasks/_ACTIVE.md` | 运行时创建的当前任务指针模板位置 |

## 配置与约定

| 项 | 约定 |
|----|------|
| 默认模块文档 | `docs/modules/{name}.md` |
| 展开后主文档 | `docs/modules/{name}/README.md` |
| 展开触发 | 单文件超过 300 行、需要独立安全/算法视角、或内部组件 >= 3 |
| 代码内容 | 只写代码指针和接口签名，不复制实现 |

## 测试

| 命令 | 覆盖场景 |
|------|---------|
| `scripts/validate_longtask.py` | 禁止旧模块主文档命名，检查展开模块 README 是否存在 |
