---
name: longtask-retrofit
description: 为既无可用 longtask 检查点又无持久架构的已有代码库建立当前知识与恢复状态。
license: MIT
compatibility: Designed for Codex. Requires Python 3.14+; install the complete longtask plugin.
metadata:
  version: "3.0.0"
---

# Longtask 既有项目接入

激活时说明正在使用 longtask-retrofit。复用[主技能](../../SKILL.md)，从当前目标限定接入范围；读取相关入口、文档、代码与测试，只有合同或来源缺口才深入历史，不先做全库盘点。

## 接入结果

从用户及持久来源恢复有边界的目标，以 `retrofit` 初始化 v3 检查点；根据真实职责与依赖建立最小架构/模块合同。创建知识时查[文档架构](../../文档架构.md)，变更状态时查[状态协议](../../references/状态协议.md)相关章节。

代码/运行行为只能标为 `observed` 并绑定版本；有依据但未确认的意图为 `inferred`，冲突为 `disputed`。用户陈述、决策等支持的已批准意图保持其权威；不得用当前代码覆盖目标。目标确实改变时用 `goal` 命令协调。

接入所需验收、来源及文档缺口保持可见；无关改进不扩入范围，除非另有授权不重构代码。仅接入请求按该范围验收并遵循[完成合同](../../SKILL.md#变更与完成)；接入若是已授权实施的前置步骤，继续原任务主线。
