# 两层文档架构

两层。L1 是恢复节点。L2 是领域手册。代码是真相。

**冷记忆规则：** 每个 L2 章节都有代码指针（文件:行号）。仅使用接口签名——绝不粘贴实现。文档是地图，而非镜像。

---

## 第一层：`_INDEX.md`（主文档）

**用途：** 任何智能体在中途恢复任务时首先加载的文档。必须在 2 分钟内可读完毕。  
**大小限制：** ≤150 行。若超过此限制，任务需要分解。

### 模板

```markdown
# {任务名称}

**任务 ID：** {task-id}
**状态：** 第 {N} 阶段 — {当前状态一行描述}
**开始日期：** {YYYY-MM-DD}
**最后更新：** {YYYY-MM-DD}

## 摘要
{2–3 句话：构建什么、为何存在、"完成"是什么样子}

## 阶段清单
- [x] 第一阶段：发现 — 已完成
- [x] 第二阶段：文档化 — 已完成
- [ ] 第三阶段：执行 — 进行中
- [ ] 第四阶段：审查 — 待定

## 工作包
| ID | 名称 | 领域 | 负责人 | 状态 |
|----|------|------|--------|------|
| WP-1 | {名称} | {领域} | {专家} | 已完成 |
| WP-2 | {名称} | {领域} | {专家} | 进行中 |
| WP-3 | {名称} | {领域} | {专家} | 待定 |

## L2 文档
| 文档 | 领域 | 内容 |
|------|------|------|
| [tech-arch.md](tech-arch.md) | 架构 | 服务设计、接口、基础设施 |
| [business-logic.md](business-logic.md) | 业务 | 规则、不变量、用户旅程 |
| [solution-detail.md](solution-detail.md) | 实现 | 文件结构、组件、配置 |
| [algorithm.md](algorithm.md) | 算法 | 核心计算逻辑 |
| [security.md](security.md) | 安全 | 认证模型、威胁矩阵 |
| [review-log.md](review-log.md) | 审查 | 发现和解决清单 |

## 跨领域契约
{跨 L2 文档共享的关键接口——事件模式、共享类型、服务间 API 契约}

示例：
- 订单事件模式：`{orderId: string, status: OrderStatus, timestamp: ISO8601}` — 参见 [events/order.ts:5](src/events/order.ts#L5)

## 开放问题
- [ ] {问题} — 由 {专家} 于 {YYYY-MM-DD} 提出
- [x] {问题} — 已解决：{答案}，{YYYY-MM-DD}
```

---

## 第二层：领域文档

仅创建本任务所需的 L2 文档。每个文档覆盖一个领域。省略不需要的文档（例如，若无重要算法则省略 `algorithm.md`）。

**每个 L2 文档中的每个章节都必须遵守以下规则：**
- 有代码指针：`[src/path/file.ts:10-45](src/path/file.ts#L10)`
- 最多包含接口签名（不含实现）
- 不描述尚不存在的代码

---

### `tech-arch.md` — 技术架构

```markdown
# 技术架构

## 服务拓扑
{服务如何连接——简单时用文字，复杂时用图表}

## 组件：{ComponentName}
**代码：** [src/services/component.ts:1-45](src/services/component.ts#L1)
**用途：** {一句话}
**接口：**
```typescript
// 仅签名——不含实现
export function doThing(input: InputType): Promise<OutputType>
export interface InputType { field: string; count: number }
```
**约束：** {性能预算、速率限制、重试策略、SLA}

## 组件：{ComponentName}
...

## 数据流
{数据如何流动：输入来源 → 处理步骤 → 输出目标}

## 基础设施
- **计算：** {K8s Deployment / Lambda / ECS task — 命名空间、副本数}
- **数据库：** {引擎、实例名、连接池方案}
- **消息队列：** {队列/主题名称、消费者组、DLQ 配置}
- **缓存：** {Redis/Memcached — TTL 策略、失效方案}

## 决策日志
| 决策 | 备选方案 | 理由 | 日期 |
|------|---------|------|------|
| 使用 PostgreSQL 持久化 | MySQL、MongoDB | 现有基础设施，PG18 标准 | {YYYY-MM-DD} |
| 使用 RocketMQ 处理事件 | Kafka、RabbitMQ | 符合平台标准 | {YYYY-MM-DD} |
```

---

### `business-logic.md` — 业务规则

```markdown
# 业务逻辑

## 领域：{DomainName}

### 规则：{RuleName}
**代码：** [src/domain/rules/rule-name.ts:23-45](src/domain/rules/rule-name.ts#L23)
**规则：** {一句话——执行什么}
**适用时机：** {触发此规则的条件}
**例外：** {规则不适用的边界情况}

### 规则：{RuleName}
...

### 不变量：{InvariantName}
**代码：** [src/domain/entity.ts:67](src/domain/entity.ts#L67)
**不变量：** {必须始终为真的内容——例如："订单总额必须等于行项目之和"}
**执行方式：** {验证层 / 数据库约束 / 两者 / 事件处理器}

## 用户旅程：{JourneyName}
| 步骤 | 描述 | 代码 |
|------|------|------|
| 1 | {用户操作或系统触发} | [handler.ts:10](src/handlers/handler.ts#L10) |
| 2 | {处理步骤} | [service.ts:45](src/services/service.ts#L45) |
| 3 | {输出或副作用} | [repository.ts:80](src/repositories/repository.ts#L80) |

**失败处理：**
- 步骤 2 失败：{发生什么——重试、补偿、通知}
- 步骤 3 失败：{发生什么}
```

