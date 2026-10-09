#!/usr/bin/env python3
"""Observe eight core journeys plus one supplemental changed-goal boundary.

Default verifies saved bindings. Collection never installs a plugin, changes model,
fabricates approval/review, or certifies release/performance. There is no refresh or
rebinding operation: changed collection inputs require a new sample.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
BINDINGS = ('scripts/run_progress_evals.py', 'scripts/collect_host_trace.py',
            'scripts/run_memory_evals.py', 'evals/progress_cases.json',
            'tests/test_progress_evals.py')
CORE_CASE_IDS = {'new-project', 'planning-only', 'fresh-checkpoint', 'stale-handoff',
                 'review-fix-rereview', 'inserted-constraint', 'investigation-return', 'narrow-low-risk'}
SUPPLEMENTAL_CASE_IDS = {'changed-goal'}
CASE_IDS = CORE_CASE_IDS | SUPPLEMENTAL_CASE_IDS


def helper():
    name = '_longtask_progress_memory_safety'
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, ROOT / 'scripts/run_memory_evals.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        sys.modules[name] = module
    return sys.modules[name]


def canonical(value):
    return helper().canonical(value)


def digest(path):
    return helper().digest(path)


def bindings():
    return {name: digest(ROOT / name) for name in BINDINGS}


def output_parent(path):
    """Open/create output ancestors without following any substituted directory."""
    path = Path(path)
    if '..' in path.parts:
        raise ValueError('Evidence output path must not contain parent traversal')
    path = Path(os.path.abspath(path))
    parent = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in path.parent.parts[1:]:
            try:
                os.mkdir(part, mode=0o700, dir_fd=parent)
            except FileExistsError:
                pass
            next_parent = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            os.close(parent)
            parent = next_parent
        return path, parent
    except BaseException:
        os.close(parent)
        raise


def new_directory(path):
    path, parent = output_parent(path)
    try:
        os.mkdir(path.name, mode=0o700, dir_fd=parent)
    finally:
        os.close(parent)


def write(path, content):
    """Create through pinned no-follow ancestors, including public result paths."""
    path, parent = output_parent(path)
    try:
        fd = os.open(path.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=parent)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(content.encode() if isinstance(content, str) else content)
    finally:
        os.close(parent)


def write_json(path, value):
    write(path, json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def load_cases():
    manifest = json.loads((ROOT / 'evals/progress_cases.json').read_text())
    cases = manifest.get('cases', [])
    if manifest.get('schema_version') != 1 or manifest.get('suite') != 'source_explicit_progress_journeys':
        raise ValueError('Unsupported progress corpus identity')
    if {case['id'] for case in cases} != CASE_IDS or len(cases) != len(CASE_IDS):
        raise ValueError('Eight core journeys and one supplemental changed-goal boundary are required')
    if {case['id'] for case in cases if case.get('supplemental') is True} != SUPPLEMENTAL_CASE_IDS:
        raise ValueError('Supplemental boundary cases must be explicitly distinguished from core eight')
    if any(not case.get('phases') or len({p['id'] for p in case['phases']}) != len(case['phases']) for case in cases):
        raise ValueError('Every journey needs unique executable phases')
    return manifest, cases


def run_cli(source, workspace, command, *args):
    proc = subprocess.run([sys.executable, str(source / 'scripts/longtask_state.py'),
                           command, '--root', str(workspace), *args],
                          capture_output=True, text=True, check=False)
    if proc.returncode:
        raise ValueError('Fixture public CLI failed: ' + proc.stderr[-2000:])
    return json.loads(proc.stdout)


CONTRACT = ('# 本地slug合同\n\n来源: approved，当前合成用户任务合同。\n'
            '实现slug.py的slug(text: str) -> str：转小写，连续任意空白折叠为单个连字符，'
            '剔除两端空白；空或全空白字符串返回空字符串；Unicode字母、数字与下划线保持。'
            '无网络、依赖下载或外部服务。输入输出完全本地。\n'
            '验收: Hello World -> hello-world；空输入 -> 空字符串；空白折叠和两端剔除；Unicode和下划线。\n')


def fixture(workspace, source, case):
    """Only public CLI creates state. No fixture approval or passing review exists."""
    new_directory(workspace)
    write(workspace / '合同.md', CONTRACT)
    write(workspace / 'AGENTS.md', '# 合成评测边界\n只处理本地合成任务；禁止网络、认证文件、个人项目和外部副作用。'
          '技能仅使用提示显式指定的源码，不用全局安装技能。当前用户提示提供本轮授权，'
          'fixture状态不提供授权。不要改写原合同来适配实现。\n')
    if case['fixture'] == 'new':
        return {'cli_operations': [], 'state_created_by': None, 'critic': None}
    write(workspace / 'docs/ARCHITECTURE.md', '# 本地文本工具\n\n来源: approved，合成目标。\n'
          '唯一产品合同见[合同](../合同.md)。slug.py负责纯字符串转换，无外部资源。\n')
    write(workspace / 'docs/modules/文本转换.md', '# 文本转换\n\n来源: approved，合成目标。\n'
          '职责与验收见[合同](../../合同.md)。当前观察仅普通单空格输入通过，完整验收尚未通过。\n')
    implementation = "def slug(text):\n    return text.lower().replace(' ', '-')\n"
    if case['fixture'] == 'narrow':
        implementation = "import re\ndef slug(text):\n    return re.sub(r'\\s+', '-', text.lower())\n"
    write(workspace / 'slug.py', implementation)
    write(workspace / 'probe.py', "print({'input': '  A\\t B  ', 'split': '  A\\t B  '.split(), 'condition': 'local-python-str-v1'})\n")
    critic = None
    if case['fixture'] == 'review':
        # This is fixture input, never submitted as authenticated review evidence.
        write(workspace / 'probe_whitespace.py',
              "import json,sys\nfrom slug import slug\nactual=slug('  Hello   World  ')\n"
              "print(json.dumps({'observed':actual,'expected':'hello-world'}))\n"
              "sys.exit(0 if actual=='hello-world' else 2)\n")
        check_command = [sys.executable, '-B', str(workspace / 'probe_whitespace.py')]
        checked = subprocess.run(check_command, cwd=workspace, capture_output=True,
                                 text=True, check=False, timeout=10)
        observed = json.loads(checked.stdout)
        if checked.returncode != 2 or observed['observed'] == observed['expected']:
            raise ValueError('Review fixture must reproduce an actual known failed whitespace check')
        critic = {'provenance': 'synthetic fixture critic; not actual independent review',
                  'requirement': 'whitespace', 'input': '  Hello   World  ',
                  'observed': observed['observed'], 'expected': observed['expected'],
                  'status': 'changes_required', 'actual_probe_exit_code': checked.returncode,
                  'actual_probe_command': check_command}
        write(workspace / 'docs/审查输入.md', '# 合成critic输入\n\n' + json.dumps(critic, ensure_ascii=False, indent=2) + '\n')
    if case['fixture'] == 'narrow':
        return {'cli_operations': [], 'state_created_by': None, 'critic': critic}
    write(workspace / 'phase_probe.py',
          "from pathlib import Path\nimport sys\n"
          "required={'discovery':['合同.md'],'architecture':['docs/ARCHITECTURE.md'],"
          "'documentation':['docs/modules/文本转换.md']}\n"
          "for name in required[sys.argv[1]]:\n"
          " assert Path(name).is_file() and Path(name).read_text().strip(),name\n"
          "print('PASS local fixture document presence: '+sys.argv[1])\n")
    operations = ['init']
    state = run_cli(source, workspace, 'init', '--mode', 'continue', '--task-id', 'synthetic-progress',
                    '--goal', '完成合同.md的本地slug实现及完整验收', '--actor', 'fixture-generator')
    for phase, next_phase in (('discovery', 'architecture'), ('architecture', 'documentation'),
                              ('documentation', 'execution')):
        check_command = [sys.executable, '-B', str(workspace / 'phase_probe.py'), phase]
        checked = subprocess.run(check_command, cwd=workspace, capture_output=True, text=True, check=False)
        if checked.returncode:
            raise ValueError('Actual fixture phase document check failed: ' + checked.stderr)
        state = run_cli(source, workspace, 'evidence', '--expected-task-id', state['task_id'],
                        '--expected-revision', str(state['revision']), '--kind', 'phase:' + phase,
                        '--summary', checked.stdout.strip(), '--result', 'pass',
                        '--command', json.dumps(check_command), '--check-id', 'fixture-doc:' + phase,
                        '--actor', 'fixture-generator')
        state = run_cli(source, workspace, 'transition', '--expected-task-id', state['task_id'],
                        '--expected-revision', str(state['revision']), '--phase', next_phase,
                        '--status', 'active', '--actor', 'fixture-generator')
        operations += ['evidence:phase:' + phase, 'transition:' + next_phase]
    package = {'id': 'slug', 'objective': '实现本地slug完整合同', 'status': 'planned',
               'dependencies': [], 'affected_modules': ['文本转换'],
               'write_set': ['slug.py', '合同.md', 'docs', 'plan.md', 'investigation.md', 'tests'],
               'acceptance_checks': ['normal', 'empty', 'whitespace', 'unicode-underscore'],
               'base_revision': state['artifact_digest'], 'risk': '本地纯函数低风险', 'risk_level': 'low',
               'required_review_roles': ['independent-quality'], 'rollback': '恢复初始slug.py',
               'stopping_condition': '实际验收失败或所需独立复审不可用',
               'owner': None, 'lease_expires': None, 'contributors': ['fixture-generator']}
    state = run_cli(source, workspace, 'package', '--expected-task-id', state['task_id'],
                    '--expected-revision', str(state['revision']), '--data', json.dumps(package),
                    '--actor', 'fixture-generator')
    package.update(status='active', owner='journey-worker',
                   lease_expires=(datetime.now(timezone.utc) + timedelta(hours=6)).isoformat())
    state = run_cli(source, workspace, 'package', '--expected-task-id', state['task_id'],
                    '--expected-revision', str(state['revision']), '--data', json.dumps(package),
                    '--actor', 'fixture-generator')
    operations += ['package:planned', 'package:active']
    if critic:
        state = run_cli(source, workspace, 'evidence', '--expected-task-id', state['task_id'],
                        '--expected-revision', str(state['revision']), '--kind', 'verification',
                        '--summary', 'Actual local whitespace check failed; synthetic critic is not independent review',
                        '--result', 'fail', '--command', json.dumps(critic['actual_probe_command']),
                        '--check-id', 'whitespace', '--package-id', 'slug', '--actor', 'fixture-generator',
                        '--details', json.dumps({'actual_probe_exit_code': critic['actual_probe_exit_code'],
                                                 'observed': critic['observed'], 'expected': critic['expected']}))
        operations += ['evidence:verification:fail']
    handoff = {'intent': 'remediate' if critic else 'execute', 'objective': '完成slug完整合同与验收',
               'reason': '当前普通输入仅局部通过；其余验收未完成',
               'target': {'kind': 'work_package', 'ref': 'slug'},
               'required_inputs': ['合同.md', 'docs/modules/文本转换.md'],
               'acceptance_checks': package['acceptance_checks'],
               'next_if_pass': {'intent': 'review', 'objective': '请真实独立审查者核验当前版本'},
               'next_if_fail': {'intent': 'remediate', 'objective': '修复当前失败验收'}}
    state = run_cli(source, workspace, 'handoff', '--expected-task-id', state['task_id'],
                    '--expected-revision', str(state['revision']), '--data', json.dumps(handoff),
                    '--actor', 'fixture-generator')
    operations += ['handoff']
    if case['fixture'] == 'stale':
        # Real product edit after version-bound public handoff, never hand-edit state.
        path = workspace / 'slug.py'
        path.write_text(path.read_text() + '\n# 合成交接后的外部变更\n')
    return {'cli_operations': operations, 'state_created_by': 'public CLI',
            'task_id': state['task_id'], 'revision': state['revision'], 'critic': critic,
            'approval_created': False, 'independent_review_created': False}


def inventory(workspace):
    return helper().fixture_inventory(workspace)


def freeze_fixture(workspace, target):
    new_directory(target)
    original = inventory(workspace)
    for name, value in original.items():
        if value == 'directory':
            directory, descriptor = output_parent(target / name / '.directory-placeholder')
            os.close(descriptor)
        elif re.fullmatch(r'[0-9a-f]{64}', value):
            content, safe = helper().read_artifact(workspace / name)
            if not safe['safe'] or safe['sha256'] != value:
                raise ValueError('Initial fixture changed during freeze')
            write(target / name, content)
        else:
            raise ValueError('Unsafe initial fixture entry: ' + name)
    if inventory(target) != original or inventory(workspace) != original:
        raise ValueError('Initial fixture changed during freeze')
    return original


def prompt(source, case, phase):
    return (f'本次显式使用源码技能 {source}/SKILL.md；引用相对该源码根解析。'
            '这不是安装后发现测试。忽略全局安装技能。\n'
            '所有资料与授权限于当前合成工作区。不要访问网络、secret或个人项目。'
            f'本次可用Python解释器为 {sys.executable}；运行技能CLI和本地探针时使用该解释器。'
            '合成续接工作包owner为journey-worker，可沿用该运行时协作actor；这不代表用户授权或独立身份认证。'
            '不替真实用户批准，不伪造独立审查。应用产品写集slug.py、必要项目文档、'
            'plan.md/investigation.md和相关tests；CLI写集中的目录前缀写作docs、tests，不用glob或末尾斜杠。'
            '检查点只通过公开运行时CLI修改。\n' + phase['instruction'])


def trace_observations(summary, trace, workspace=None):
    """Direct events plus clearly labelled candidates; no semantic keyword score."""
    rows = helper().events(trace)
    seen = set()
    commands, tools, first_file_change = [], [], None
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get('message'), dict):
            raise ValueError('Collector trace rows must contain message objects')
        msg = row.get('message', {})
        if row.get('direction') != 'server_to_client' or msg.get('method') != 'item/completed':
            continue
        params = msg.get('params') or {}
        if not isinstance(params, dict) or not isinstance(params.get('item'), dict):
            raise ValueError('Completed trace event must contain an item object')
        item = params['item']
        identity = item.get('id')
        if not identity or identity in seen:
            continue
        seen.add(identity)
        kind = item.get('type')
        if kind in ('commandExecution', 'mcpToolCall', 'dynamicToolCall', 'fileChange',
                    'webSearch', 'collabAgentToolCall'):
            tools.append(identity)
        if kind == 'commandExecution':
            command = item.get('command') or ''
            commands.append({'item_id': identity, 'elapsed_ms': row.get('elapsed_ms'),
                             'command_sha256': canonical(command), 'exit_code': item.get('exitCode'),
                             'output_chars': len(item.get('aggregatedOutput') or ''),
                             'material_read_candidate': bool(re.search(r'\b(cat|sed|head|tail|rg)\b', command)),
                             'state_cli_candidate': bool(re.search(r'longtask_state\.py(?:\s|$)', command)),
                             'verification_candidate': bool(re.search(r'\b(unittest|pytest|verify|test)\b', command))})
        if kind == 'fileChange' and item.get('status') == 'completed' and first_file_change is None:
            changes = item.get('changes') or []
            allowed_paths = {'slug.py'} | ({str(Path(workspace) / 'slug.py')} if workspace else set())
            if any(change.get('path') in allowed_paths for change in changes if isinstance(change, dict)):
                first_file_change = {'item_id': identity, 'elapsed_ms': row.get('elapsed_ms'),
                                     'assurance': 'observed completed fileChange to application; semantic effectiveness unknown'}
    return {'latency_ms': summary.get('latency_ms'), 'unique_observed_tool_ids': tools,
            'unique_observed_commands': commands, 'first_application_change_event': first_file_change,
            'nonzero_exit_candidate_ids': [c['item_id'] for c in commands if isinstance(c['exit_code'], int) and c['exit_code'] != 0],
            'material_read_candidate_ids': [c['item_id'] for c in commands if c['material_read_candidate']],
            'state_cli_candidate_ids': [c['item_id'] for c in commands if c['state_cli_candidate']],
            'semantic_material_bytes': None, 'unjustified_rechecks': None,
            'wrong_permission_questions': None, 'corrective_calls': None,
            'first_effective_advancement': None, 'usage_updates': summary.get('usage_updates'),
            'limitation': 'Candidates require trace-level semantic review; shell/file writes may have no fileChange event'}


def acceptance(workspace, case, stages):
    """Execute a fixed external local probe; never run agent-supplied tests as oracle."""
    if len(stages) != len(case['phases']) or any(stage.get('status') != 'collected' or stage.get('collector_exit_code', 0) != 0 for stage in stages):
        return {'status': 'unable_to_verify', 'checks': {}, 'reason': 'Required host phases incomplete, failed or timed out'}
    if case['acceptance'] == 'changed-goal-boundary':
        content, document = helper().read_artifact(workspace / 'docs/变更说明.md')
        _, application = helper().read_artifact(workspace / 'slug.py')
        original_hash = stages[0].get('before', {}).get('slug.py')
        checks = {'new_document_exists': bool(content) and document['safe'],
                  'original_application_bytes_preserved': bool(original_hash) and application['safe']
                  and application['sha256'] == original_hash and all(
                      stage.get(boundary, {}).get('slug.py') == original_hash
                      for stage in stages for boundary in ('before', 'after'))}
        return {'status': 'pass' if all(checks.values()) else 'fail', 'checks': checks,
                'document': document, 'artifact': application,
                'reason': 'Fixed document/product byte boundary only; goal/state adjustment semantics require independent review'}
    if case['acceptance'] == 'planning':
        content, snap = helper().read_artifact(workspace / 'plan.md')
        checks = {'plan_exists': bool(content) and snap['safe'],
                  'application_not_implemented': not (workspace / 'slug.py').exists()}
        return {'status': 'pass' if all(checks.values()) else 'fail', 'checks': checks,
                'reason': 'Planning artifact presence and product boundary only; plan semantics unknown'}
    content, snapshot = helper().read_artifact(workspace / 'slug.py')
    if not snapshot['safe'] or content is None:
        return {'status': 'fail', 'checks': {'safe_application_exists': False}, 'artifact': snapshot}
    tests = [('normal', 'Hello World', 'hello-world'), ('empty', '', ''),
             ('whitespace', '  A\t  B\n ', 'a-b'), ('all-whitespace', ' \t\n', ''),
             ('unicode-underscore', '  É_a  42 ', 'é_a-42')]
    probe = ("import importlib.util,json,sys\nspec=importlib.util.spec_from_file_location('fixture_slug',sys.argv[1])\n"
             "m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)\nchecks={}\n"
             f"for name,value,expected in {tests!r}:\n"
             " try: checks[name]=m.slug(value)==expected\n except Exception: checks[name]=False\n")
    if case['acceptance'] == 'application-with-constraint':
        probe += "try:\n m.slug(None);checks['None-TypeError']=False\nexcept TypeError: checks['None-TypeError']=True\nexcept Exception: checks['None-TypeError']=False\n"
    probe += "print(json.dumps(checks,sort_keys=True))\nsys.exit(0 if all(checks.values()) else 2)\n"
    # -I prevents fixture/site import tricks. The imported product still runs only
    # inside the explicitly authorized disposable workspace and bounded process.
    try:
        proc = subprocess.run([sys.executable, '-I', '-B', '-c', probe, str(workspace / 'slug.py')],
                              cwd=workspace, capture_output=True, text=True, check=False, timeout=10)
        checks = json.loads(proc.stdout) if proc.returncode in (0, 2) else {}
        if not isinstance(checks, dict) or set(checks) != {t[0] for t in tests} | ({'None-TypeError'} if case['acceptance'] == 'application-with-constraint' else set()):
            raise ValueError('Malformed product probe output')
        return {'status': 'pass' if proc.returncode == 0 and all(value is True for value in checks.values()) else 'fail',
                'checks': checks, 'exit_code': proc.returncode, 'artifact': snapshot,
                'reason': 'Actual fixed local product probes; independent review and longtask completion not evaluated'}
    except (subprocess.TimeoutExpired, ValueError) as exc:
        return {'status': 'unable_to_verify', 'checks': {}, 'artifact': snapshot,
                'reason': type(exc).__name__}


def stage_binding(stage):
    return {key: stage.get(key) for key in ('phase', 'trace_path', 'trace_sha256', 'summary_path',
            'summary_sha256', 'prompt_path', 'prompt_sha256', 'collector_run_id', 'thread_id',
            'last_turn_id', 'status', 'failure_class', 'evaluated_archive_sha256',
            'failure_path', 'failure_sha256', 'artifact_error', 'collector_exit_code')}


def collect_case(args, case, source, out, inputs, snapshot_paths):
    workspace = out / (case['id'] + '-fixture')
    metadata = fixture(workspace, source, case)
    initial = freeze_fixture(workspace, out / (case['id'] + '-initial'))
    source_manifest = helper().source_manifest(source)
    archive = canonical(source_manifest)
    stages, thread = [], None
    for phase in case['phases']:
        task = out / (case['id'] + '-' + phase['id'] + '-prompt.txt')
        write(task, prompt(source, case, phase))
        raw = out / (case['id'] + '-' + phase['id'] + '-transport')
        before = inventory(workspace)
        command = [sys.executable, snapshot_paths['scripts/collect_host_trace.py'], '--out', str(raw),
                   '--cwd', str(workspace), '--synthetic-only', '--sandbox', 'workspace-write',
                   '--evaluated-archive-sha256', archive, '--retention-until', args.retention_until,
                   '--timeout', str(args.timeout), '--prompt-file', str(task),
                   '--config', 'mcp_servers.node_repl.enabled=false']
        if thread:
            command += ['--thread-id', thread]
        proc = subprocess.run(command, capture_output=True, text=True, check=False)
        summary_path = raw / 'summary.json'
        stage = {'phase': phase['id'], 'prompt_path': str(task), 'prompt_sha256': digest(task),
                 'collector_exit_code': proc.returncode, 'before': before, 'after': inventory(workspace),
                 'summary_path': str(summary_path), 'summary_sha256': None,
                 'trace_path': None, 'trace_sha256': None, 'status': 'unable_to_verify',
                 'failure_class': 'CollectorSummaryUnavailable', 'evaluated_archive_sha256': archive,
                 'observations': None}
        try:
            summary_bytes, summary_snapshot = helper().read_artifact(summary_path)
            stage['summary_sha256'] = summary_snapshot['sha256']
            if not summary_snapshot['safe'] or summary_bytes is None:
                raise ValueError('Collector summary unavailable or unsafe: ' + str(summary_snapshot['reason']))
            summary = json.loads(summary_bytes)
            if not isinstance(summary, dict) or summary.get('status') not in ('collected', 'unable_to_verify'):
                raise ValueError('Collector summary has invalid object/status structure')
            expected_trace = raw / 'trace.jsonl'
            if summary.get('trace_path') != str(expected_trace):
                raise ValueError('Collector summary trace locator differs from original raw directory')
            stage['trace_path'] = str(expected_trace)
            stage['trace_sha256'] = digest(expected_trace)
            if summary.get('trace_sha256') != stage['trace_sha256']:
                raise ValueError('Collector trace bytes differ from original summary digest')
            stage.update({key: summary.get(key) for key in ('status', 'failure_class', 'collector_run_id',
                         'thread_id', 'last_turn_id', 'trace_path', 'trace_sha256', 'evaluated_archive_sha256')})
            if stage['trace_path']:
                stage['observations'] = trace_observations(summary, stage['trace_path'], workspace)
            thread = stage.get('thread_id') or thread
        except (OSError, ValueError, KeyError, TypeError, UnicodeError) as exc:
            stage.update(status='unable_to_verify', failure_class=type(exc).__name__,
                         artifact_error=True, observations=None)
            # Preserve the original failed output and actual locators. Never replace
            # malformed transport with a synthetic successful summary/trace.
            locators = {}
            for label, path in (('summary', summary_path), ('trace', raw / 'trace.jsonl')):
                try:
                    observed_hash = digest(path)
                    locators[label] = {'path': str(path), 'sha256': observed_hash, 'error': None}
                    stage[label + '_path'], stage[label + '_sha256'] = str(path), observed_hash
                except (OSError, ValueError) as artifact_exc:
                    locators[label] = {'path': str(path), 'sha256': None, 'error': type(artifact_exc).__name__}
            failure = out / (case['id'] + '-' + phase['id'] + '-collector-failure.json')
            write_json(failure, {'exit_code': proc.returncode, 'stdout': proc.stdout, 'stderr': proc.stderr,
                                'raw_path': str(raw), 'failure_class': type(exc).__name__,
                                'failure_message': str(exc), 'locators': locators})
            stage['failure_path'], stage['failure_sha256'] = str(failure), digest(failure)
        stages.append(stage)
        if stage['status'] != 'collected' or helper().source_manifest(source) != source_manifest:
            break
    grade = acceptance(workspace, case, stages)
    if len(stages) != len(case['phases']) or helper().source_manifest(source) != source_manifest:
        grade = {'status': 'unable_to_verify', 'checks': {}, 'reason': 'Incomplete phases or evaluated source drift'}
    final = inventory(workspace)
    identity = {'sample_id': str(uuid.uuid4()), 'case_id': case['id'], 'case_target_sha256': canonical(case),
                'collection_inputs_sha256': inputs, 'collection_snapshot_paths': snapshot_paths,
                'source_path': str(source), 'source_sha256': source_manifest,
                'evaluated_archive_sha256': archive, 'fixture_path': str(workspace),
                'initial_fixture_path': str(out / (case['id'] + '-initial')),
                'initial_fixture_sha256': initial, 'stages': [stage_binding(stage) for stage in stages]}
    identity_path = out / (case['id'] + '-collection-identity.json')
    write_json(identity_path, identity)
    sample = {'case_id': case['id'], 'journey': case['journey'], 'supplemental': case.get('supplemental', False),
              'collection_identity': identity,
              'collection_identity_path': str(identity_path), 'collection_identity_sha256': digest(identity_path),
              'fixture_metadata': metadata, 'stages': stages, 'final_fixture_sha256': final,
              'grade': grade, 'semantic_judgments': [], 'semantic_status': 'unknown',
              'limitations': ['Synthetic critic is not actual independent review',
                              'Fixed product probe does not prove workflow completion or authorization correctness']}
    sample['sample_sha256'] = canonical(sample)
    return sample


def judgment_errors(judgment, sample, private):
    """Optional independent interpretation must bind actual source, target, trace."""
    identity = sample['collection_identity']
    expected = {'collection_identity_sha256': sample['collection_identity_sha256'],
                'source_manifest_sha256': identity['evaluated_archive_sha256'],
                'case_target_sha256': identity['case_target_sha256'],
                'trace_sha256': [stage['trace_sha256'] for stage in identity['stages']]}
    errors = []
    if judgment.get('binding') != expected or not judgment.get('reviewer') or not judgment.get('evidence'):
        errors.append('Semantic judgment lacks source/target/trace/identity binding or evidence')
    if judgment.get('status') not in ('pass', 'fail', 'unable_to_verify'):
        errors.append('Invalid semantic judgment status')
    if not re.fullmatch(r'[0-9a-f]{64}', str(judgment.get('artifact_sha256', ''))):
        errors.append('Semantic judgment lacks actual artifact digest')
    if private:
        content, snapshot = helper().read_artifact(judgment.get('artifact_path', ''))
        if not snapshot['safe'] or snapshot['sha256'] != judgment.get('artifact_sha256'):
            errors.append('Private semantic judgment artifact binding mismatch')
        elif content is not None:
            saved = json.loads(content)
            if any(saved.get(key) != judgment.get(key) for key in ('binding', 'reviewer', 'status', 'evidence')):
                errors.append('Semantic judgment differs from original artifact')
    return errors


def semantic_status(judgments):
    """Conservative aggregate of retained judgments, never an optimistic label."""
    if not judgments:
        return 'unknown'
    statuses = [judgment.get('status') for judgment in judgments]
    if 'fail' in statuses:
        return 'fail'
    if 'unable_to_verify' in statuses:
        return 'unable_to_verify'
    return 'pass' if all(status == 'pass' for status in statuses) else 'unknown'


def verify(data, private=False):
    errors = []
    if data.get('schema_version') != 1 or data.get('suite') != 'source_explicit_progress_journeys':
        return ['Invalid progress results identity']
    current = bindings()
    if data.get('inputs_sha256') != current:
        errors.append('Saved progress input binding is stale; collect new samples, never rebind')
    cases = {case['id']: case for case in load_cases()[1]}
    samples = data.get('samples', [])
    if not isinstance(samples, list) or not samples:
        return errors + ['No retained real journey samples']
    run_ids, thread_ids, sample_ids = [], [], []
    for sample in samples:
        try:
            identity = sample['collection_identity']
            case = cases[sample['case_id']]
            if sample.get('supplemental') != case.get('supplemental', False):
                errors.append('Supplemental case classification differs from fixed corpus')
            if sample.get('sample_sha256') != canonical({key: value for key, value in sample.items() if key != 'sample_sha256'}):
                errors.append('Saved sample changed after collection')
            if identity['case_id'] != case['id'] or identity['case_target_sha256'] != canonical(case):
                errors.append('Original case target binding mismatch')
            if identity['collection_inputs_sha256'] != data.get('inputs_sha256'):
                errors.append('Original collection inputs cannot be rebound')
            if set(identity['collection_snapshot_paths']) != set(BINDINGS):
                errors.append('Original private input snapshot locators are incomplete')
            if identity['evaluated_archive_sha256'] != canonical(identity['source_sha256']):
                errors.append('Original source manifest binding mismatch')
            if identity['stages'] != [stage_binding(stage) for stage in sample['stages']]:
                errors.append('Original host identity or evidence cannot be rebound')
            actual_phases = [stage['phase'] for stage in sample['stages']]
            expected_phases = [phase['id'] for phase in case['phases']]
            if not actual_phases or actual_phases != expected_phases[:len(actual_phases)]:
                errors.append('Observed phases differ from fixed journey order')
            if sample['grade']['status'] == 'pass' and (actual_phases != expected_phases or
                    any(stage['status'] != 'collected' or stage.get('collector_exit_code') != 0 for stage in sample['stages'])):
                errors.append('Deterministic success cannot replace missing/failed host phases')
            if sample['grade']['status'] not in ('pass', 'fail', 'unable_to_verify'):
                errors.append('Invalid deterministic acceptance status')
            judgments = sample.get('semantic_judgments', [])
            if not isinstance(judgments, list) or any(not isinstance(judgment, dict) for judgment in judgments):
                errors.append('Semantic judgments must be a list of bound objects')
                judgments = []
            if sample.get('semantic_status') not in ('unknown', 'pass', 'fail', 'unable_to_verify'):
                errors.append('Invalid overall semantic status')
            if sample.get('semantic_status') != semantic_status(judgments):
                errors.append('Semantic status differs from conservative retained judgment aggregate')
            if sample.get('semantic_status') == 'pass' and not judgments:
                errors.append('Semantic success requires actual independent judgment')
            ids = {stage.get('thread_id') for stage in sample['stages'] if stage.get('thread_id')}
            if len(ids) > 1:
                errors.append('Continuation changed thread identity')
            thread_ids += list(ids)
            sample_ids.append(identity['sample_id'])
            for stage in sample['stages']:
                if stage.get('collector_run_id'):
                    run_ids.append(stage['collector_run_id'])
                if stage.get('status') not in ('collected', 'unable_to_verify'):
                    errors.append('Unknown collector phase status')
                if stage.get('status') == 'collected' and not all(stage.get(key) for key in
                        ('collector_run_id', 'thread_id', 'last_turn_id', 'trace_path', 'trace_sha256', 'summary_sha256')):
                    errors.append('Successful host phase lacks original observed identity/evidence')
                if stage.get('evaluated_archive_sha256') != identity['evaluated_archive_sha256']:
                    errors.append('Collector source binding mismatch')
            for judgment in judgments:
                errors += judgment_errors(judgment, sample, private)
            if private:
                if digest(sample['collection_identity_path']) != sample['collection_identity_sha256']:
                    errors.append('Original private collection identity changed')
                elif json.loads(Path(sample['collection_identity_path']).read_text()) != identity:
                    errors.append('Private collection identity differs from public result')
                for name, path in identity['collection_snapshot_paths'].items():
                    if digest(path) != identity['collection_inputs_sha256'][name]:
                        errors.append('Original private collection input changed: ' + name)
                if helper().source_manifest(Path(identity['source_path'])) != identity['source_sha256']:
                    errors.append('Original private evaluated source changed')
                if inventory(Path(identity['initial_fixture_path'])) != identity['initial_fixture_sha256']:
                    errors.append('Original private initial fixture changed')
                if inventory(Path(identity['fixture_path'])) != sample['final_fixture_sha256']:
                    errors.append('Actual final fixture binding mismatch')
                for stage in sample['stages']:
                    for path_key, hash_key in (('trace_path', 'trace_sha256'), ('summary_path', 'summary_sha256'),
                                              ('prompt_path', 'prompt_sha256'), ('failure_path', 'failure_sha256')):
                        if stage.get(hash_key) and digest(stage[path_key]) != stage[hash_key]:
                            errors.append('Private ' + path_key + ' binding mismatch')
                    if stage.get('artifact_error'):
                        if not stage.get('failure_sha256') or stage.get('status') != 'unable_to_verify':
                            errors.append('Malformed collector output requires retained failure artifact')
                        else:
                            failure = json.loads(Path(stage['failure_path']).read_text())
                            for locator in failure['locators'].values():
                                try:
                                    current_locator = {'path': locator['path'], 'sha256': digest(locator['path']), 'error': None}
                                except (OSError, ValueError) as exc:
                                    current_locator = {'path': locator['path'], 'sha256': None, 'error': type(exc).__name__}
                                if current_locator != locator:
                                    errors.append('Original failed collector artifact presence/hash changed')
                    elif stage.get('summary_sha256'):
                        summary = json.loads(Path(stage['summary_path']).read_text())
                        if any(summary.get(key) != stage.get(key) for key in ('status', 'failure_class', 'collector_run_id',
                               'thread_id', 'last_turn_id', 'trace_path', 'trace_sha256', 'evaluated_archive_sha256')):
                            errors.append('Collector summary differs from saved identity')
                        if stage.get('trace_path') and trace_observations(summary, stage['trace_path'], identity['fixture_path']) != stage['observations']:
                            errors.append('Trace-derived observations differ from actual transport')
        except (OSError, ValueError, KeyError, TypeError) as exc:
            errors.append('Malformed or unavailable bound evidence: ' + type(exc).__name__)
    for label, values in (('sample', sample_ids), ('collector run', run_ids), ('independent thread', thread_ids)):
        if len(values) != len(set(values)):
            errors.append('Independent samples reuse ' + label + ' identity')
    return errors


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--collect', action='store_true')
    ap.add_argument('--verify-private', action='store_true')
    ap.add_argument('--case', choices=sorted(CASE_IDS))
    ap.add_argument('--results', type=Path, default=ROOT / 'evals/progress_results.json')
    ap.add_argument('--out', type=Path)
    ap.add_argument('--candidate', type=Path, default=ROOT)
    ap.add_argument('--retention-until', type=helper().collector_api().retention_deadline)
    ap.add_argument('--timeout', type=helper().collector_api().finite_timeout, default=120)
    args = ap.parse_args()
    if args.collect:
        if args.verify_private or not args.out or not args.retention_until:
            ap.error('Collection requires new --out and --retention-until; cannot combine --verify-private')
        # Reserve the final file before any model call. Keep its descriptor pinned
        # so substituted ancestors cannot redirect the final write after collection.
        result_path, result_parent = output_parent(args.results)
        try:
            result_fd = os.open(result_path.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                0o600, dir_fd=result_parent)
        finally:
            os.close(result_parent)
        out, directory_fd = helper().collector_api().private_output(args.out)
        os.close(directory_fd)
        frozen_inputs = bindings()
        paths = {}
        for name, expected in frozen_inputs.items():
            content, snapshot = helper().read_artifact(ROOT / name)
            if not snapshot['safe'] or snapshot['sha256'] != expected:
                raise ValueError('Collection input changed before snapshot: ' + name)
            target = out / 'inputs' / name
            write(target, content)
            paths[name] = str(target)
        source = out / 'source-candidate'
        helper().copy_source_snapshot(args.candidate, source)
        cases = [case for case in load_cases()[1] if not args.case or case['id'] == args.case]
        data = {'schema_version': 1, 'suite': 'source_explicit_progress_journeys',
                'collected_at': datetime.now(timezone.utc).isoformat(), 'inputs_sha256': frozen_inputs,
                'retention_until': args.retention_until, 'samples': [],
                'assurance': 'Real source-explicit synthetic observation; no installation/release/performance claim'}
        for case in cases:
            sample = collect_case(args, case, source, out, frozen_inputs, paths)
            data['samples'].append(sample)
            # An immutable per-case result survives later fixture/runner failures.
            write_json(out / (case['id'] + '-result.json'), sample)
            print(json.dumps({'case': case['id'], 'deterministic_acceptance': sample['grade']['status'],
                              'semantic_status': sample['semantic_status']}, ensure_ascii=False), flush=True)
        with os.fdopen(result_fd, 'w', encoding='utf-8') as result_stream:
            result_stream.write(json.dumps(data, ensure_ascii=False, indent=2) + '\n')
        errors = verify(data, private=True)
    else:
        if args.out or args.retention_until or args.case:
            ap.error('--out, --retention-until and --case apply only to collection')
        data = json.loads(args.results.read_text())
        errors = verify(data, private=args.verify_private)
    print(json.dumps({'status': 'pass' if not errors else 'fail', 'errors': errors,
                      'retained_samples': len(data.get('samples', [])),
                      'release_gate': 'not_evaluated', 'performance_improvement': 'unknown'}, ensure_ascii=False))
    return 1 if errors else 0


if __name__ == '__main__':
    raise SystemExit(main())
