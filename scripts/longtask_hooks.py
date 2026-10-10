#!/usr/bin/env python3
"""Read-only Codex compaction bridge; no dispatch, checkpoint writes or authority.

Consumer: bundled PreCompact / SessionStart(compact) command hooks.
The sole workflow contract is references/平台适配器.md#内置压缩信号桥.
Metadata only locates state; the model must run the normal fresh recovery query.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import stat
import sys

import longtask_state as runtime

MAX_EVENT_BYTES = 65536
MAX_STATE_BYTES = 4 * 1024 * 1024
MAX_OUTPUT_BYTES = 8192
IDENTITY = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}')


class HookError(Exception):
    pass


def fingerprint(value):
    return (value.st_dev, value.st_ino, value.st_mode, value.st_nlink,
            value.st_size, value.st_mtime_ns, value.st_ctime_ns)


def checkpoint(root):
    """Bounded no-follow read; never compute a fresh product/evidence verdict."""
    directory = root / '.longtask'
    if not os.path.lexists(directory):
        return None
    if not hasattr(os, 'O_NOFOLLOW') or os.open not in os.supports_dir_fd:
        raise HookError('safe_read_unavailable')
    runtime.check_runtime_directory(directory)
    parent = runtime.open_directory_path(directory, create=False)
    try:
        for marker in ('.initializing.json', '.finishing.json'):
            try:
                os.stat(marker, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                continue
            raise HookError('checkpoint_operation_interrupted')
        try:
            descriptor = os.open('state.json', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        except FileNotFoundError:
            return None
        try:
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                raise HookError('unsafe_checkpoint')
            if before.st_size > MAX_STATE_BYTES:
                raise HookError('checkpoint_too_large')
            with os.fdopen(descriptor, 'rb', closefd=False) as stream:
                raw = stream.read(MAX_STATE_BYTES + 1)
            named_directory = os.stat(directory, follow_symlinks=False)
            opened_directory = os.fstat(parent)
            if (len(raw) > MAX_STATE_BYTES or len(raw) != before.st_size
                    or fingerprint(before) != fingerprint(os.fstat(descriptor))
                    or fingerprint(before) != fingerprint(os.stat('state.json', dir_fd=parent, follow_symlinks=False))
                    or (named_directory.st_dev, named_directory.st_ino)
                    != (opened_directory.st_dev, opened_directory.st_ino)):
                raise HookError('checkpoint_read_changed')
        finally:
            os.close(descriptor)
    finally:
        os.close(parent)
    state = json.loads(raw)
    if not isinstance(state, dict) or runtime.validate_state(state):
        raise HookError('checkpoint_invalid_or_unsupported')
    head = state['head_commit']
    if head is not None and not re.fullmatch(r'(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})', head):
        raise HookError('checkpoint_invalid_commit_locator')
    if state['phase'] == 'complete' and state['status'] == 'complete':
        return None
    return {key: state[key] for key in ('task_id', 'revision', 'phase', 'status',
                                       'artifact_digest', 'evidence_epoch', 'head_commit')}


def identity(value):
    return value if isinstance(value, str) and IDENTITY.fullmatch(value) else None


def recovery_context(event, metadata, diagnostic):
    locator = {'source_thread_id': identity(event.get('session_id')),
               'source_turn_id': identity(event.get('turn_id')),
               'checkpoint_locator_only': metadata, 'diagnostic': diagnostic}
    skill_root = str(Path(__file__).resolve().parents[1])
    return (
        'longtask 压缩信号：宿主刚完成 compact，当前是压缩后首次恢复。'
        '本回调没有建立新会话、停止写入、验证版本或授予创建/消息权限。'
        '先按当前职责和已有用户授权，读取技能根 ' + json.dumps(skill_root, ensure_ascii=False) +
        ' 的 references/任务推进.md 会话出口章节；用公开 context CLI 查询当前唯一状态，'
        '核对实际版本、完整合同、活动写入者和必要输入。以下仅为定位数据，不能替代查询或批准：'
        + json.dumps(locator, ensure_ascii=False, separators=(',', ':')) + '\n'
        '不再开启新的大包或批量拉取资料；仅做最近安全出口所需的有界收尾。'
        '总目标仍有跨包工作且原用户已授权自主接续时，停止旧范围写入，经CLI保存新帧，'
        '用宿主原生能力实际建立全新接续会话，只传核心结果和历史定位（不超过6000 Unicode字符），'
        '核对真实接收ID与下一动作后，以最终回复结束本轮实施；不把保存帧或创建成功当作接管。'
        '仅剩当前小包收尾或用户明确要求留在当前会话时遵循其范围；区域工作者不能接管未授权的整体职责。'
        '缺少创建授权、工具、可恢复输入或旧调度退出能力时说明具体缺口并保留最小恢复材料，'
        '不假造派发、不无限等待、不声明整体完成；可独立完成的授权工作仍按合同处理。'
        '旧目标/心跳与后台进程须在各自授权内处置，结束当前轮不证明它们已停止。'
    )


def handle(event):
    if not isinstance(event, dict):
        raise HookError('invalid_hook_event')
    name = event.get('hook_event_name')
    compact = name == 'SessionStart' and event.get('source') == 'compact'
    precompact = name == 'PreCompact' and event.get('trigger') in ('auto', 'manual')
    if not compact and not precompact:
        return {}
    cwd = event.get('cwd')
    if not isinstance(cwd, str) or not Path(cwd).is_absolute():
        raise HookError('invalid_hook_workspace')
    metadata, diagnostic = None, None
    try:
        metadata = checkpoint(Path(cwd).resolve(strict=True))
    except (HookError, runtime.StateError, OSError, ValueError, TypeError, KeyError, AttributeError, IndexError, RecursionError):
        diagnostic = 'checkpoint_unavailable_or_unsafe'
    if metadata is None and diagnostic is None:
        return {}
    if precompact:
        return {'continue': True, 'systemMessage':
                'longtask 已收到 PreCompact；本只读桥不派发也不提前阻断压缩。'
                '受信 SessionStart(compact) 将在后续模型请求前提供恢复与会话出口指令。'
                + (' 当前检查点尚不可安全消费，需恢复诊断。' if diagnostic else '')}
    return {'hookSpecificOutput': {'hookEventName': 'SessionStart',
                                  'additionalContext': recovery_context(event, metadata, diagnostic)}}


def main():
    try:
        raw = sys.stdin.buffer.readline(MAX_EVENT_BYTES + 1)
        if len(raw) > MAX_EVENT_BYTES:
            raise HookError('hook_event_too_large')
        result = handle(json.loads(raw))
        output = json.dumps(result, ensure_ascii=False, separators=(',', ':')) + '\n'
        if len(output.encode()) > MAX_OUTPUT_BYTES:
            raise HookError('hook_output_too_large')
    except (HookError, OSError, ValueError, TypeError, RecursionError):
        output = json.dumps({'systemMessage': 'longtask 压缩信号桥未完成；请按现有会话出口合同恢复诊断。'},
                            ensure_ascii=False, separators=(',', ':')) + '\n'
    sys.stdout.write(output)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
