# 贡献指南

感谢你帮助改进 longtask。项目面向个人 Codex 使用，优先接受能改善真实恢复、验收与维护体验的聚焦变更。

## 报告问题

使用 [Issue 表单](https://github.com/J-ChenX/longtask/issues/new/choose)，说明版本、Codex/Python 环境、最小复现、预期与实际行为。提交前搜索已有问题；日志和状态样本请脱敏。漏洞请使用[安全政策](SECURITY.md)中的私下渠道。

功能建议请描述具体任务及验收结果。新的宿主兼容、状态迁移或发布机制会改变项目边界，需要先明确意图；当前边界见 [README](../README.md#兼容性与验证边界)。

## 开发环境

需要 Git 与 Python 3.14+；项目运行时与测试使用标准库，无需安装第三方 Python 包。

```bash
git clone https://github.com/J-ChenX/longtask.git
cd longtask
git switch -c codex/your-change
```

外部贡献者可先 fork 仓库。修改前读取 [AGENTS.md](../AGENTS.md)与[仓库维护](../docs/仓库维护.md)，再按影响范围阅读架构、模块和权威合同。

若本机由 mise 管理 uv、由 uv 管理 Python，使用现有 mise 环境中的 uv 显式选择解释器：

```bash
uv run --python 3.14 python --version
uv run --python 3.14 python scripts/validate_longtask.py
uv run --python 3.14 python -m unittest discover -s tests -v
uv run --python 3.14 python scripts/run_skill_evals.py --results evals/invocation_results.json
uv run --python 3.14 python scripts/run_forward_evals.py
```

这与下面的直接 Python 命令等价。uv 尚未在当前目录激活时，可以通过 `mise exec uv@<已安装版本> -- uv run --python 3.14 python …` 按次调用；无需为此修改全局工具配置。参见 [uv 的解释器选择](https://docs.astral.sh/uv/guides/scripts/#using-different-python-versions)与 [mise exec](https://mise.jdx.dev/cli/exec.html)。

## 验证变更

仓库要求的四项检查：

```bash
python3 scripts/validate_longtask.py
python3 -m unittest discover -s tests -v
python3 scripts/run_skill_evals.py --results evals/invocation_results.json
python3 scripts/run_forward_evals.py
```

如果随包文件或合同变化，已保存的摘要可能过期。先实际重跑并更新对应证据，再执行上述检查：

```bash
# 执行确定性前向套件并保存本次结果
python3 scripts/run_forward_evals.py --write
# 重建本地候选，避免 dist 中旧归档与源码不一致
python3 scripts/build_release.py build --root . --output dist/longtask-3.0.0.zip
python3 scripts/build_release.py verify --root . --archive dist/longtask-3.0.0.zip
```

发现元数据变化须按[调用评测协议](../references/评测协议.md)重新采样，不手动重绑旧分类。宿主结果必须保留真实的保证级别：没有在当前归档上采样的场景为 `unable_to_verify`，发布门保持阻塞。评测记录的更新不等于真实执行通过。

CI 校验已保存的绑定并重跑本地合同测试；它不会替你生成独立调用分类、宿主样本或正式发布证据。正式发布另外运行 `python3 scripts/validate_longtask.py --require-release-pass`，发布合同见[发布与恢复](../references/发布与恢复.md)。

## 提交 Pull Request

保持单个明确目的，说明用户可见的问题、变更后的行为、执行过的验证与剩余限制。重要变更同步架构和受影响模块，并按 [AGENTS.md](../AGENTS.md#仓库变更)进行风险审查；细则引用对应权威文件。

提交说明使用简短的 `docs:`、`fix:`、`feat:`、`test:` 或 `ci:` 前缀即可，无需为文档修改提升技能版本。不要包含 `.longtask/`、`dist/`、个人配置或秘密；保留无关改动。按[行为准则](CODE_OF_CONDUCT.md)参与讨论。

贡献将以仓库的 [MIT 许可证](../LICENSE)分发。个人维护项目不承诺固定响应时间。
