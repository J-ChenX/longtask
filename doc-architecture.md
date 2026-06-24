# 三层模块化文档架构

三层。L0 是全景入口。L1 是模块血肉。L2 是模块神经。代码是真相。

**冷记忆规则：** 每个文档都有代码指针（文件:行号）。仅使用接口签名——绝不粘贴实现。文档是地图，而非镜像。

---

## L0：`ARCHITECTURE.md`（项目骨架）

**用途：** 任何人进入项目首先阅读的文档。5 分钟了解项目全貌、模块地图、构建顺序和当前进度。

```markdown
# {项目名称} — 架构全景

**最后更新：** {YYYY-MM-DD}

## 项目概述
{3-5 句话：做什么、为谁做、核心价值}

## 模块地图
{模块间依赖关系——文本描述或 mermaid 图}

## 模块清单

| 模块 | 职责 | 依赖 | 骨架 | 血肉 | 神经 | 文档 |
|------|------|------|------|------|------|------|
| payment | 支付处理 | auth, inventory | ✅ | 🔄 | ⬜ | [→](modules/payment/) |
| inventory | 库存管理 | — | ✅ | ⬜ | ⬜ | [→](modules/inventory/) |
| auth | 用户认证 | — | ⬜ | ⬜ | ⬜ | [→](modules/auth/) |

状态标记：⬜ 待开始 | 🔄 进行中 | ✅ 已完成

## 构建顺序
1. auth — 基础模块，无依赖，优先构建
2. inventory — 独立模块，可与 auth 并行
3. payment — 依赖 auth + inventory，最后构建

## 全局技术选型
| 类别 | 选型 | 原因 |
|------|------|------|
| 运行时 | Node.js 24 | LTS 支持至 2026 |
| 数据库 | PostgreSQL 18 | 事务 + JSONB |
| 缓存 | Redis 7 | 会话 + 消息队列 |

## 全局安全策略
- **认证方式：** JWT（OAuth2 兼容）
- **授权模型：** RBAC（admin / operator / viewer）
- **数据分类：** 公开 / 内部 / 机密 / 秘密
- 各模块安全细节见模块 security.md

## 附录
- [全局关注点](appendix/global-concerns.md) — 跨模块基础设施、部署、监控
```

---

## L1：模块血肉

**默认：单文件。** 简单模块使用 `modules/{name}.md`，内容等同于下方主文档模板。  
**展开为目录**当以下任一条件成立时：单文件超过 300 行 / 需要独立 security.md 或 algorithm.md / 模块内部组件 ≥ 3 个。  
展开时：将 `{name}.md` 重命名为 `{name}/README.md`，其余文件按需创建在同目录下。

视角章节（架构、安全、算法）默认作为单文件内的 section，各节超过 100 行时再拆分为独立文件。

### `modules/{name}.md`（默认）/ `modules/{name}/README.md`（展开后）— 业务视角

```markdown
# {模块名称}

**最后更新：** {YYYY-MM-DD}
**状态：** 🔄 血肉进行中

## 一句话职责
{这个模块做什么——一句话说清楚}

## 业务背景
{为什么需要这个模块，解决什么问题——2-3 句话}

## 核心流程
{1-2 个关键业务场景的步骤描述}

| 步骤 | 描述 | 代码 |
|------|------|------|
| 1 | 用户发起支付请求 | [handler.ts:15](src/payment/handler.ts#L15) |
| 2 | 验证订单状态 | [service.ts:42](src/payment/service.ts#L42) |
| 3 | 调用支付网关扣款 | [gateway.ts:28](src/payment/gateway.ts#L28) |
| 4 | 更新订单状态 | [repository.ts:55](src/payment/repository.ts#L55) |

## 业务规则
| 规则 | 触发条件 | 代码 |
|------|---------|------|
| 重复支付检测 | 同一订单已有进行中支付 | [service.ts:50](src/payment/service.ts#L50) |
| 支付超时取消 | 30s 内未收到回调 | [service.ts:78](src/payment/service.ts#L78) |

## 对外接口摘要
| 接口 | 签名 | 消费者 |
|------|------|--------|
| POST /api/pay | `(Order) => Payment` | order 模块 |

→ 详细接口见 [architecture.md](architecture.md)
→ 实现细节见 [internals.md](internals.md)
```

### `modules/{name}/architecture.md`（或单文件内 `## 架构` section）— 架构视角

**独立文件条件：** 模块已展开为目录，且有 3+ 内部组件或 2+ 外部依赖。  
**否则：** 作为 `{name}.md` 内的 `## 架构` section。

