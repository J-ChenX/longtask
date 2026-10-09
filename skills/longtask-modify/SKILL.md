---
name: longtask-modify
description: 变更已建立 longtask 项目的架构、模块合同或关键技术；普通功能编辑不启用。
license: MIT
compatibility: Designed for Codex. Requires Python 3.14+; install the complete longtask plugin.
metadata:
  version: "3.0.0"
---

# Longtask 架构修改

直接激活时简述 longtask-modify；从主入口转入无需重复宣告。复用[主技能](../../SKILL.md)和当前目标，只补读受影响模块与决策。

## 变更边界

明确受影响依赖、写集、批准与审查证据，以及迁移/回滚出口。沿用有效授权；状态或证据失效不撤销范围内实施授权。

无检查点时，按当前文档和用户目标初始化 `modify`；已有检查点时执行：

```text
python3 {skill-root}/scripts/longtask_state.py modify --root {workspace-root} --expected-task-id {id} --expected-revision {n} --reason {reason} --package-id {affected-id}
```

多个包重复传 `--package-id`，无包时可省略。命令进入 modify/architecture 并冻结指定包及其传递消费者，不用改写 goal 或重新 init 绕过切换。

按[状态协议](../../references/状态协议.md)协调：新架构获批后显式 supersede 冻结包，用新 ID 重建合同。只有输入和写集彼此独立时，才继续未受影响包。

## 验收结果

更新唯一权威架构、决策和受影响模块，按[文档变更规则](../../文档架构.md#变更同步)修复消费者。实现新合同，验证真实链路、迁移及回滚；义务满足后删除被取代路径。

按风险完成[独立审查](../../专家审查协议.md)，证据绑定最终集成版本。分支与交付切片按需查[任务推进](../../references/任务推进.md)，全部当前验收满足后遵循[完成合同](../../SKILL.md#变更与完成)。
