#!/usr/bin/env python3
"""Opt-in synchronous PreCompact veto; no state access, dispatch or new authority.

Consumer: the user-configured PreCompact hook documented in 平台适配器.md.
Standalone so a reviewed copy can run without replacing an active plugin.
"""
from __future__ import annotations

import json
import os
import sys

MAX_EVENT_BYTES = 65536


def main():
    if os.name == 'nt':
        sys.stdout.reconfigure(encoding='utf-8', newline='\n')
        sys.stderr.reconfigure(encoding='utf-8', newline='\n')
    # This command is exclusively a PreCompact handler. Even damaged input must
    # preserve the user's veto; never echo untrusted event contents.
    diagnostic = False
    try:
        raw = sys.stdin.buffer.readline(MAX_EVENT_BYTES + 1)
        if len(raw) > MAX_EVENT_BYTES:
            raise ValueError('event_too_large')
        event = json.loads(raw)
        if not isinstance(event, dict) or event.get('hook_event_name') != 'PreCompact':
            raise ValueError('unexpected_event')
    except (OSError, ValueError, TypeError):
        diagnostic = True
    print(json.dumps({
        'continue': False,
        'stopReason': '用户已启用禁压缩门；在压缩前停止当前轮。',
        'systemMessage': (
            'longtask 已拒绝本次压缩。本钩子不创建接续会话；请核对实际接管状态及最近保存的恢复输入。'
            + (' 回调输入无法核对，仍保留禁压缩要求。' if diagnostic else '')
        ),
    }, ensure_ascii=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
