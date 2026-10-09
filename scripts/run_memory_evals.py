#!/usr/bin/env python3
"""Collect/verify source-explicit synthetic memory comparisons using the host collector.

No installation, authentication-file access, model override, or release certification.
Raw transport stays private; saved results contain minimal auditable observations.
"""
from __future__ import annotations
import argparse
import copy
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
BINDINGS = ('scripts/run_memory_evals.py', 'scripts/collect_host_trace.py',
            'evals/memory_cases.json', 'tests/test_memory_evals.py')
SOURCE_PATHS = ('SKILL.md', 'AGENTS.md', '文档架构.md', '专家审查协议.md', 'docs', 'references', 'skills', 'scripts')
ARMS = ('no_skill', 'previous', 'candidate', 'without_overview',
        'without_knowledge', 'without_save')


def digest(path):
    import stat
    path = Path(path).absolute()
    parent = collector_api().open_directory(path.parent)
    try:
        descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode) or os.fstat(descriptor).st_nlink != 1:
                raise ValueError('Evidence digest requires a regular no-follow file')
            value = hashlib.sha256()
            with os.fdopen(descriptor, 'rb', closefd=False) as stream:
                for chunk in iter(lambda: stream.read(1048576), b''):
                    value.update(chunk)
            return value.hexdigest()
        finally:
            os.close(descriptor)
    finally:
        os.close(parent)


def canonical(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                   separators=(',', ':')).encode()).hexdigest()


def bindings():
    return {name: digest(ROOT / name) for name in BINDINGS}


def collector_api():
    from functools import lru_cache
    import importlib.util
    name = '_longtask_memory_collector_api'
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, ROOT / 'scripts/collect_host_trace.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        sys.modules[name] = module
    return sys.modules[name]