```markdown
# {模块名称} — 架构

**最后更新：** {YYYY-MM-DD}

## 内部组件
| 组件 | 职责 | 代码 |
|------|------|------|
| PaymentService | 支付核心编排 | [service.ts:1](src/payment/service.ts#L1) |
| PaymentRepository | 支付数据持久化 | [repository.ts:1](src/payment/repository.ts#L1) |
| PaymentGateway | 第三方支付适配 | [gateway.ts:1](src/payment/gateway.ts#L1) |

## 接口详情
```typescript
// 仅签名——不含实现
export function charge(order: Order): Promise<Payment>
export interface Payment { id: string; orderId: string; status: PaymentStatus; amount: number }
export type PaymentStatus = 'pending' | 'success' | 'failed' | 'timeout'
```

## 数据流
{输入来源 → 处理步骤 → 输出目标——文本或图}

## 外部依赖
| 依赖模块 | 方式 | 接口 | 失败处理 |
|----------|------|------|---------|
| auth | HTTP POST /verify | `(token: string) => User` | 返回 401，不重试 |
| inventory | HTTP POST /reserve | `(items: Item[]) => boolean` | 重试 3 次，失败回滚 |

→ 业务规则见 [README.md](README.md)
→ 实现细节见 [internals.md](internals.md)
```

---

## L2：模块神经

深入实现细节。仅在模块进入实现阶段且已展开为目录时创建独立文件。若模块仍为单文件，则作为文件末尾的 section 追加。

### `modules/{name}/internals.md`（或单文件内 `## 实现细节` section）— 执行视角

