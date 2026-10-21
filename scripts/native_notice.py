#!/usr/bin/env python3
"""Opt-in early continuity notice for native host tools; never dispatches chats.

Consumer / installation contract: references/全局接续.md. Ships with the complete
plugin; default hooks do not enable it. Permission snapshots and token observation
share the bounded reader, while context-only reads convey no authority.
"""
from __future__ import annotations

import json
import sys

from session_permissions import SourceError, _decode, read_context_usage

MAX_EVENT = 65536
POLICY = (
    '本钩子仅供用户明确选择全局禁压缩与自动接续后登记；先核对该选择及原始授权来源。'
    '已有原目标内创建接续聊天和发送必要指令的授权继续有效，不扩大业务权限。'
    '按安全片段提前交接；常规任务使用轻量核心结果和原聊天定位，活动longtask任务消费唯一检查点完整合同。'
    '停止旧范围及后台写入，保存目标、成果、未提交修改、未运行/失败检查、授权来源与剩余验收，'
    '使用当前宿主原生创建/读取/等待工具，确认接收者已核对基线并开始下一具体动作后结束旧会话实施。'
    '保存材料或创建回执不等于接管；创建/归属/接管失败须报告缺口，未知结果不重复创建。'
    '可立即完成的小任务直接收尾。此提示不派发、不阻止压缩；禁压缩门和接管能力分别核验。'
)
WARNING = (
    '全局接续提前预警：宿主记录的最近请求用量已达到上下文窗口的50%。'
    '这不是实时精确剩余量，后续工具输出还会增加占用。现在只做最近安全出口的有界收尾，'
    '不再开启大包或批量读取。仍有未完成工作时，按已有授权用当前宿主原生工具建立全新接续聊天，'
    '仅传目标、核心结果、必要定位、剩余验收与授权来源，保留未提交改动和失败/未运行检查。'
    '停止旧范围及后台写入，确认真实接收ID、基线核对及下一具体动作后结束旧会话实施。'
    '缺少工具或接管未确认时明确报告。可立即完成的小任务直接收尾。'
)
UNAVAILABLE = {'systemMessage': '全局接续提前提示未完成，不能据此判断预算安全；按安全片段推进并核对独立禁压缩门。'}


def notice(event):
    """Read-only hook response; no receipts, checkpoint or transport access."""
    try:
        if not isinstance(event, dict):
            raise ValueError('invalid_event')
        name = event.get('hook_event_name')
        if name == 'UserPromptSubmit':
            context = POLICY
        elif name == 'PostToolUse':
            usage = read_context_usage(event.get('transcript_path'), event.get('session_id'), event.get('cwd'))
            if usage is None:
                raise SourceError('context_unavailable')
            if usage['lastRequestTokens'] * 2 < usage['modelContextWindow']:
                return {}
            context = WARNING
        else:
            raise ValueError('unsupported_event')
        return {'hookSpecificOutput': {'hookEventName': name, 'additionalContext': context}}
    except (SourceError, OSError, ValueError, TypeError, KeyError, AttributeError, RuntimeError, RecursionError):
        return dict(UNAVAILABLE)


def main():
    try:
        raw = sys.stdin.buffer.readline(MAX_EVENT + 1)
        if len(raw) > MAX_EVENT:
            raise ValueError('event_too_large')
        result = notice(_decode(raw))
    except (ValueError, TypeError, UnicodeError, RecursionError):
        result = dict(UNAVAILABLE)
    # ASCII JSON survives native Windows legacy stdout encodings.
    sys.stdout.buffer.write(json.dumps(result, ensure_ascii=True).encode('ascii') + b'\n')


if __name__ == '__main__':
    main()
