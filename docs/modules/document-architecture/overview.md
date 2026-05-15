# 文档架构

**最后更新：** 2026-05-15
**状态：** 🔄 血肉进行中

## 一句话职责

定义三层模块化文档模板——ARCHITECTURE.md（骨架）、模块视角文档（血肉）、实现细节文档（神经）。

## 业务背景

旧文档体系按领域（架构/业务/安全）切分，人类理解一个模块需要跨 4+ 份文件拼凑信息。新体系改为按模块组织——进入 `modules/payment/` 目录即可看到该模块的全部视角文档。各视角文档通过 MD 引用互连，兼顾人类阅读直觉和 LLM 检索效率。

## 核心流程

### 文档创建流程

| 步骤 | 描述 | 代码 |
|------|------|------|
| 1 | 第二阶段创建 ARCHITECTURE.md（模块地图 + 构建顺序 + 状态表） | [doc-architecture.md:11-36](doc-architecture.md#L11) |
| 2 | 第三阶段为每个模块创建 overview.md（必须）+ architecture.md（按需） | [doc-architecture.md:38-86](doc-architecture.md#L38) |
| 3 | 第四阶段创建 internals.md + security.md + algorithm.md（按需） | [doc-architecture.md:89-152](doc-architecture.md#L89) |
| 4 | 更新 ARCHITECTURE.md 状态表 | [doc-architecture.md:260-265](doc-architecture.md#L260) |

## 三层结构

| 层级 | 文件 | 定位 | 阅读时间 |
|------|------|------|---------|
| L0 骨架 | ARCHITECTURE.md | 项目全景、模块地图、构建顺序、状态 | 5 分钟 |
| L1 血肉 | modules/{name}/overview.md | 模块业务视角（职责、流程、规则） | 10 分钟 |
| L1 血肉 | modules/{name}/architecture.md | 模块架构视角（组件、接口、数据流） | 按需 |
| L2 神经 | modules/{name}/internals.md | 文件结构、配置、已知坑 | 深入时 |
| L2 神经 | modules/{name}/security.md | 权限、数据分类、威胁 | 按需 |

## 业务规则

| 规则 | 触发条件 | 代码 |
|------|---------|------|
| overview.md 必须 | 每个模块至少创建 overview.md | [doc-architecture.md](doc-architecture.md) |
| architecture.md 按需 | 模块有 3+ 内部组件或 2+ 外部依赖 | [doc-architecture.md](doc-architecture.md) |
| 视角分离 | 不同视角内容严格分文档，不混淆 | [doc-architecture.md](doc-architecture.md) |
| MD 引用互联 | 视角文档间通过相对路径引用建立联系 | [doc-architecture.md](doc-architecture.md) |
| 代码指针仅签名 | 文档中只写接口签名 + 文件:行号，不粘贴实现 | [doc-architecture.md:5](doc-architecture.md#L5) |

## 对外接口摘要

| 接口 | 签名 | 消费者 |
|------|------|--------|
| ARCHITECTURE.md 模板 | `(项目信息) => Markdown` | 第二阶段 |
| overview.md 模板 | `(模块信息) => Markdown` | 第三阶段 |
| architecture.md 模板 | `(组件信息) => Markdown` | 第三阶段 |
| internals.md 模板 | `(实现信息) => Markdown` | 第四阶段 |
| security.md 模板 | `(安全信息) => Markdown` | 第四阶段 |

→ 实现细节见 [internals.md](internals.md)