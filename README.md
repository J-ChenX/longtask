# longtask

面向 Codex 个人使用的长任务技能，用于必须跨越上下文重置、跨会话保留决策，或协调多个可独立验证工作包的复杂编码任务。运行时要求 Python 3.10 或更高版本。

## 3.0.0 边界

3.0.0 是破坏性版本：只支持 Codex，不包含 Claude Code 清单、规则或验证路径；不读取、不迁移 1.x/2.x 状态，也不提供跨版本回滚。旧任务记录不作为新工作的恢复来源；先将仍有效的项目事实合并到当前文档，再按当前目标初始化检查点。故障恢复使用当前 3.0.0 受信归档、工作区备份和同版本完整重装。

## 设计

- 持久文档只记录当前项目目标、功能、合同、决策、限制和验证方法；
- 单份 `.longtask/` 临时检查点支持未完成工作的跨会话恢复，收尾后清除；
- Git 版本和 SHA-256 摘要绑定批准、审查和完成声明；
- 授权策略决定何时继续、何时询问；
- 确定性测试和行为评测衡量路由与恢复质量。

代码描述已观察行为；已批准目标和合同描述预期行为。冲突在协调前始终保持可见。

## 用户可用的行为流程

用户提出目标 → 读取当前架构和相关模块 → 明确变更与验收 → 实现并更新项目内容 → 验证与独立审查 → 清除临时检查点。

中断时从唯一检查点恢复未完成工作；已经完成的功能直接从模块文档和测试理解，不需要查找原任务。架构变更会重新检查依赖与合同，审查请求默认只读。项目长期保留“现在有什么、如何工作、如何验证”，不保留“哪项任务、哪个智能体、经过几轮才完成”。

详细的可执行生命周期、失败恢复、架构变更及 worktree 用法见[操作示例](references/操作示例.md)。日常可用 `doctor` 查看恢复缺口，用 `--output summary` 减少状态历史输出。

新项目只需先保存目标，再逐步形成计划并更新交接；具体命令见[初始化与交接](references/初始化与交接.md)。只规划时保留断点，应用仍未实施。

## 入口技能

| 场景 | 技能 |
|---|---|
| 自动状态/能力路由 | [`longtask`](SKILL.md) |
| 新项目 | [`longtask-setup`](skills/longtask-setup/SKILL.md) |
| 中断/压缩后恢复 | [`longtask-continue`](skills/longtask-continue/SKILL.md) |
| 独立只读审查 | [`longtask-review`](skills/longtask-review/SKILL.md) |
| 架构/工作流变更 | [`longtask-modify`](skills/longtask-modify/SKILL.md) |
| 既无检查点、又无持久架构的已有代码 | [`longtask-retrofit`](skills/longtask-retrofit/SKILL.md) |

六个入口由 `.codex-plugin/plugin.json` 的原生 `./skills/` 根统一发现，并作为一个资源闭合插件发布。生成文档的分层和固定文件名边界见[文档架构](文档架构.md)。

## 校验

```bash
python3 scripts/build_release.py build --root . --output dist/longtask-3.0.0.zip
python3 scripts/build_release.py verify --root . --archive dist/longtask-3.0.0.zip
python3 scripts/validate_longtask.py
python3 -m unittest discover -s tests -v
python3 scripts/run_skill_evals.py --results evals/invocation_results.json
python3 scripts/run_forward_evals.py
# 仅发布流水线；Codex 宿主或当前版本恢复门未通过时返回非零
python3 scripts/validate_longtask.py --require-release-pass
```

`release-manifest.json` 是发布资源闭包的唯一清单。构建器固定文件顺序、时间戳、权限和存储方式，并在归档内写入逐文件 SHA-256 清单。`dist/` 是可清除的构建输出，不纳入 Git。普通静态校验在没有输出时使用临时构建，有输出时检查其与源码一致；发布门要求实际候选归档。schema、文档和哈希绑定评测始终校验。

提取后的插件使用 `python3 scripts/validate_longtask.py --installed` 自检；该结果与源码的真实宿主发布门分开报告。

## 安装与恢复

个人安装必须先移出同名旧独立技能，再从已验证的 `dist/longtask-3.0.0.zip` 提取完整插件并通过默认个人 marketplace 安装；不要复制可变工作区或单独复制某个嵌套技能。发布方须通过受信渠道提供外部 SHA-256，安装位置、marketplace 条目、缓存刷新和恢复命令统一见[发布与恢复](references/发布与恢复.md)。

上线门只接受当前归档绑定的 Codex 全新会话矩阵和当前版本恢复演练。Codex 本地 manifest 预检不能代替真实宿主工作流。详细步骤见[发布与恢复](references/发布与恢复.md)。

技能格式遵循 [Agent Skills 规范](https://agentskills.io/specification)，Codex 插件清单由 Codex 定义。项目使用 MIT 许可证。

发布验收区分核心可用性与收益：`release_gate` 保留真实行为、授权、版本绑定和恢复要求；`benchmark_gate` 单独判断对照与性能数据。查看[发布与恢复](references/发布与恢复.md)；`--require-release-pass` 会逐项列出尚缺的核心验收。易变评测运行结果保留在源码目录，不随插件安装。