---

### `solution-detail.md` — 实现细节

```markdown
# 实现细节

## 文件结构
```
src/
  {module}/
    {file}.ts           # {一行用途说明}
    {file}.ts           # {一行用途说明}
  {module}/
    {file}.ts           # {一行用途说明}
tests/
  {module}/
    {file}.test.ts      # {module} 的测试
```

## 组件：{ComponentName}
**代码：** [src/{path}/file.ts](src/{path}/file.ts)
**职责：** {一句话——做什么，不是怎么做}
**依赖：** {列出导入/调用的内容}
**消费者：** {列出导入/调用此组件的内容}

## 组件：{ComponentName}
...

## 配置
| 环境变量 | 用途 | 是否必需 | 默认值 |
|---------|------|---------|-------|
| `DATABASE_URL` | PostgreSQL 连接字符串 | 是 | — |
| `REDIS_HOST` | 缓存主机 | 否 | `localhost` |
| `ORDER_TIMEOUT_MS` | 最大订单处理时间 | 否 | `5000` |

## 已知注意事项
- {新智能体必须了解的非显而易见行为——例如："此端点按订单 ID 而非请求 ID 保证幂等性"}
- {非显而易见的约束——例如："PolarDB 读副本最多有 100ms 延迟——不要用于一致性敏感的读操作"}
```

---

### `algorithm.md` — 核心算法

*若任务没有重要的算法组件，则省略此文件。*

```markdown
# 核心算法

## 算法：{AlgorithmName}
**代码：** [src/core/algorithm.ts:10-89](src/core/algorithm.ts#L10)
**复杂度：** 时间 O({time})，空间 O({space})
**输入：** {类型和约束——例如："已排序的订单 ID 数组，最多 10,000 个元素"}
**输出：** {类型和语义}

**关键步骤：**
1. {步骤 1 发生什么——意图，而非代码}
2. {步骤 2 发生什么}
3. {步骤 3 发生什么}

**边界情况：**
| 情况 | 处理方式 | 代码位置 |
|------|---------|---------|
| 空输入 | 返回空结果 | [:L15](src/core/algorithm.ts#L15) |
| 重复键 | 后写覆盖 | [:L42](src/core/algorithm.ts#L42) |
| 溢出 | 抛出 `RangeError` | [:L67](src/core/algorithm.ts#L67) |
```

---

### `security.md` — 安全模型

```markdown
# 安全模型

## 认证
- **方式：** {JWT / session token / API key / mTLS}
- **代码：** [src/auth/middleware.ts:1-60](src/auth/middleware.ts#L1)
- **令牌有效期：** {持续时间和刷新策略}
- **存储：** {令牌存储位置——httpOnly cookie、Authorization header 等}

## 授权
| 端点 / 操作 | 所需角色 | 执行代码 |
|------------|---------|---------|
| `GET /api/orders` | `authenticated` | [routes/orders.ts:15](src/routes/orders.ts#L15) |
| `DELETE /api/orders/:id` | `admin` | [routes/orders.ts:42](src/routes/orders.ts#L42) |
| `POST /api/refunds` | `finance` | [routes/refunds.ts:20](src/routes/refunds.ts#L20) |

## 数据分类
| 数据类型 | 分类 | 静态加密 | 传输加密 | 访问范围 |
|---------|------|---------|---------|---------|
| 订单详情 | 机密 | 是（AES-256） | TLS 1.3 | 已认证用户 |
| 支付令牌 | 秘密 | 是（KMS） | TLS 1.3 | 仅限财务角色 |
| 员工用户名 | 内部 | 否 | TLS 1.3 | 已认证用户 |

## 威胁矩阵
| 威胁 | 缓解措施 | 代码 |
|------|---------|------|
| SQL 注入 | 通过 ORM 使用参数化查询 | [db/client.ts:23](src/db/client.ts#L23) |
| IDOR（跨用户访问） | 返回数据前检查所有者 | [services/order.ts:67](src/services/order.ts#L67) |
| 重放攻击 | 带 TTL 的幂等性键 | [middleware/idempotency.ts:5](src/middleware/idempotency.ts#L5) |
| 日志中的秘密 | 所有日志输出的 PII 脱敏器 | [utils/logger.ts:12](src/utils/logger.ts#L12) |
```

---

### `review-log.md` — 审查发现

输出格式详见 `expert-roles.md`。

---

## 文档生命周期规则

**任务开始时（第二阶段）：**
- 创建 `_INDEX.md` 和所有所需 L2 文档（仅章节标题，尚无代码指针——代码尚未存在）
- 将所有章节标记为 `[计划中 — 代码尚未存在]`

**执行期间（第三阶段）：**
- 每个工作包完成后，用真实代码指针替换 `[计划中]` 占位符
- 若实现过程中接口签名发生变化，更新接口签名
- 工作包在其文档章节拥有真实指针之前，视为**未完成**

**审查时（第四阶段）：**
- 审查者检查所有 `[计划中]` 标记是否已消除
- 已完成工作包中任何残留的 `[计划中]` = **阻断**发现

**功能移除时：**
- 删除文档章节——不要留下"已移除"注释
- 更新 `_INDEX.md` 工作包列表

**代码合并后发生变更时：**
- 若修改了文档章节覆盖的代码，更新指针和接口
- 这是保持文档长期准确的主要机制
