"""供会话适配器和提前通知消费的有界宿主快照；不管理会话、授权或任务状态。"""
from __future__ import annotations

from collections import deque
import json
import os
from pathlib import Path
import stat
import time
import uuid

MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_RECORD_BYTES = 2 * 1024 * 1024
MAX_SCAN_SECONDS = 5
USER_TEXT_LIMIT = 6000
USER_MESSAGE_LIMIT = 8
TRUNCATION_MARKER = "[truncated]"


class SourceError(Exception):
    """固定错误码；不得附带宿主内容、路径或配置。"""


def _decode(raw):
    def unique_pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError
            result[key] = value
        return result

    value = json.loads(raw, object_pairs_hook=unique_pairs,
                       parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    if not isinstance(value, dict):
        raise ValueError
    return value


def _open_source(path, sessions_root):
    """逐级 openat，拒绝目录及最终文件的链接，不在校验后重新按路径打开。"""
    path, sessions_root = Path(path), Path(sessions_root)
    if (not path.is_absolute() or not sessions_root.is_absolute()
            or '..' in path.parts or '..' in sessions_root.parts):
        raise SourceError("permission_source_unavailable")
    try:
        relative = path.relative_to(sessions_root)
    except ValueError:
        raise SourceError("permission_source_unavailable") from None
    if not relative.parts:
        raise SourceError("permission_source_unavailable")
    # The runtime backend pins each ancestor and rejects POSIX links / Windows
    # reparse points. Do not resolve and then reopen an ordinary path.
    import longtask_state as runtime
    directory = runtime.open_directory_path(path.parent, create=False)
    try:
        return runtime.fs.open(path.name, os.O_RDONLY | runtime.fs.O_NOFOLLOW |
                               runtime.fs.O_NONBLOCK, dir_fd=directory)
    finally:
        os.close(directory)


def _policy(payload, source_thread_id, root, *, settings):
    if settings:
        if payload.get('thread_id') != source_thread_id:
            raise SourceError("permission_source_identity_mismatch")
        payload = payload.get('thread_settings')
    if not isinstance(payload, dict):
        raise SourceError("permission_source_record_invalid")
    if ('thread_id' in payload and payload['thread_id'] != source_thread_id
            or payload.get('cwd') != str(root)):
        raise SourceError("permission_source_identity_mismatch")
    profile = payload.get('active_permission_profile')
    if (payload.get('permission_profile') != {'type': 'disabled'}
            or not isinstance(profile, dict)
            or profile.get('id') != ':danger-full-access'
            or set(profile) - {'id', 'extends'}
            or profile.get('extends') is not None
            or (not settings and payload.get('sandbox_policy') != {'type': 'danger-full-access'})
            or (settings and 'sandbox_policy' in payload
                and payload['sandbox_policy'] != {'type': 'danger-full-access'})):
        raise SourceError("permission_source_policy_unsupported")
    approval, reviewer = payload.get('approval_policy'), payload.get('approvals_reviewer')
    if (not isinstance(approval, str) or approval not in {'never', 'on-request', 'untrusted'}
            or not isinstance(reviewer, str)
            or reviewer not in {'user', 'auto_review', 'guardian_subagent'}):
        raise SourceError("permission_source_policy_unsupported")
    return {'approvalPolicy': approval, 'approvalsReviewer': reviewer}


def _user_text(record):
    payload = record.get('payload')
    if (record.get('type') != 'response_item' or not isinstance(payload, dict)
            or payload.get('type') != 'message' or payload.get('role') != 'user'):
        return None
    content = payload.get('content')
    if not isinstance(content, list):
        return None
    pieces = [item['text'] for item in content
              if isinstance(item, dict) and item.get('type') == 'input_text'
              and isinstance(item.get('text'), str)]
    return '\n'.join(pieces) if pieces else None


def _bounded_messages(messages, total_count):
    truncated = total_count > len(messages) or sum(map(len, messages)) > USER_TEXT_LIMIT
    budget = USER_TEXT_LIMIT - (len(TRUNCATION_MARKER) if truncated else 0)
    result = []
    for message in reversed(messages):
        if budget <= 0:
            break
        fragment = message if len(message) <= budget else message[-budget:]
        result.append(fragment)
        budget -= len(fragment)
    result.reverse()
    if truncated:
        if result:
            result[0] = TRUNCATION_MARKER + result[0]
        else:
            result = [TRUNCATION_MARKER]
    return result


def read_source_snapshot(path, source_thread_id, root, *, sessions_root=None,
                         include_user_messages=False, include_context=False):
    """读取最新宿主记录；并非原子 live 权限，也不是可转移的用户批准。"""
    return _read_snapshot(path, source_thread_id, root, sessions_root=sessions_root,
                          include_user_messages=include_user_messages,
                          include_context=include_context)


def read_context_usage(path, source_thread_id, root, *, sessions_root=None):
    """只返回新鲜来源的最近请求计数，不要求或输出权限/用户文本。"""
    return _read_snapshot(path, source_thread_id, root, sessions_root=sessions_root,
                          include_context=True, context_only=True)


def _read_snapshot(path, source_thread_id, root, *, sessions_root=None,
                   include_user_messages=False, include_context=False, context_only=False):
    descriptor = None
    try:
        try:
            if (not isinstance(source_thread_id, str)
                    or str(uuid.UUID(source_thread_id)) != source_thread_id):
                raise ValueError
        except (ValueError, AttributeError):
            raise SourceError("permission_source_identity_mismatch") from None
        root = Path(root)
        if not root.is_absolute() or '..' in root.parts:
            raise SourceError("permission_source_identity_mismatch")
        if sessions_root is None:
            sessions_root = Path(os.environ.get('CODEX_HOME', str(Path.home() / '.codex'))) / 'sessions'
        deadline = time.monotonic() + MAX_SCAN_SECONDS
        descriptor = _open_source(path, sessions_root)
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise SourceError("permission_source_unavailable")
        if before.st_size > MAX_FILE_BYTES:
            raise SourceError("permission_source_limit_exceeded")
        if not before.st_size:
            raise SourceError("permission_source_record_invalid")
        if context_only and not -5 <= time.time() - before.st_mtime <= 300:
            raise SourceError("permission_source_unavailable")
        identity_seen = False
        latest = None
        context_usage = None
        messages = deque(maxlen=USER_MESSAGE_LIMIT)
        user_count = 0
        remaining = before.st_size
        with os.fdopen(descriptor, 'rb') as stream:
            descriptor = None
            while remaining:
                if time.monotonic() > deadline:
                    raise SourceError("permission_source_limit_exceeded")
                raw = stream.readline(min(MAX_RECORD_BYTES + 1, remaining))
                remaining -= len(raw)
                if len(raw) > MAX_RECORD_BYTES:
                    raise SourceError("permission_source_limit_exceeded")
                if not raw or not raw.endswith(b'\n'):
                    raise SourceError("permission_source_record_invalid")
                record = _decode(raw)
                kind, payload = record.get('type'), record.get('payload')
                if kind == 'session_meta':
                    if (identity_seen or not isinstance(payload, dict)
                            or payload.get('id') != source_thread_id
                            or payload.get('cwd') != str(root)):
                        raise SourceError("permission_source_identity_mismatch")
                    identity_seen = True
                elif not identity_seen:
                    raise SourceError("permission_source_identity_mismatch")
                if kind == 'turn_context':
                    latest = (payload, False)
                elif (kind == 'event_msg' and isinstance(payload, dict)
                      and payload.get('type') == 'thread_settings_applied'):
                    latest = (payload, True)
                if (include_context and kind == 'event_msg' and isinstance(payload, dict)
                        and payload.get('type') == 'token_count'):
                    context_usage = None
                    info = payload.get('info')
                    if isinstance(info, dict):
                        usage = info.get('last_token_usage')
                        window = info.get('model_context_window')
                        tokens = usage.get('total_tokens') if isinstance(usage, dict) else None
                        if (type(tokens) is int and type(window) is int
                                and tokens >= 0 and window > 0):
                            context_usage = {'lastRequestTokens': tokens, 'modelContextWindow': window}
                        else:
                            context_usage = None
                if include_user_messages:
                    message = _user_text(record)
                    if message is not None:
                        messages.append(message)
                        user_count += 1
            after = os.fstat(stream.fileno())
            if (after.st_nlink != 1 or after.st_size < before.st_size
                    or (after.st_size == before.st_size
                        and (after.st_mtime_ns, after.st_ctime_ns)
                        != (before.st_mtime_ns, before.st_ctime_ns))):
                raise SourceError("permission_source_unavailable")
        if time.monotonic() > deadline:
            raise SourceError("permission_source_limit_exceeded")
        if not identity_seen:
            raise SourceError("permission_source_record_invalid")
        if context_only:
            return context_usage
        if latest is None:
            raise SourceError("permission_source_record_invalid")
        policy = _policy(latest[0], source_thread_id, root, settings=latest[1])
        result = {'start': {**policy, 'permissions': ':danger-full-access'},
                  'expected': {**policy, 'sandbox': {'type': 'dangerFullAccess'},
                               'activePermissionProfile': {'id': ':danger-full-access'}},
                  'sourceThreadId': source_thread_id, 'observation': 'host_recorded_snapshot'}
        if include_user_messages:
            result['userMessages'] = _bounded_messages(messages, user_count)
            result['historyTruncated'] = (user_count > len(messages)
                                          or sum(map(len, messages)) > USER_TEXT_LIMIT)
        if include_context:
            result['contextUsage'] = context_usage
        return result
    except SourceError:
        raise
    except (OSError, ValueError, TypeError, UnicodeError, RuntimeError, RecursionError):
        raise SourceError("permission_source_unavailable") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
