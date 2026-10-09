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

外部贡献者可先 fork 仓库。修改前读取 [AGENTS.md](../AGENTS.md)，按影响范围定位[仓库维护](../docs/仓库维护.md)、架构、模块和权威合同；普通措辞修正无需先展开全库文档。

若本机由 mise 管理 uv、由 uv 管理 Python，使用现有 mise 环境中的 uv 显式选择解释器：

```bash
uv run --python 3.14 python --version
uv run --python 3.14 python scripts/validate_longtask.py
uv run --python 3.14 python -m unittest discover -s tests -v
uv run --python 3.14 python scripts/run_skill_evals.py
uv run --python 3.14 python scripts/run_forward_evals.py --run
```

这与下面的直接 Python 命令等价。uv 尚未在当前目录激活时，可以通过 `mise exec uv@<已安装版本> -- uv run --python 3.14 python …` 按次调用；无需为此修改全局工具配置。参见 [uv 的解释器选择](https://docs.astral.sh/uv/guides/scripts/#using-different-python-versions)与 [mise exec](https://mise.jdx.dev/cli/exec.html)。

## 验证变更

仓库要求的四项检查：

开发期间先运行受影响的检查，在重要变更集成后执行完整检查。本地单元测试和前向合同使用可丢弃 fixture，不接触生产服务；可在已授权变更范围内运行、修复本次引入的失败并重跑相关检查，无需逐步申请批准。真实宿主采集、安装和发布遵循各自合同。

```bash
python3 scripts/validate_longtask.py
python3 -m unittest discover -s tests -v
python3 scripts/run_skill_evals.py
python3 scripts/run_forward_evals.py --run
```

这些源码检查不依赖运行结果。文件归属、产物忽略及测试取舍按 [AGENTS.md](../AGENTS.md#源码与产物架构规范) 执行。需要核验本地证据时运行 `python3 scripts/validate_longtask.py --with-evaluation-results`，它要求当前调用、前向、记忆及宿主记录，缺失或过期即失败。

如果随包文件或合同变化，已保存的摘要可能过期。需要保存或验证证据时，实际重跑并更新本地记录：

```bash
# 执行确定性前向套件并保存本次结果
python3 scripts/run_forward_evals.py --write
# 重建本地候选，避免 dist 中旧归档与源码不一致
python3 scripts/build_release.py build --root . --output dist/longtask-4.0.0.zip
python3 scripts/build_release.py verify --root . --archive dist/longtask-4.0.0.zip
```

发现元数据变化须按[调用评测协议](../references/评测协议.md)重新采样，不手动重绑旧分类。宿主结果必须保留真实的保证级别：没有在当前归档上采样的场景为 `unable_to_verify`，发布门保持阻塞。评测记录的更新不等于真实执行通过。

技能精简需同时核对描述的触发边界、模式资料加载与完整验收。文本体积对照只能支持字符或字节变化的声明；真实工具调用、重复读取、停止行为和成本需从宿主 trace 独立判断，不能用固定措辞检查代替。

CI 检查语料和源码并执行当前确定性合同测试，不依赖本地结果文件；它不会替你生成独立调用分类、宿主样本或正式发布证据。正式发布另外运行 `python3 scripts/validate_longtask.py --require-release-pass`，发布合同见[发布与恢复](../references/发布与恢复.md)。

## 提交 Pull Request

保持单个明确目的，说明用户可见的问题、变更后的行为、执行过的验证与剩余限制。重要变更同步架构和受影响模块，并按 [AGENTS.md](../AGENTS.md#仓库变更)进行风险审查；细则引用对应权威文件。

提交说明使用简短的 `docs:`、`fix:`、`feat:`、`test:` 或 `ci:` 前缀即可，无需为文档修改提升技能版本。不要包含评测结果、trace、快照、`.longtask/`、`dist/`、个人配置或秘密；保留无关改动。按[行为准则](CODE_OF_CONDUCT.md)参与讨论。

贡献将以仓库的 [MIT 许可证](../LICENSE)分发。个人维护项目不承诺固定响应时间。

## 自动生成版本候选

更新全部版本元数据并提交后，创建与 `release-manifest.json` 一致的新版本标签，再推送分支和标签，例如：

```bash
git tag -a v4.1.0 -m 'longtask 4.1.0'
git push origin main v4.1.0
```

标签对应提交必须包含候选发布工作流。GitHub Actions 自动执行检查、打包并上传 ZIP 与校验文件到候选预发布；普通 commit 不生成 Release。完整条件、失败恢复及正式发布边界见[发布合同](../references/发布与恢复.md#github-候选预发布)。