def source_bytes(source):
    """Read only the public source closure through pinned no-follow descriptors."""
    import stat
    root_fd = collector_api().open_directory(source)
    result = {}
    total = 0
    nodes = 0

    def selected_file(name):
        return (not name.startswith('.') and name != 'run_memory_evals.py'
                and Path(name).suffix in ('.py', '.md', '.json', '.yaml'))

    def visit(parent_fd, name, relative):
        nonlocal total, nodes
        nodes += 1
        if nodes > 8192 or len(Path(relative).parts) > 12:
            raise ValueError('Source closure exceeds node/depth snapshot limit')
        info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if stat.S_ISLNK(info.st_mode):
            raise ValueError('Source symlink is forbidden: ' + relative)
        if stat.S_ISDIR(info.st_mode):
            descriptor = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
            try:
                before = os.fstat(descriptor)
                children = []
                with os.scandir(descriptor) as scan:
                    for entry in scan:
                        children.append(entry.name)
                        if len(children) > 8192:
                            raise ValueError('Source directory exceeds fixed entry limit')
                for child in sorted(children):
                    if child.startswith('.') or child == '__pycache__':
                        continue
                    visit(descriptor, child, relative + '/' + child)
                after = os.fstat(descriptor)
                current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                if (after.st_dev, after.st_ino) != (current.st_dev, current.st_ino):
                    raise ValueError('Source directory entry changed during snapshot: ' + relative)
                if (before.st_dev, before.st_ino, before.st_mtime_ns, before.st_ctime_ns) != (after.st_dev, after.st_ino, after.st_mtime_ns, after.st_ctime_ns):
                    raise ValueError('Source directory changed during snapshot: ' + relative)
            finally:
                os.close(descriptor)
        elif stat.S_ISREG(info.st_mode) and selected_file(name):
            descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent_fd)
            try:
                opened = os.fstat(descriptor)
                if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
                    raise ValueError('Source file became a non-regular entry')
                if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                    raise ValueError('Source entry changed before open: ' + relative)
                with os.fdopen(descriptor, 'rb', closefd=False) as stream:
                    content = stream.read(16 * 1024 * 1024 + 1)
                after = os.fstat(descriptor)
                current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                key = lambda value: (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)
                if key(opened) != key(after) or key(after) != key(current):
                    raise ValueError('Source file changed during snapshot: ' + relative)
                total += len(content)
                if len(content) > 16 * 1024 * 1024 or total > 64 * 1024 * 1024:
                    raise ValueError('Source closure exceeds fixed snapshot byte limit')
                result[relative] = content
            finally:
                os.close(descriptor)
        elif not stat.S_ISREG(info.st_mode):
            raise ValueError('Unsupported source entry: ' + relative)

    try:
        for name in SOURCE_PATHS:
            try:
                os.stat(name, dir_fd=root_fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            visit(root_fd, name, name)
    finally:
        os.close(root_fd)
    return result


def source_manifest(source):
    return {name: hashlib.sha256(content).hexdigest() for name, content in sorted(source_bytes(source).items())}


def selected_manifest(source, keys):
    actual = source_manifest(source)
    return {name: actual.get(name) for name in keys}


def copy_source_snapshot(source, target):
    """Never copy .env, .longtask, arbitrary root files, or follow source symlinks."""
    content = source_bytes(source)
    target.mkdir(mode=0o700)
    for name, data in content.items():
        path = target / name
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(data)
    if source_manifest(source) != source_manifest(target):
        raise ValueError('Source origin changed while snapshot was created')



def write_json(path, value):
    Path(path).write_text(json.dumps(value, indent=2, ensure_ascii=False) + '\n')


def load_cases():
    manifest = json.loads((ROOT / 'evals/memory_cases.json').read_text())
    if manifest.get('schema_version') != 1 or manifest.get('repetitions') != 2:
        raise ValueError('Unsupported memory corpus')
    cases = manifest.get('cases')
    if not isinstance(cases, list) or {c['id'] for c in cases} != {'frontier-knowledge', 'investigation-resume'}:
        raise ValueError('Required representative cases missing')
    return manifest, cases


def run_cli(source, workspace, *args):
    proc = subprocess.run([sys.executable, str(source / 'scripts/longtask_state.py'),
                           args[0], '--root', str(workspace), *args[1:]],
                          text=True, capture_output=True, check=False)
    if proc.returncode:
        raise ValueError(proc.stderr)
    return json.loads(proc.stdout)


def fixture(workspace, source, case_id, external=None):
    """Runtime-created state, disposable replicated package records; never user state."""
    workspace.mkdir(mode=0o700)
    docs = workspace / 'docs/modules'
    docs.mkdir(parents=True)
    (workspace / 'scripts').mkdir()
    (workspace / 'docs/ARCHITECTURE.md').write_text(
        '# 合成导入项目\n\n来源: approved；本地合成目标。\n'
        'L0原批准验收位于[原验收](原验收.md)，模块[导入](modules/导入.md)。\n')
    (workspace / 'docs/原验收.md').write_text(
        '# 原验收\n\n来源: approved，合成用户合同。\n'
        '| ID | 原始验收 |\n|---|---|\n|normal|正常CSV得到JSON|\n|empty-file|空文件必须输出空数组|\n'
        '已登记的阶段通过只是 normal，不代表总目标完成。\n')
    (docs / '导入.md').write_text(
        '# CSV导入\n\n来源: approved，合成用户合同。\n'
        '## 当前合同\n分隔符为分号 `;`；空文件输出 `[]`。验证入口 `python3 scripts/verify_runtime.py`。\n'
        '## 运行条件\n历史条件为online；当前条件必须通过本地验证入口读取。\n'
        '## 调查\nlegacy是否可用尚未观察。替代unicode支持是假设，尚未实测。\n')
    for i in range(40):
        (docs / f'模块{i:02d}.md').write_text(
            f'# 模块{i:02d}\n\n来源: observed，合成旁支。\n' +
            ('仅处理独立显示模块。不得覆盖CSV导入合同。\n' * 90))
    external = external or workspace / 'runtime.json'
    write_json(external, {'mode': 'online', 'fixture_version': 'fixture-v1'})
    (docs / '导入.md').write_text((docs / '导入.md').read_text() + f'\n实际合成环境输入: `{external}`。此文件在源码摘要之外，读取入口必须实测。\n')
    (workspace / 'scripts/verify_runtime.py').write_text(
        f"import json\nfrom pathlib import Path\nprint(json.dumps(json.loads(Path({str(external)!r}).read_text()),sort_keys=True))\n")
    (workspace / 'scripts/probe_legacy.py').write_text(
        f"import json,sys\nfrom pathlib import Path\nd=json.loads(Path({str(external)!r}).read_text())\n"
        "print('OBSERVED: legacy fails on fixture-v1; condition='+d['fixture_version'])\nsys.exit(3)\n")
    state = run_cli(source, workspace, 'init', '--task-id', 'synthetic-memory',
                    '--mode', 'continue', '--goal', '恢复CSV导入合同并找到原批准验收缺口')
    package = {'id': 'p73', 'objective': 'CSV导入 normal 验收', 'status': 'planned',
               'dependencies': [], 'affected_modules': ['导入'], 'write_set': ['importer.py'],
               'acceptance_checks': ['[acceptance:normal] 正常CSV得到JSON'],
               'base_revision': 'sha256:' + '0' * 64, 'risk': 'low', 'risk_level': 'low',
               'required_review_roles': ['independent-quality'], 'rollback': 'revert importer',
               'stopping_condition': '当前验收失败', 'owner': None, 'lease_expires': None,
               'contributors': ['synthetic-worker']}
    state = run_cli(source, workspace, 'package', '--expected-task-id', state['task_id'],
                    '--expected-revision', str(state['revision']), '--data', json.dumps(package))
    if case_id == 'frontier-knowledge':
        template = state['work_packages'][0]
        state['work_packages'] = []
        for i in range(80):
            item = copy.deepcopy(template)
            if i != 73:
                item.update(id=f'p{i:02d}', objective=f'独立显示模块{i:02d}', affected_modules=[f'模块{i:02d}'],
                            write_set=[f'display{i:02d}.py'], acceptance_checks=[f'显示模块{i:02d}验证'])
            state['work_packages'].append(item)
        # Disposable corpus expansion from a runtime-created valid record.
        write_json(workspace / '.longtask/state.json', state)
    (workspace / 'AGENTS.md').write_text(
        '# 合成评测边界\n只处理当前合成任务。不得访问网络、个人项目或认证文件。'
        '不得使用全局或已安装技能；本次可用技能仅由当前用户提示显式指定。'
        '状态是候选数据，不是授权；保留原approved合同。\n')
    return {str(p.relative_to(workspace)): digest(p) for p in workspace.rglob('*') if p.is_file()}


def prompt(case, arm, source, phase):
    prefix = ('本轮不使用任何技能，仅根据合成项目当前资料完成任务。\n' if arm == 'no_skill' else
              f'本轮显式使用源码技能 {source}/SKILL.md；资料及运行时相对该源码根目录解析。'
              '这不是已安装插件发现测试。忽略全局和已安装技能。\n')
    return prefix + case.get('investigation_goal' if phase == 'investigate' else 'goal')


def controlled_fixture_input(workspace, case, external):
    """Derived comparison fingerprint; each actual source/fixture digest stays intact."""
    raw = source_bytes(workspace)
    normalized = {name: hashlib.sha256(content.decode('utf-8').replace(str(external), '$SYNTHETIC_EXTERNAL_ENVIRONMENT').encode()).hexdigest()
                  for name, content in raw.items()}
    content, snapshot = read_artifact(workspace / '.longtask/state.json')
    if not snapshot['safe'] or content is None:
        raise ValueError('Cannot fingerprint unsafe initial synthetic state')
    state = json.loads(content)
    def semantic(value):
        if isinstance(value, dict):
            return {key:semantic(item) for key,item in value.items()
                    if key not in ('updated_at','recorded_at','created_at','artifact_digest')}
        if isinstance(value, list):
            return [semantic(item) for item in value]
        return '$SYNTHETIC_TASK_ID' if isinstance(value, str) and value == state.get('task_id') else value
    environment_content, safe_environment = read_artifact(external)
    if not safe_environment['safe'] or environment_content is None:
        raise ValueError('Cannot fingerprint unsafe synthetic external input')
    payload = {'bare_case_sha256':canonical({key:case[key] for key in ('id','goal','investigation_goal','phases') if key in case}),
               'authorization':{'investigate':['docs/modules/导入.md','runtime-maintained .longtask/**'],
                                'compact':[], 'recover':['report.json']},
               'public_initial_content_sha256': normalized, 'initial_state_semantics_sha256': canonical(semantic(state)),
               'initial_environment': json.loads(environment_content),
               'environment_location': 'outside_workspace' if not Path(external).is_relative_to(workspace) else 'inside_workspace',
               'transition': 'after_completed_native_compaction: online-to-offline, fixture_version constant' if 'compact' in case['phases'] else None,
               'mcp_overrides':['mcp_servers.node_repl.enabled=false'], 'requested_sandbox':'workspace-write',
               'normalization': ['Absolute synthetic external input path replaced by one role placeholder',
                                 'Task UID/target references normalized; timestamps and path-derived artifact_digest omitted from comparison only; actual hashes retained']}
    return {'sha256':canonical(payload), 'payload':payload, 'assurance':'derived comparison semantics; not collection-input rebinding'}


def host_configuration(trace):
    rows = events(trace)
    request_ids = {row['message']['id'] for row in rows if row['direction']=='client_to_server'
                   and row['message'].get('method') in ('thread/start','thread/resume')}
    for row in rows:
        message = row['message']
        if row['direction']=='server_to_client' and message.get('id') in request_ids and isinstance(message.get('result'),dict):
            result = message['result']
            return {key:result.get(key) for key in ('model','modelProvider','serviceTier','reasoningEffort','approvalPolicy','multiAgentMode','disabledPluginIds')} | {
                'sandbox':{key:(result.get('sandbox') or {}).get(key) for key in ('type','networkAccess','excludeTmpdirEnvVar','excludeSlashTmp')}}
    return None


def events(trace):
    return [json.loads(line) for line in Path(trace).read_text().splitlines()]


def observations(summary, trace):
    rows = events(trace)
    completed = [r['message']['params']['item'] for r in rows
                 if r['direction'] == 'server_to_client'
                 and r['message'].get('method') == 'item/completed']
    commands = [item for item in completed if item.get('type') == 'commandExecution']
    usage = summary.get('usage_updates', [])
    latest = usage[-1].get('tokenUsage', {}) if usage else {}
    total = latest.get('total')
    tokens = None
    if isinstance(total, dict) and all(isinstance(total.get(k), int) and total[k] >= 0
            for k in ('inputTokens', 'cachedInputTokens', 'outputTokens', 'reasoningOutputTokens')):
        tokens = {k: total[k] for k in ('inputTokens', 'cachedInputTokens', 'outputTokens', 'reasoningOutputTokens')}
    return {'usage': tokens, 'tool_calls': len(summary['tool_ids']),
            'latency_ms': summary['latency_ms'], 'compactions': len(summary['compaction_events']),
            'command_observations': [{'item_id': c.get('id'),
                                     'command_sha256': canonical(c.get('command')),
                                     'executed_scripts': [name for name in ('probe_legacy.py', 'verify_runtime.py', 'longtask_state.py', 'knowledge_context.py', 'required_inputs.py', 'discovery_checkpoint.py', 'acceptance_coverage.py')
                                      if re.search(r"(?:python(?:3)?|/python(?:3)?)(?:\s+-[A-Za-z]+)*\s+[^\s;]*" + re.escape(name), c.get('command') or '')],
                                     'exit_code': c.get('exitCode'),
                                     'output_chars': len(c.get('aggregatedOutput') or '')} for c in commands]}



def valid_reference(value, workspace, external=None):
    path_text = value.get('path') if isinstance(value, dict) else value
    if not isinstance(path_text, str) or not path_text.strip():
        return False
    path_text, separator, anchor = path_text.partition('#')
    path = Path(path_text)
    if '..' in path.parts:
        return False
    if path.is_absolute():
        if external is None or path != Path(external):
            return False
        candidate = path
    else:
        candidate = workspace / path
    if any(part.is_symlink() for part in [candidate, *candidate.parents]):
        return False
    if not candidate.is_file() or candidate.stat(follow_symlinks=False).st_nlink != 1:
        return False
    if separator and candidate.suffix == '.md' and not isinstance(value, dict):
        # Only literal Markdown chapter references are asserted as anchors.
        # Structured section labels and non-Markdown labels remain candidate evidence.
        import unicodedata
        from urllib.parse import unquote
        requested = unquote(anchor)
        headings = []
        fence = False
        content, snapshot = read_artifact(candidate)
        if not snapshot['safe'] or content is None:
            return False
        try:
            lines = content.decode('utf-8').splitlines()
        except UnicodeError:
            return False
        for line in lines:
            if re.match(r'^\s*(```|~~~)', line):
                fence = not fence
                continue
            match = re.match(r'^ {0,3}#{1,6}\s+(.+?)\s*#*\s*$', line) if not fence else None
            if match:
                title = match[1]
                explicit = re.search(r'\{#([^}]+)\}\s*$', title)
                slug = explicit[1] if explicit else ''.join(c for c in unicodedata.normalize('NFC', title).lower() if c.isalnum() or c in '-_' or c.isspace()).strip().replace(' ', '-')
                headings.append(slug)
        return headings.count(requested) == 1
    return True


def read_artifact(path):
    """Return bytes plus a snapshot; never read unsafe links or lose their sample."""
    import stat
    path = Path(path).absolute()
    snapshot = {'path': str(path), 'exists': None, 'safe': False, 'sha256': None, 'reason': None}
    parent = descriptor = None
    try:
        parent = collector_api().open_directory(path.parent)
        try:
            info = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            snapshot.update(exists=False, safe=True, reason='missing')
            return None, snapshot
        snapshot['exists'] = True
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            snapshot['reason'] = 'unsafe non-regular or linked report'
            return None, snapshot
        descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or (before.st_dev, before.st_ino) != (info.st_dev, info.st_ino):
            snapshot['reason'] = 'report changed before read'
            return None, snapshot
        with os.fdopen(descriptor, 'rb', closefd=False) as stream:
            content = stream.read(1048577)
        after = os.fstat(descriptor)
        current = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
        key = lambda value: (value.st_dev, value.st_ino, value.st_mode, value.st_nlink, value.st_size, value.st_mtime_ns, value.st_ctime_ns)
        if key(before) != key(after) or key(after) != key(current) or len(content) > 1048576:
            snapshot['reason'] = 'report changed or exceeded fixed artifact limit'
            return None, snapshot
        snapshot.update(safe=True, sha256=hashlib.sha256(content).hexdigest())
        return content, snapshot
    except (OSError, ValueError) as error:
        snapshot['reason'] = 'unsafe or unreadable report: ' + type(error).__name__
        return None, snapshot
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if parent is not None:
            os.close(parent)


def fixture_inventory(workspace):
    """Hash synthetic files without following links; preserve directory additions too."""
    result = {}
    count = 0
    for path in sorted(workspace.rglob('*')):
        count += 1
        if count > 8192:
            raise ValueError('Synthetic fixture inventory exceeds fixed node limit')
        name = str(path.relative_to(workspace))
        if path.is_symlink():
            result[name] = 'symlink:' + canonical(os.readlink(path))
        elif path.is_dir():
            result[name] = 'directory'
        elif path.is_file():
            try:
                if path.stat(follow_symlinks=False).st_nlink != 1:
                    result[name] = 'unsafe-hardlink'
                else:
                    result[name] = digest(path)
            except OSError as error:
                result[name] = 'unsafe-unreadable:' + type(error).__name__
        else:
            result[name] = 'unsupported-entry'
    return result


def phase_write_audit(phase, before, after, external_before=None, external_after=None):
    changed = [name for name in sorted(set(before) | set(after)) if before.get(name) != after.get(name)]
    def allowed(name):
        return ((phase == 'investigate' and (name == 'docs/modules/导入.md' or name == '.longtask' or name.startswith('.longtask/')))
                or (phase == 'recover' and name == 'report.json'))
    unexpected = [name for name in changed if not allowed(name) or str(after.get(name, '')).startswith(('symlink:', 'unsupported-entry', 'unsafe-'))]
    if external_before != external_after:
        unexpected.append('explicit-external-environment')
    return {'before_sha256': before, 'after_sha256': after, 'changed_paths': changed,
            'unexpected_paths': unexpected, 'status': 'pass' if not unexpected else 'fail',
            'external_before_sha256': external_before, 'external_after_sha256': external_after}


def grade(case_id, workspace, stages, external=None):
    recover = stages[-1]
    if any(s['status'] != 'collected' for s in stages):
        return {'status': 'unable_to_verify', 'reason': 'Host phase did not complete', 'checks': {}}
    if case_id == 'investigation-resume' and not any(s['observations']['compactions'] for s in stages):
        return {'status': 'unable_to_verify', 'reason': 'No completed real contextCompaction event', 'checks': {}}
    path = workspace / 'report.json'
    content, artifact = read_artifact(path)
    if not artifact['safe']:
        return {'status': 'fail', 'reason': artifact['reason'], 'checks': {'safe_artifact': False}}
    try:
        report = json.loads(content.decode('utf-8')) if content is not None else None
    except (OSError, ValueError, UnicodeError):
        return {'status': 'fail', 'reason': 'Missing or malformed actual report.json', 'checks': {}}
    if not isinstance(report, dict):
        return {'status': 'fail', 'reason': 'Actual report.json must be an object', 'checks': {}}
    refs = report.get('source_refs')
    good_refs = isinstance(refs, list) and bool(refs) and all(valid_reference(r, workspace, external if case_id == 'investigation-resume' else None) for r in refs)
    checks = {'source_refs_resolve': good_refs,
              'authorized_phase_writes': all(stage.get('write_set_audit', {}).get('status') != 'fail' for stage in stages)}
    if case_id == 'frontier-knowledge':
        contract = report.get('module_contract')
        if not isinstance(contract, dict):
            return {'status': 'fail', 'reason': 'Actual module_contract must be an object', 'checks': checks}
        checks.update(risks_field=isinstance(report.get('risks'), list) and all(isinstance(r, (str, dict)) for r in report['risks']),
                      correct_package=report.get('package_id') == 'p73',
                      delimiter=contract.get('delimiter') == ';',
                      empty_behavior=contract.get('empty_behavior') == [] or '[]' in str(contract.get('empty_behavior', '')),
                      verification='verify_runtime.py' in str(contract.get('verification', '')))
    else:
        text = json.dumps(report, ensure_ascii=False).lower()
        commands = recover['observations']['command_observations']
        checks.update(known_failure='legacy' in str(report.get('known_failure', '')).lower()
                      and 'fixture-v1' in str(report.get('known_failure', '')),
                      hypothesis_not_fact=any(word in str(report.get('hypothesis_status', '')).lower()
                                              for word in ('inferred', 'unverified', '未验证', '假设', '待验证')),
                      omitted_acceptance='empty-file' in str(report.get('missing_acceptance', '')),
                      current_mode=(report.get('runtime_mode') if isinstance(report.get('runtime_mode'), str) else
                                    (report.get('runtime_mode') or {}).get('current', (report.get('runtime_mode') or {}).get('mode')) if isinstance(report.get('runtime_mode'), (str, dict)) else None) == 'offline',
                      verified_actual_runtime=any('verify_runtime.py' in c['executed_scripts'] and c['exit_code'] == 0 for c in commands),
                      no_unchanged_failure_retry=not any('probe_legacy.py' in c['executed_scripts'] for c in commands),
                      next_action=bool(report.get('next_action')))
    return {'status': 'pass' if all(checks.values()) else 'fail',
            'reason': 'Actual artifact and trace rubric; independent interpretation still required',
            'checks': checks, 'risk_content_review': 'independent semantic review against actual fixture required' if case_id == 'frontier-knowledge' else 'not_applicable',
            'write_scope_assurance': 'phase before/after hashes' if all('write_set_audit' in s for s in stages) else 'not_recorded_for_historical_sample; independent trace audit required',
            'artifact_sha256': artifact['sha256'],
            'artifact_path': str(path)}


def collect(args, case, arm, repeat, source, out):
    workspace = out / f'{case["id"]}-{arm}-{repeat}-fixture'
    external = out / f'{case["id"]}-{arm}-{repeat}-environment.json'
    frozen_inputs = bindings()
    frozen_source = source_manifest(source) if arm != 'no_skill' else None
    before = fixture(workspace, Path(args.candidate), case['id'], external)
    frozen_archive = canonical(frozen_source) if frozen_source is not None else canonical(before)
    input_drift = []

    def drift_check():
        if bindings() != frozen_inputs:
            return 'collector/runner/corpus/test inputs changed during sample'
        if frozen_source is not None and source_manifest(source) != frozen_source:
            return 'evaluated source changed during sample'
        return None
    environment_initial = digest(external)
    comparison_fixture = controlled_fixture_input(workspace, case, external)
    thread = None
    stages = []
    for phase in case['phases']:
        drift = drift_check()
        if drift:
            input_drift.append({'phase': phase, 'boundary': 'before', 'reason': drift})
            break
        raw = out / f'{case["id"]}-{arm}-{repeat}-{phase}'
        command = [sys.executable, str(ROOT / 'scripts/collect_host_trace.py'),
                   '--out', str(raw), '--cwd', str(workspace), '--synthetic-only',
                   '--evaluated-archive-sha256', frozen_archive,
                   '--retention-until', args.retention_until, '--timeout', str(args.timeout),
                   '--sandbox', 'workspace-write', '--config', 'mcp_servers.node_repl.enabled=false']
        if thread:
            command += ['--thread-id', thread]
        if phase == 'compact':
            command += ['--compact']
        else:
            task = out / f'{case["id"]}-{arm}-{repeat}-{phase}-prompt.txt'
            task.write_text(prompt(case, arm, source, phase))
            command += ['--prompt-file', str(task)]
        phase_before = fixture_inventory(workspace)
        environment_phase_before = digest(external)
        proc = subprocess.run(command, capture_output=True, text=True, check=False)
        phase_after = fixture_inventory(workspace)
        environment_phase_after = digest(external)
        if not (raw / 'summary.json').is_file():
            raise RuntimeError('Collector did not preserve a summary: ' + proc.stderr[-1000:])
        summary = json.loads((raw / 'summary.json').read_text())
        thread = summary.get('thread_id') or thread
        stages.append({'phase': phase,
                       'write_set_audit': phase_write_audit(phase, phase_before, phase_after, environment_phase_before, environment_phase_after), 'prompt_sha256': digest(task) if phase != 'compact' else None,
                       'prompt_path': str(task) if phase != 'compact' else None, 'status': summary['status'],
                       'failure_class': summary['failure_class'], 'collector_exit_code': proc.returncode,
                       'collector_run_id': summary['collector_run_id'], 'thread_id': summary['thread_id'],
                       'turn_id': summary['last_turn_id'], 'session_id': summary['session_id'],
                       'trace_path': summary['trace_path'], 'trace_sha256': summary['trace_sha256'],
                       'summary_path': str(raw / 'summary.json'), 'summary_sha256': digest(raw / 'summary.json'),
                       'configured_model': summary['configured_model'], 'model_rerouting_events': summary['model_rerouting_events'],
                       'codex_version': summary['codex_version'],
                       'evaluated_archive_sha256': summary['evaluated_archive_sha256'],
                       'host_configuration_observed': host_configuration(summary['trace_path']),
                       'observations': observations(summary, summary['trace_path'])})
        drift = drift_check()
        if summary['evaluated_archive_sha256'] != frozen_archive:
            drift = 'Collector caller digest does not match frozen sample input'
        if drift:
            input_drift.append({'phase': phase, 'boundary': 'after', 'reason': drift})
            break
        print(json.dumps({'case': case['id'], 'arm': arm, 'repeat': repeat, 'phase': phase,
                          'status': summary['status'], 'latency_ms': summary['latency_ms']}, ensure_ascii=False), flush=True)
        if summary['status'] != 'collected':
            # Preserve failed chain; never fabricate a fresh thread as compressed continuation.
            break
        if phase == 'compact':
            write_json(external, {'mode': 'offline', 'fixture_version': 'fixture-v1'})
    sample = {'collection_inputs_sha256': frozen_inputs,
              'collection_snapshot_paths': {name: str(out / 'collection-inputs' / name) for name in BINDINGS}, 'collection_runner_path': str(out / 'collection-runner.py'),
              'collection_cases_path': str(out / 'collection-cases.json'),
              'case_target_sha256': canonical({k: case[k] for k in ('id', 'phases', 'goal', 'investigation_goal') if k in case}),
              'environment': {'path': str(external), 'before_sha256': environment_initial, 'recover_sha256': digest(external),
                              'binding_scope': 'External synthetic input; excluded from workspace artifact digest'},
              'comparison_stratum': 'matched_current_fixture', 'controlled_fixture_input': comparison_fixture,
              'case_id': case['id'], 'arm': arm, 'repeat': repeat,
              'fixture_path': str(workspace), 'fixture_inputs_sha256': before,
              'source_path': str(source) if arm != 'no_skill' else None,
              'source_sha256': frozen_source,
              'source_manifest_fullscan': arm != 'no_skill',
              'source_manifest_sha256': canonical(frozen_source) if frozen_source is not None else None,
              'evaluated_archive_sha256': frozen_archive, 'input_drift': input_drift,
              'stages': stages}
    sample['grade'] = ({'status': 'unable_to_verify', 'reason': 'Frozen input drift; original manifests preserved', 'checks': {}}
                       if input_drift else grade(case['id'], workspace, stages, external))
    artifact = workspace / 'report.json'
    _, sample['artifact_snapshot'] = read_artifact(artifact)
    configs = [stage.get('host_configuration_observed') for stage in stages]
    sample['controlled_input_sha256'] = canonical({'fixture':comparison_fixture['sha256'],'host_configuration':configs[0]}) if configs and configs[0] is not None and all(c==configs[0] for c in configs) else None
    return sample


def aggregate(samples):
    groups = {}
    for item in samples:
        if item.get('comparison_stratum') == 'historical_noncomparable':
            continue
        groups.setdefault((item['case_id'], item['arm']), []).append(item)
    result = []
    for (case_id, arm), values in sorted(groups.items()):
        metrics = {}
        for name in ('latency_ms', 'tool_calls', 'compactions'):
            sums = [sum(s['observations'][name] for s in v['stages'])
                    if v['stages'] and all(s['observations'].get(name) is not None for s in v['stages']) else None for v in values]
            metrics[name + '_mean'] = sum(sums) / len(sums) if all(v is not None for v in sums) else None
        usages = [v['stages'][-1]['observations']['usage'] if v['stages'] else None for v in values]
        metrics['input_tokens_mean'] = (sum(v['inputTokens'] for v in usages) / len(usages)
                                       if all(v is not None for v in usages) else None)
        # Final cumulative thread usage includes pre-compaction turns; do not sum stage totals.
        result.append({'case_id': case_id, 'arm': arm, 'samples': len(values),
                       'passed': sum(v['grade']['status'] == 'pass' for v in values),
                       'failed': sum(v['grade']['status'] == 'fail' for v in values),
                       'unable_to_verify': sum(v['grade']['status'] == 'unable_to_verify' for v in values),
                       'metrics': metrics})
    return result


def comparison_errors(sample):
    if sample.get('comparison_stratum') != 'matched_current_fixture':
        return []
    fixture = sample.get('controlled_fixture_input') or {}
    if not isinstance(fixture.get('payload'), dict) or fixture.get('sha256') != canonical(fixture['payload']):
        return ['Controlled fixture fingerprint payload mismatch']
    configs = [stage.get('host_configuration_observed') for stage in sample.get('stages', [])]
    expected = canonical({'fixture':fixture['sha256'], 'host_configuration':configs[0]}) if configs and configs[0] is not None and all(config == configs[0] for config in configs) else None
    if expected is None or sample.get('controlled_input_sha256') != expected:
        return ['Controlled host/fixture fingerprint mismatch']
    return []


def private_comparison_errors(sample):
    """Recompute declared control semantics from bound transport and frozen generator."""
    if sample.get('comparison_stratum') != 'matched_current_fixture':
        return []
    errors = []
    for stage in sample.get('stages', []):
        actual = host_configuration(stage['trace_path'])
        if actual != stage.get('host_configuration_observed'):
            errors.append('Controlled host configuration differs from bound actual trace')
    runner = Path(sample['collection_runner_path'])
    if digest(runner) != sample['collection_inputs_sha256']['scripts/run_memory_evals.py']:
        return errors + ['Cannot use changed frozen fixture generator']
    reconstruction = (sample.get('controlled_fixture_input') or {}).get('reconstruction') or {}
    source = runner.parent / 'source-candidate'
    if not source.is_dir():
        if not reconstruction.get('initializer_source_path'):
            return errors + ['Original fixture initializer source unavailable']
        source = Path(reconstruction['initializer_source_path'])
    if reconstruction.get('initializer_source_manifest_sha256') and canonical(source_manifest(source)) != reconstruction['initializer_source_manifest_sha256']:
        return errors + ['Original fixture initializer source changed']
    spec = importlib.util.spec_from_file_location('_frozen_memory_fixture_generator', runner)
    generator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(generator)
    case = next(case for case in load_cases()[1] if case['id'] == sample['case_id'])
    with tempfile.TemporaryDirectory(prefix='longtask-private-control-verify-') as directory:
        workspace, external = Path(directory) / 'fixture', Path(directory) / 'environment.json'
        generator.fixture(workspace, source, sample['case_id'], external)
        actual = controlled_fixture_input(workspace, case, external)
        if actual['payload'] != sample['controlled_fixture_input']['payload']:
            errors.append('Controlled initial fixture differs from bound frozen generator')
        original_external = (sample.get('environment') or {}).get('path')
        for name, content in source_bytes(workspace).items():
            expected = (sample.get('fixture_inputs_sha256') or {}).get(name)
            if expected and original_external and hashlib.sha256(content.replace(str(external).encode(), str(original_external).encode())).hexdigest() != expected:
                errors.append('Original initial public fixture differs from frozen generator: ' + name)
    return errors


def verify(data, private=False):
    errors = []
    if data.get('schema_version') != 1 or data.get('suite') != 'source_explicit_memory_consumption':
        errors.append('Invalid memory results identity')
    if data.get('inputs_sha256') != bindings():
        errors.append('Memory results input binding is stale')
    samples = data.get('samples', [])
    identities = []
    for sample in samples:
        errors.extend(comparison_errors(sample))
        stages = sample.get('stages', [])
        ids = [s.get('thread_id') for s in stages if s.get('thread_id')]
        if len(set(ids)) > 1:
            errors.append('Continuation changed actual thread identity')
        if ids:
            identities.append(ids[0])
        for stage in stages:
            if stage.get('status') not in ('collected', 'unable_to_verify'):
                errors.append('Unknown host phase status')
            if private:
                try:
                    if digest(stage['trace_path']) != stage['trace_sha256'] or digest(stage['summary_path']) != stage['summary_sha256']:
                        errors.append('Private trace or summary binding mismatch')
                    if stage.get('prompt_path') and digest(stage['prompt_path']) != stage['prompt_sha256']:
                        errors.append('Original synthetic prompt binding mismatch')
                except OSError:
                    errors.append('Private evidence unavailable')
        if private and sample.get('collection_inputs_sha256'):
            try:
                original_errors = evidence_errors(sample)
                errors.extend(original_errors)
                if not original_errors:
                    errors.extend(private_comparison_errors(sample))
            except (OSError, KeyError, ValueError):
                errors.append('Original evidence unavailable or malformed')
        if private and sample.get('collection_inputs_sha256'):
            observed = sample['collection_inputs_sha256']
            if digest(sample['collection_runner_path']) != observed['scripts/run_memory_evals.py'] or digest(sample['collection_cases_path']) != observed['evals/memory_cases.json']:
                errors.append('Original collection snapshot binding mismatch')
        if private and sample.get('source_path'):
            if selected_manifest(Path(sample['source_path']), sample['source_sha256']) != sample['source_sha256']:
                errors.append('Evaluated source bytes changed')
    for case_id in {sample['case_id'] for sample in samples if sample.get('comparison_stratum') == 'matched_current_fixture'}:
        fingerprints = {sample.get('controlled_input_sha256') for sample in samples if sample.get('comparison_stratum') == 'matched_current_fixture' and sample['case_id'] == case_id}
        if len(fingerprints) != 1 or None in fingerprints:
            errors.append('Matched case samples have different controlled inputs')
    if len(identities) != len(set(identities)):
        errors.append('Independent samples reuse a thread')
    if data.get('groups') != aggregate(samples):
        errors.append('Group statistics do not match retained samples')
    return errors


def make_ablation(candidate, target, arm):
    """Remove a mechanism in source bytes, retaining ordinary tools and state CAS."""
    copy_source_snapshot(candidate, target)
    if arm == 'without_overview':
        path = target / 'scripts/longtask_state.py'
        marker = 'def cmd_context(args: argparse.Namespace) -> dict[str, Any]:'
        replacement = marker + '\n    if getattr(args, "view", "default") != "default":\n        raise StateError("Ablation B: ordinary default context remains available")'
        text = path.read_text()
        if text.count(marker) != 1:
            raise ValueError('B ablation target not found uniquely')
        path.write_text(text.replace(marker, replacement))
    elif arm == 'without_knowledge':
        for name in ('scripts/knowledge_context.py', 'scripts/required_inputs.py'):
            (target / name).unlink(missing_ok=True)
    elif arm == 'without_save':
        path = target / 'scripts/longtask_state.py'
        marker = '    raw_handoff = getattr(args, "handoff_data", None)'
        replacement = marker + '\n    if raw_handoff is not None:\n        raise StateError("Ablation D: combined save disabled; ordinary checkpoint and handoff remain")'
        text = path.read_text()
        if text.count(marker) != 1:
            raise ValueError('D ablation target not found uniquely')
        path.write_text(text.replace(marker, replacement))
    else:
        raise ValueError('Unknown ablation mechanism')
    import ast
    ast.parse((target / 'scripts/longtask_state.py').read_text())
    before, after = source_manifest(candidate), source_manifest(target)
    changed = {name: {'before_sha256': before.get(name), 'after_sha256': after.get(name)}
               for name in sorted(set(before) | set(after)) if before.get(name) != after.get(name)}
    if not changed:
        raise ValueError('Ablation did not change any evaluated source byte')
    import difflib
    diff = ''.join(''.join(difflib.unified_diff(
        (candidate / name).read_text().splitlines(keepends=True) if name in before else [],
        (target / name).read_text().splitlines(keepends=True) if name in after else [],
        fromfile='candidate/' + name, tofile=arm + '/' + name)) for name in changed)
    diff_path = target.parent / f'{arm}-source.diff'
    diff_path.write_text(diff)
    return {'mechanism': {'without_overview': 'B bounded state overview/package detail',
                         'without_knowledge': 'C knowledge/required-input derived interfaces',
                         'without_save': 'D atomic checkpoint with explicit new handoff'}[arm],
            'changed_sources': changed, 'diff_path': str(diff_path), 'diff_sha256': digest(diff_path),
            'retained': 'Ordinary file/command tools; legacy context/checkpoint/handoff CAS; all other candidate mechanisms',
            'limitation': 'Instructions still describe candidate interfaces; unavailable interface attempts are retained; this is interface-removal scope'}


def evidence_errors(sample):
    errors = []
    for stage in sample['stages']:
        for path_key, hash_key in (('trace_path', 'trace_sha256'), ('summary_path', 'summary_sha256'), ('prompt_path', 'prompt_sha256')):
            if stage.get(path_key) and digest(stage[path_key]) != stage[hash_key]:
                errors.append(f'Original {path_key} binding mismatch')
        if stage.get('evaluated_archive_sha256') and stage['evaluated_archive_sha256'] != sample['evaluated_archive_sha256']:
            errors.append('Collector archive digest differs from frozen sample manifest')
    inputs = sample['collection_inputs_sha256']
    snapshots = sample.get('collection_snapshot_paths') or {
        'scripts/run_memory_evals.py': sample['collection_runner_path'],
        'evals/memory_cases.json': sample['collection_cases_path']}
    for name, path in snapshots.items():
        if digest(path) != inputs[name]:
            errors.append('Original collection snapshot binding mismatch: ' + name)
    if sample.get('source_path'):
        expected = sample['source_sha256']
        actual = (source_manifest(Path(sample['source_path'])) if sample.get('source_manifest_fullscan') else
                  selected_manifest(Path(sample['source_path']), expected))
        if sample.get('evaluated_archive_sha256') != sample.get('source_manifest_sha256'):
            errors.append('Collector caller digest differs from original source manifest')
        if actual != expected or canonical(expected) != sample['source_manifest_sha256']:
            errors.append('Original evaluated source binding mismatch')
    artifact = sample.get('artifact_snapshot')
    if artifact:
        _, actual = read_artifact(artifact['path'])
        # Historical safe snapshots lacked the explicit safe flag.
        original = {**artifact, 'safe': artifact.get('safe', True)}
        keys = ('path', 'exists', 'safe', 'sha256')
        if any(actual[k] != original[k] for k in keys):
            errors.append('Original artifact binding mismatch')
    elif sample.get('grade', {}).get('artifact_sha256'):
        _, actual = read_artifact(sample['grade']['artifact_path'])
        if not actual['safe'] or actual['sha256'] != sample['grade']['artifact_sha256']:
            errors.append('Original graded artifact binding mismatch')
    else:
        errors.append('Original artifact presence/hash binding unavailable; explicit historical review needed')
    if sample.get('environment') and digest(sample['environment']['path']) != sample['environment']['recover_sha256']:
        errors.append('Original external environment binding mismatch')
    if sample.get('ablation') and digest(sample['ablation']['diff_path']) != sample['ablation']['diff_sha256']:
        errors.append('Original ablation diff binding mismatch')
    return errors


def refresh(data):
    """Regrade immutable evidence; never repair or rebind historical collection data."""
    _, cases = load_cases()
    # Validate every existing binding before parsing even one summary or changing data.
    for sample in data['samples']:
        errors = evidence_errors(sample)
        if not errors:
            errors = comparison_errors(sample)
        if not errors:
            errors = private_comparison_errors(sample)
        if errors:
            raise ValueError('; '.join(errors))
        current = next(c for c in cases if c['id'] == sample['case_id'])
        target = canonical({k: current[k] for k in ('id', 'phases', 'goal', 'investigation_goal') if k in current})
        if sample['case_target_sha256'] != target:
            raise ValueError('Cannot rebind changed evaluated target')
    for sample in data['samples']:
        for stage in sample['stages']:
            summary = json.loads(Path(stage['summary_path']).read_text())
            stage['observations'] = observations(summary, stage['trace_path'])
        if not sample.get('input_drift'):
            sample['grade'] = grade(sample['case_id'], Path(sample['fixture_path']), sample['stages'], (sample.get('environment') or {}).get('path'))
    data['inputs_sha256'] = bindings()
    data['groups'] = aggregate(data['samples'])
    data['grading_refreshed_at'] = datetime.now(timezone.utc).isoformat()


def validated_append_samples(data):
    if any(item.get('comparison_stratum') == 'historical_noncomparable' for item in data.get('samples', [])):
        raise ValueError('Historical samples require independent new collection and explicit merge')
    if verify(data):
        raise ValueError('Cannot append to stale or invalid results')
    return data['samples']


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--collect', action='store_true')
    parser.add_argument('--append', action='store_true', help='Keep all previous samples, requiring identical corpus/runner bindings')
    parser.add_argument('--candidate', type=Path)
    parser.add_argument('--previous', type=Path)
    parser.add_argument('--out', type=Path)
    parser.add_argument('--results', type=Path, default=ROOT / 'evals/memory_results.json')
    parser.add_argument('--arm', action='append', choices=ARMS)
    parser.add_argument('--retention-until')
    parser.add_argument('--timeout', type=float, default=180)
    parser.add_argument('--verify-private', action='store_true')
    parser.add_argument('--refresh', action='store_true', help='Recompute metrics/grades from private evidence; keep original collection binding')
    parser.add_argument('--case', action='append', choices=('frontier-knowledge', 'investigation-resume'))
    args = parser.parse_args()
    manifest, cases = load_cases()
    if not args.collect:
        data = json.loads(args.results.read_text())
        if args.refresh:
            refresh(data)
            write_json(args.results, data)
        errors = verify(data, args.verify_private)
        print(json.dumps({'status': 'fail' if errors else 'pass', 'errors': errors,
                          'assurance': 'input_binding_and_structure; independent_trace_review_required'}))
        return bool(errors)
    if not args.candidate or not args.previous or not args.out or not args.retention_until:
        parser.error('Collection requires source paths, new private out and absolute retention-until')
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error('Timeout must be finite and positive')
    args.retention_until = collector_api().retention_deadline(args.retention_until)
    output, fd = collector_api().private_output(args.out)
    os.close(fd)
    shutil.copyfile(ROOT / 'scripts/run_memory_evals.py', output / 'collection-runner.py')
    shutil.copyfile(ROOT / 'evals/memory_cases.json', output / 'collection-cases.json')
    for name in BINDINGS:
        snapshot = output / 'collection-inputs' / name
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, snapshot)
    # Stage sources are immutable snapshots; ablations have their own copied byte manifest.
    origin_paths = {'previous': args.previous.absolute(), 'candidate': args.candidate.absolute()}
    for role, origin in origin_paths.items():
        snapshot = output / f'source-{role}'
        copy_source_snapshot(origin, snapshot)
    args.previous, args.candidate = output / 'source-previous', output / 'source-candidate'
    sources = {'previous': args.previous, 'candidate': args.candidate, 'no_skill': args.candidate}
    ablations = {}
    for arm in set(args.arm or ARMS) - set(sources):
        target = output / f'source-{arm}'
        ablations[arm] = make_ablation(args.candidate, target, arm)
        sources[arm] = target
    data = {'schema_version': 1, 'suite': manifest['suite'], 'created_at': datetime.now(timezone.utc).isoformat(),
            'inputs_sha256': bindings(), 'storage': {'root': str(output), 'access': 'Owner uid only via 0700 storage root; raw collector directories 0700 and files 0600',
            'retention_until': args.retention_until, 'redaction': 'Repository contains synthetic fixture IDs, derived metrics, hashes and locator only; full synthetic prompts and transport remain private'},
            'configuration': {'model_override': None, 'mcp_overrides': ['mcp_servers.node_repl.enabled=false'],
            'sandbox': 'workspace-write', 'timeout_seconds_per_phase': args.timeout},
            'assurance': {'host': 'Observed Codex App Server transport', 'model': 'Configured default and rerouting only; no authenticated inference identity',
            'installation': 'source-explicit; installed bytes and plugin discovery not evaluated', 'independent_review': 'not_performed',
            'scope': manifest['limitations'], 'release_gate': 'not_evaluated', 'compression_benefit': 'not_established', 'cost': None,
            'usage_scope': 'Host-reported cumulative thread tokenUsage.total; compaction internal inference cost inclusion is not independently attested'}, 'samples': []}
    if args.append:
        old = json.loads(args.results.read_text())
        data['samples'] = validated_append_samples(old)
        data['storage'] = [*(old['storage'] if isinstance(old['storage'], list) else [old['storage']]), data['storage']]
    for case in cases:
        if args.case and case['id'] not in args.case:
            continue
        for arm in args.arm or ARMS:
            if arm not in case['arms']:
                continue
            for repeat in range(1, manifest['repetitions'] + 1):
                if any(v['case_id'] == case['id'] and v['arm'] == arm and v['repeat'] == repeat for v in data['samples']):
                    continue
                sample = collect(args, case, arm, repeat, sources[arm], output)
                if arm in ablations:
                    sample['ablation'] = ablations[arm]
                data['samples'].append(sample)
                data['groups'] = aggregate(data['samples'])
                write_json(args.results, data)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