```markdown
# {模块名称} — 实现细节

**最后更新：** {YYYY-MM-DD}

## 文件结构
```
src/payment/
  handler.ts          ← HTTP 请求处理 [handler.ts:1](src/payment/handler.ts#L1)
  service.ts          ← 核心业务逻辑 [service.ts:1](src/payment/service.ts#L1)
  repository.ts       ← 数据库访问 [repository.ts:1](src/payment/repository.ts#L1)
  gateway.ts          ← 第三方支付适配 [gateway.ts:1](src/payment/gateway.ts#L1)
  types.ts            ← 类型定义 [types.ts:1](src/payment/types.ts#L1)
tests/payment/
  service.test.ts     ← 核心逻辑测试
  gateway.test.ts     ← 支付网关模拟测试
```

## 配置
| 环境变量 | 用途 | 是否必需 | 默认值 |
|---------|------|---------|-------|
| `PAYMENT_GATEWAY_URL` | 第三方支付地址 | 是 | — |
| `PAYMENT_TIMEOUT_MS` | 支付超时时间 | 否 | `30000` |
| `PAYMENT_RETRY_MAX` | 最大重试次数 | 否 | `3` |

## 已知注意事项
- {非显而易见的行为或约束——新开发者最需要知道的}
- {例如：支付网关回调不保证顺序——通过幂等键 `idempotency_key` 处理}
- {例如：数据库事务超时设置为 5s，长连接需使用连接池}

## 测试
| 文件 | 覆盖场景 |
|------|---------|
| [service.test.ts](src/payment/__tests__/service.test.ts) | 正常支付、超时、重复支付、余额不足 |
| [gateway.test.ts](src/payment/__tests__/gateway.test.ts) | 网关超时、回调乱序、签名验证 |
```

### `modules/{name}/security.md`（或单文件内 `## 安全` section）— 安全视角

**独立文件条件：** 模块已展开为目录，且处理敏感数据或有权限控制。  
**否则：** 作为 `{name}.md` 内的 `## 安全` section。

```markdown
# {模块名称} — 安全

## 权限要求
| 操作 | 所需角色 | 执行代码 |
|------|---------|---------|
| 发起支付 | authenticated | [handler.ts:20](src/payment/handler.ts#L20) |
| 查询支付 | authenticated | [handler.ts:35](src/payment/handler.ts#L35) |
| 退款 | admin | [handler.ts:50](src/payment/handler.ts#L50) |

## 数据分类
| 数据 | 分类 | 静态加密 | 传输加密 | 访问控制 |
|------|------|---------|---------|---------|
| 支付令牌 | 秘密 | KMS | TLS 1.3 | 仅支付网关内部 |
| 支付金额 | 机密 | AES-256 | TLS 1.3 | 已认证用户 |
| 支付状态 | 内部 | 否 | TLS 1.3 | 已认证用户 |

## 威胁与缓解
| 威胁 | 缓解措施 | 代码 |
|------|---------|------|
| 重放攻击 | 幂等键 + TTL | [service.ts:50](src/payment/service.ts#L50) |
| 金额篡改 | 服务端校验订单金额 | [service.ts:35](src/payment/service.ts#L35) |
```

### `modules/{name}/algorithm.md`（或单文件内 `## 算法` section）— 算法视角

**独立文件条件：** 模块已展开为目录，且包含非平凡算法。  
**否则：** 作为 `{name}.md` 内的 `## 算法` section。若无则省略。

```markdown
# {模块名称} — 算法

## 算法：{名称}
**代码：** [src/payment/matching.ts:10-89](src/payment/matching.ts#L10)
**复杂度：** 时间 O(n log n)，空间 O(n)
**用途：** {一句话——这个算法解决什么问题}

**关键步骤：**
1. {步骤意图，不是代码}
2. {步骤意图}

**边界情况：**
| 情况 | 处理 |
|------|------|
| 空输入 | 返回空列表 |
| 重复数据 | 保留最新 |
```

---

## 附录

### `tasks/_ACTIVE.md` — 当前任务指针

```markdown
# Active longtask

task_id: {task-id}
phase: 第一阶段/第二阶段/第三阶段/第四阶段/第五阶段/完成
active_module: {module-name or none}
last_checkpoint: {最近已完成的步骤}
next_action: {下一步要做什么}
doc_version: {N — 每次 ARCHITECTURE.md 或模块文档结构性变更时递增}
updated_at: {YYYY-MM-DD}
```

**用途：** 多任务或跨会话恢复时，先读取 `_ACTIVE.md` 定位当前任务，再加载 `tasks/{task-id}/_INDEX.md` 和相关模块文档。`doc_version` 用于判断模块文档是否需要重新审查——与 ARCHITECTURE.md 中的 `最后更新` 日期对比，若 `doc_version` 落后则表示恢复前需先同步文档。

### `appendix/global-concerns.md` — 跨模块全局关注点

```markdown
# 全局关注点

## 基础设施
- **部署：** K8s Deployment，3 副本，HPA 自动扩缩
- **数据库迁移：** Flyway，`db/migrations/` 目录
- **日志：** 结构化 JSON → ELK

## 监控
- **健康检查：** `GET /health` 各服务
- **告警：** PagerDuty — 错误率 > 1% 或 P99 > 2s

## 跨模块契约
- **事件格式：** `{eventId: string; type: EventType; payload: unknown; timestamp: ISO8601}`
- **认证头：** `Authorization: Bearer <jwt>`
```

---

## 文档延续协议

**核心原则：延续优先于新建。** 80% 的文档操作是在已有文档上追加或更新，不是从头创建。

### 操作优先级

任何新工作开始前，先搜索已有文档。按以下优先级操作：

| 优先级 | 操作 | 触发条件 |
|--------|------|---------|
| 1 | 更新代码指针 | 文件移动、行号漂移、函数重命名（签名不变） |
| 2 | 更新已有 section | 接口签名变更、行为变更、规则修改 |
| 3 | 追加到已有 section | 新增接口、新增组件、新增规则 |
| 4 | 在已有文档内新增 section | 模块需要新视角（如新增安全需求） |
| 5 | 展开单文件为目录 | 单文件超过 300 行，或需要 2+ 独立视角文件 |
| 6 | 新建模块文档 | 仅当 ARCHITECTURE.md 中无对应模块时 |

### 追加规则

在已有 section 末尾追加，直接添加到对应表格最后一行：

- 新增接口 → 追加到「对外接口摘要」表格
- 新增组件 → 追加到「内部组件」表格
- 新增规则 → 追加到「业务规则」表格
- 新增流程步骤 → 追加到「核心流程」表格

### 不要新建文档的情形

| 变更 | 正确做法 |
|------|---------|
| 模块新增一个接口 | 追加到已有模块文档的接口摘要表 |
| 模块新增一个组件 | 追加到已有模块文档的内部组件表 |
| 修改一条业务规则 | 更新已有模块文档对应行的描述和代码指针 |
| 模块新增安全需求 | 在已有模块文档末尾追加 `## 安全` section |
| 修复 bug 改变行为 | 更新已有模块文档中对应流程步骤的描述 |

### 展开单文件为目录

当模块单文件超过 300 行时：
1. 创建 `modules/{name}/` 目录
2. 将 `modules/{name}.md` 重命名为 `modules/{name}/README.md`
3. 将超过 100 行的 section 拆分为独立文件（architecture.md、security.md 等）
4. 在 README.md 中用 MD 引用替代被拆出的 section：`→ 详见 [architecture.md](architecture.md)`
5. 更新 ARCHITECTURE.md 中模块的文档链接

---

## 文档生命周期规则

**第二阶段（架构分层）产出：**
- 创建 `ARCHITECTURE.md`：项目概述、模块清单（骨架状态标记）、构建顺序、全局选型

**第三阶段（文档化）产出：**
- 每个模块创建 `modules/{name}.md`（单文件，默认）
- 若模块已达到展开条件，创建 `modules/{name}/README.md` + 按需创建其余文件
- 更新 `ARCHITECTURE.md` 中模块的血肉状态
- 标记为 `[计划中 — 代码尚未存在]` 的章节为待完成项

**第四阶段（执行）产出：**
- 每个工作包完成后，**追加或更新**对应模块文档中的 section
- 用真实代码指针替换 `[计划中]` 占位符
- 单文件超过 300 行时执行展开流程
- 更新 `ARCHITECTURE.md` 中模块的神经状态

**第五阶段（审查）：**
- 审查者检查所有 `[计划中]` 标记是否已消除
- 检查 `ARCHITECTURE.md` 状态表与实际文档一致性
- 残留 `[计划中]` = 阻断

**模块移除时：**
- 删除模块文件（或目录），从 `ARCHITECTURE.md` 模块清单中移除
- 更新依赖该模块的其他模块文档

**视角文档增减：**
- 不需要的视角不创建
- 执行过程中需要新视角时：优先在已有文件内追加 section，超过 100 行再拆分为独立文件
