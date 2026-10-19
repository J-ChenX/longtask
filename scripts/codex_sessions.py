#!/usr/bin/env python3
"""可选 MCP 会话适配入口；只连接现有 Codex daemon，不管理 longtask 状态。"""
from __future__ import annotations

import argparse
import base64
import hashlib
import struct
import json
import os
from pathlib import Path
import selectors
import stat
import subprocess
import sys
import time
import uuid
import unicodedata

from session_permissions import SourceError, read_source_snapshot

MAX_FRAME = 2 * 1024 * 1024
PROMPT_LIMIT = 6000
PROTOCOLS = {"2024-11-05", "2025-03-26", "2025-06-18"}


class AdapterError(Exception):
    """对外仅返回固定错误，不泄漏宿主错误、提示词或凭证。"""


def parse_json(data):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result
    return json.loads(data, object_pairs_hook=pairs,
                      parse_constant=lambda value: (_ for _ in ()).throw(ValueError("nonfinite")))


def thread_id(value):
    if not isinstance(value, str):
        raise AdapterError("invalid_thread_id")
    try:
        if str(uuid.UUID(value)) != value:
            raise ValueError
    except ValueError:
        raise AdapterError("invalid_thread_id") from None
    return value


class Proxy:
    """串行、有界 JSON-RPC；连接失效后不重连、不重放任何请求。"""
    def __init__(self, codex, root):
        self.codex, self.root = codex, root
        self.process = None
        self.selector = selectors.DefaultSelector()
        self.writer = selectors.DefaultSelector()
        self.buffer = b""
        self.counter = 0
        self.failed = False

    def connect(self):
        if self.failed:
            raise AdapterError("connection_unavailable_no_retry")
        if self.process is not None:
            return
        try:
            self.process = subprocess.Popen(
                [self.codex, "app-server", "proxy"], cwd=self.root,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0,
            )
            os.set_blocking(self.process.stdin.fileno(), False)
            self.writer.register(self.process.stdin, selectors.EVENT_WRITE)
            self.selector.register(self.process.stdout, selectors.EVENT_READ)
            deadline = time.monotonic() + 10
            self.handshake(deadline)
            self.rpc("initialize", {"clientInfo": {"name": "longtask_sessions", "version": "1.0.0"},
                                    "capabilities": {"experimentalApi": True}},
                     timeout=max(0, deadline - time.monotonic()))
            self.send({"method": "initialized", "params": {}}, deadline)
        except (OSError, ValueError, UnicodeError, AdapterError):
            self.failed = True
            self.close()
            raise AdapterError("existing_daemon_unavailable") from None

    def receive(self, size, deadline):
        while len(self.buffer) < size:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not self.selector.select(remaining):
                raise AdapterError("rpc_timeout")
            data = os.read(self.process.stdout.fileno(), min(65536, MAX_FRAME + 1))
            if not data:
                raise AdapterError("transport_closed")
            self.buffer += data
            if len(self.buffer) > MAX_FRAME + 65536:
                raise AdapterError("host_response_too_large")
        result, self.buffer = self.buffer[:size], self.buffer[size:]
        return result

    def handshake(self, deadline):
        # proxy 转发原始 WebSocket-over-UDS 字节；不能把它当成 stdio JSONL。
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = ("GET / HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\n"
                   "Connection: Upgrade\r\nSec-WebSocket-Version: 13\r\n"
                   f"Sec-WebSocket-Key: {key}\r\n\r\n")
        self.write(request.encode("ascii"), deadline)
        header = bytearray()
        while not header.endswith(b"\r\n\r\n"):
            header.extend(self.receive(1, deadline))
            if len(header) > 8192:
                raise AdapterError("invalid_websocket_upgrade")
        lines = header.decode("ascii").split("\r\n")
        if lines[0].split(" ", 2)[:2] != ["HTTP/1.1", "101"]:
            raise AdapterError("invalid_websocket_upgrade")
        headers = {}
        for line in lines[1:-2]:
            name, value = line.split(":", 1)
            name = name.strip().lower()
            if name in headers:
                raise AdapterError("invalid_websocket_upgrade")
            headers[name] = value.strip()
        expected = base64.b64encode(hashlib.sha1(
            (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")).digest()).decode("ascii")
        if (headers.get("sec-websocket-accept") != expected
                or headers.get("upgrade", "").lower() != "websocket"
                or "upgrade" not in [token.strip() for token in headers.get("connection", "").lower().split(",")]
                or "sec-websocket-extensions" in headers or "sec-websocket-protocol" in headers):
            raise AdapterError("invalid_websocket_upgrade")

    def write(self, data, deadline):
        try:
            offset = 0
            while offset < len(data):
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not self.writer.select(remaining):
                    raise AdapterError("rpc_timeout")
                try:
                    written = os.write(self.process.stdin.fileno(), data[offset:])
                except BlockingIOError:
                    continue
                if written <= 0:
                    raise AdapterError("transport_failed")
                offset += written
        except (OSError, AttributeError, ValueError, AdapterError):
            self.failed = True
            raise AdapterError("transport_failed_or_timed_out") from None

    def frame(self, payload, opcode=1, deadline=None):
        deadline = deadline if deadline is not None else time.monotonic() + 10
        if len(payload) > MAX_FRAME:
            raise AdapterError("request_too_large")
        mask = os.urandom(4)
        size = len(payload)
        head = bytes((0x80 | opcode, 0x80 | min(size, 126))) if size < 65536 else bytes((0x80 | opcode, 0xff))
        if size >= 65536:
            head += struct.pack("!Q", size)
        elif size >= 126:
            head += struct.pack("!H", size)
        self.write(head + mask + bytes(value ^ mask[i % 4] for i, value in enumerate(payload)), deadline)

    def send(self, message, deadline=None):
        self.frame(json.dumps(message, ensure_ascii=False).encode("utf-8"), deadline=deadline)

    def message(self, deadline):
        parts = bytearray()
        started = False
        while True:
            first, second = self.receive(2, deadline)
            final, opcode = bool(first & 0x80), first & 15
            if first & 0x70 or second & 0x80:
                raise AdapterError("invalid_websocket_frame")
            size = second & 127
            if size == 126:
                size = struct.unpack("!H", self.receive(2, deadline))[0]
            elif size == 127:
                size = struct.unpack("!Q", self.receive(8, deadline))[0]
            if size > MAX_FRAME or len(parts) + size > MAX_FRAME:
                raise AdapterError("host_response_too_large")
            if opcode >= 8 and (not final or size > 125):
                raise AdapterError("invalid_websocket_frame")
            payload = self.receive(size, deadline)
            if opcode == 8:
                raise AdapterError("transport_closed")
            if opcode == 9:
                self.frame(payload, opcode=10, deadline=deadline)
                continue
            if opcode == 10:
                continue
            if opcode not in (0, 1) or (opcode == 0) != started:
                raise AdapterError("invalid_websocket_frame")
            parts.extend(payload)
            if final:
                return bytes(parts)
            started = True

    def rpc(self, method, params, timeout=10):
        if self.failed:
            raise AdapterError("connection_unavailable_no_retry")
        self.counter += 1
        request_id = self.counter
        deadline = time.monotonic() + timeout
        try:
            self.send({"id": request_id, "method": method, "params": params}, deadline)
            while True:
                if time.monotonic() >= deadline:
                    raise AdapterError("rpc_timeout")
                line = self.message(deadline)
                message = parse_json(line.decode("utf-8"))
                if not isinstance(message, dict):
                    raise AdapterError("invalid_host_response")
                if "method" in message:
                    # 适配层不能代替人类批准或执行动态工具；宿主可在原生界面处理。
                    if "id" in message:
                        self.send({"id": message["id"], "error": {
                            "code": -32601, "message": "Client action unavailable"}}, deadline)
                    continue
                if message.get("id") != request_id:
                    continue
                if "error" in message:
                    raise AdapterError("host_request_rejected")
                if not isinstance(message.get("result"), dict):
                    raise AdapterError("invalid_host_response")
                return message["result"]
        except (OSError, ValueError, UnicodeError, RecursionError, AdapterError):
            self.failed = True
            raise AdapterError("host_result_unconfirmed") from None

    def close(self):
        # 只停止自有 proxy；现有 daemon 和其管理的 turn 不属于本进程。
        if self.process is not None:
            if self.process.stdin and not self.process.stdin.closed:
                try:
                    self.process.stdin.close()
                except OSError:
                    pass
            try:
                self.process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                try:
                    self.process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=1)
            if self.process.stdout:
                self.process.stdout.close()
        self.selector.close()
        self.writer.close()


class Sessions:
    def __init__(self, root, proxy, allow_worktrees=False, permission_source_thread=None):
        self.root, self.proxy = root, proxy
        self.allow_worktrees = allow_worktrees
        self.creation_uncertain = False
        self.permission_source_thread = (thread_id(permission_source_thread)
                                         if permission_source_thread is not None else None)
        self.uncertain_messages = set()

    def source_snapshot(self, include_user_messages=False):
        """只采用用户绑定主窗口的宿主元数据；工具调用不能选择高权限来源。"""
        identity = self.permission_source_thread
        if identity is None:
            raise AdapterError("permission_source_not_configured")
        self.proxy.connect()
        result = self.proxy.rpc("thread/read", {"threadId": identity, "includeTurns": False})
        source = result.get("thread")
        if (not isinstance(source, dict) or source.get("id") != identity
                or source.get("cwd") != str(self.root) or not isinstance(source.get("path"), str)):
            raise AdapterError("permission_source_identity_unavailable")
        try:
            return read_source_snapshot(source["path"], identity, self.root,
                                        include_user_messages=include_user_messages)
        except SourceError as exc:
            raise AdapterError(str(exc)) from None

    @staticmethod
    def verify_permissions(result, snapshot):
        for field, expected in snapshot["expected"].items():
            actual = result.get(field)
            if field == "activePermissionProfile":
                if (not isinstance(actual, dict) or actual.get("id") != expected["id"]
                        or actual.get("extends") is not None):
                    raise AdapterError("created_permissions_mismatch")
            elif actual != expected:
                raise AdapterError("created_permissions_mismatch")

    def workspace(self, value=None):
        """默认固定根；显式启用后仅接受同仓库已登记的真实工作树根。"""
        if value is None:
            return self.root
        if (not isinstance(value, str) or not value or len(value) > 4096
                or any(unicodedata.category(ch).startswith("C") for ch in value)):
            raise AdapterError("invalid_workspace")
        target = Path(value)
        try:
            if not target.is_absolute() or target.resolve(strict=True) != target or not target.is_dir():
                raise AdapterError("invalid_workspace")
            if target == self.root:
                return target
            if not self.allow_worktrees:
                raise AdapterError("worktree_access_not_enabled")
            deadline = time.monotonic() + 3

            def git_path(directory, option):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AdapterError("worktree_identity_timeout")
                result = subprocess.run(
                    ["git", "-C", str(directory), "rev-parse", "--path-format=absolute", option],
                    capture_output=True, timeout=remaining, check=False,
                )
                if result.returncode or len(result.stdout) > 8192:
                    raise AdapterError("worktree_identity_unavailable")
                return Path(os.fsdecode(result.stdout).rstrip("\n"))

            if git_path(target, "--show-toplevel") != target:
                raise AdapterError("workspace_not_worktree_root")
            # 登记可残留，或原路径已被另一个目录替换；核对双方实际 Git 归属。
            common = [git_path(directory, "--git-common-dir").resolve(strict=True)
                      for directory in (self.root, target)]
            if common[0] != common[1]:
                raise AdapterError("workspace_repository_mismatch")
            admin = git_path(target, "--absolute-git-dir")
            marker = target / ".git"
            if admin.resolve(strict=True) != admin or marker.is_symlink():
                raise AdapterError("workspace_not_registered_worktree")
            if admin == common[0]:
                if not marker.is_dir() or marker.resolve(strict=True) != admin:
                    raise AdapterError("workspace_not_registered_worktree")
            else:
                # Git 旧版 porcelain 无 NUL 输出；直接核对工作树登记的双向链接，
                # 避免按行解析可含换行的路径，也拒绝复制/伪装的 .git 指针。
                if admin.parent != common[0] / "worktrees" or not marker.is_file():
                    raise AdapterError("workspace_not_registered_worktree")
                if not hasattr(os, "O_NOFOLLOW"):
                    raise AdapterError("safe_worktree_read_unavailable")
                descriptor = os.open(admin / "gitdir", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                try:
                    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                        raise AdapterError("workspace_not_registered_worktree")
                    backlink = os.read(descriptor, 8193)
                finally:
                    os.close(descriptor)
                if backlink != os.fsencode(marker) + b"\n":
                    raise AdapterError("workspace_not_registered_worktree")
        except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired):
            raise AdapterError("workspace_unavailable") from None
        return target

    def read(self, value, timeout=10, *, workspace=None):
        return self._read(value, self.workspace(workspace), timeout)

    def _read(self, value, root, timeout=10):
        identity = thread_id(value)
        self.proxy.connect()
        deadline = time.monotonic() + timeout
        summary = self.proxy.rpc("thread/read", {"threadId": identity, "includeTurns": False}, timeout)
        observed = summary.get("thread")
        if not isinstance(observed, dict):
            raise AdapterError("invalid_host_response")
        if observed.get("id") != identity or observed.get("cwd") != str(root):
            raise AdapterError("thread_outside_configured_workspace")
        result = self.proxy.rpc("thread/read", {"threadId": identity, "includeTurns": True},
                                max(.01, deadline - time.monotonic()))
        thread = result.get("thread")
        if not isinstance(thread, dict):
            raise AdapterError("invalid_host_response")
        if thread.get("id") != identity or thread.get("cwd") != str(root):
            raise AdapterError("thread_outside_configured_workspace")
        turns = thread.get("turns")
        if not isinstance(turns, list) or any(not isinstance(turn, dict) for turn in turns):
            raise AdapterError("invalid_host_response")
        turn = turns[-1] if turns else {}
        if turns and (not isinstance(turn.get("id"), str) or not isinstance(turn.get("status"), str)):
            raise AdapterError("invalid_host_response")
        items = turn.get("items", [])
        if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
            raise AdapterError("invalid_host_response")
        status = thread.get("status")
        if not isinstance(status, dict) or not isinstance(status.get("type"), str):
            raise AdapterError("invalid_host_response")
        messages = [item.get("text", "") for item in items
                    if item.get("type") == "agentMessage" and isinstance(item.get("text"), str)]
        text = "\n".join(messages)
        return {"threadId": identity, "cwd": str(root), "status": {"type": status["type"]},
                "turnId": turn.get("id"), "turnStatus": turn.get("status"),
                "latestAssistantText": text[-PROMPT_LIMIT:], "textTruncated": len(text) > PROMPT_LIMIT,
                "assurance": "observed_snapshot_not_takeover_approval"}

    def create(self, prompt, title, *, workspace=None):
        if (not isinstance(prompt, str) or not prompt.strip() or len(prompt) > PROMPT_LIMIT
                or not isinstance(title, str) or not title.strip() or len(title) > 80
                or any(unicodedata.category(ch).startswith("C") for ch in title)):
            raise AdapterError("invalid_creation_input")
        if self.creation_uncertain:
            raise AdapterError("prior_creation_unknown_do_not_retry")
        root = self.workspace(workspace)
        self.proxy.connect()  # 握手失败发生在创建前，不启动独立 app-server 兜底。
        snapshot = self.source_snapshot() if self.permission_source_thread else None
        identity = None
        stage = "thread/start"
        try:
            result = self.proxy.rpc(stage, {"cwd": str(root), "ephemeral": False,
                                           **(snapshot["start"] if snapshot else {})})
            created = result.get("thread")
            if not isinstance(created, dict):
                raise AdapterError("invalid_host_response")
            identity = thread_id(created.get("id"))
            if created.get("cwd") != str(root):
                raise AdapterError("unexpected_creation_workspace")
            if snapshot:
                stage = "permissions/verify"
                self.verify_permissions(result, snapshot)
                latest = self.source_snapshot()
                if latest["start"] != snapshot["start"] or latest["expected"] != snapshot["expected"]:
                    raise AdapterError("source_permissions_changed")
            stage = "thread/name/set"
            self.proxy.rpc(stage, {"threadId": identity, "name": title})
            stage = "turn/start"
            started = self.proxy.rpc(stage, {"threadId": identity,
                                           "input": [{"type": "text", "text": prompt}]})
            turn = started.get("turn", {})
            if not isinstance(turn, dict) or not isinstance(turn.get("id"), str) or not turn["id"]:
                raise AdapterError("turn_identity_missing")
            return {"status": "dispatched", "threadId": identity, "turnId": turn["id"],
                    "cwd": str(root), "takeoverConfirmed": False,
                    "permissionSync": "verified_host_snapshot" if snapshot else "host_default_unverified",
                    "permissionSourceThreadId": self.permission_source_thread,
                    "nextAction": "read_thread 核对基线与下一实际动作；派发成功不等于接管"}
        except (AdapterError, KeyError, TypeError):
            self.creation_uncertain = True
            if identity is not None:
                self.uncertain_messages.add(identity)
            return {"status": "unknown", "threadId": identity, "cwd": str(root), "stage": stage,
                    "retrySafe": False, "takeoverConfirmed": False,
                    "nextAction": "停止创建；用原生聊天列表核对结果，已知 ID 用 read_thread 查询；不得重放提示词"}

    def send(self, identity, message, *, workspace=None):
        identity = thread_id(identity)
        if not isinstance(message, str) or not message.strip() or len(message) > PROMPT_LIMIT:
            raise AdapterError("invalid_message")
        if identity in self.uncertain_messages:
            raise AdapterError("prior_message_unknown_do_not_retry")
        snapshot = self.read(identity, workspace=workspace)
        active = snapshot["status"]["type"] == "active" and snapshot["turnStatus"] == "inProgress"
        idle = (snapshot["status"]["type"] == "idle"
                and snapshot["turnStatus"] in (None, "completed", "failed", "interrupted"))
        if not active and not idle:
            raise AdapterError("thread_delivery_state_unknown")
        stage = "turn/steer" if active else "turn/start"
        params = {"threadId": identity, "input": [{"type": "text", "text": message}]}
        if active:
            params["expectedTurnId"] = snapshot["turnId"]
        try:
            result = self.proxy.rpc(stage, params)
            turn = result.get("turnId") if active else result.get("turn", {}).get("id")
            if not isinstance(turn, str) or not turn or (active and turn != snapshot["turnId"]):
                raise AdapterError("message_turn_unconfirmed")
            return {"status": "steered" if active else "dispatched", "threadId": identity,
                    "turnId": turn, "cwd": snapshot["cwd"], "receivedByModel": False,
                    "nextAction": "核对接收者实际动作；宿主接受不证明模型已消费，审批等待仍须按原权限处理"}
        except (AdapterError, AttributeError, TypeError):
            self.uncertain_messages.add(identity)
            return {"status": "unknown", "threadId": identity, "stage": stage,
                    "retrySafe": False, "receivedByModel": False,
                    "nextAction": "核对原操作与接收者；不重放、不改用排队或新turn重复投递"}

    def call(self, name, args):
        if not isinstance(args, dict):
            raise AdapterError("invalid_arguments")
        fields = set(args) - {"workspace"}
        if "workspace" in args and args["workspace"] is None:
            raise AdapterError("invalid_workspace")
        if name == "read_authorization" and not args:
            source = self.source_snapshot(include_user_messages=True)
            return {key: source[key] for key in ("sourceThreadId", "observation", "userMessages", "historyTruncated")}
        if name == "send_message_to_thread" and fields == {"threadId", "message"}:
            return self.send(args["threadId"], args["message"], workspace=args.get("workspace"))
        if name == "create_thread" and fields == {"prompt", "title"}:
            return self.create(args["prompt"], args["title"], workspace=args.get("workspace"))
        if name == "read_thread" and fields == {"threadId"}:
            return self.read(args["threadId"], workspace=args.get("workspace"))
        if name == "wait_threads" and fields == {"threadIds", "timeoutMs"}:
            ids, timeout = args["threadIds"], args["timeoutMs"]
            if (not isinstance(ids, list) or not 1 <= len(ids) <= 8
                    or type(timeout) is not int or not 0 <= timeout <= 30000):
                raise AdapterError("invalid_wait_input")
            identities = [thread_id(value) for value in ids]
            if len(set(identities)) != len(identities):
                raise AdapterError("invalid_wait_input")
            root = self.workspace(args.get("workspace"))
            self.proxy.connect()
            deadline = time.monotonic() + timeout / 1000
            transport_deadline = time.monotonic() + max(10, timeout / 1000)
            while True:
                # timeoutMs 限制轮询窗口；每个快照仍有独立的有界传输预算。
                snapshots = []
                for value in identities:
                    remaining = transport_deadline - time.monotonic()
                    if remaining <= 0:
                        raise AdapterError("snapshot_budget_exhausted")
                    snapshots.append(self._read(value, root, min(10, remaining)))
                if timeout == 0 or any(row["turnStatus"] in ("completed", "failed", "interrupted")
                                       for row in snapshots) or time.monotonic() >= deadline:
                    return {"threads": snapshots}
                time.sleep(min(.25, max(0, deadline - time.monotonic())))
                if time.monotonic() >= deadline:
                    return {"threads": snapshots}
        raise AdapterError("unknown_tool_or_arguments")


def tools():
    definitions = [
        ("create_thread", "在配置的本地目录创建全新 Codex 会话并派发一次提示词。须已有用户创建及派发授权；提示词不是新的人类授权。未知结果不重试，派发不代表接管。",
         {"prompt": {"type": "string", "minLength": 1, "maxLength": PROMPT_LIMIT},
          "title": {"type": "string", "minLength": 1, "maxLength": 80}}, False),
        ("read_thread", "读取同一配置目录的真实会话快照；只返回最近 turn 的助手文字，不返回命令、工具或用户历史。",
         {"threadId": {"type": "string", "format": "uuid"}}, True),
        ("wait_threads", "等待同一配置目录的 1–8 个会话，至多 30 秒。timeoutMs=0 取即时快照；不代表已接管。",
         {"threadIds": {"type": "array", "items": {"type": "string", "format": "uuid"},
                        "minItems": 1, "maxItems": 8, "uniqueItems": True},
          "timeoutMs": {"type": "integer", "minimum": 0, "maximum": 30000}}, True),
        ("send_message_to_thread", "在用户已授权的目标内直接发送协作消息：活动turn使用带expectedTurnId的steer，空闲时启动turn，不调用queue。未知结果不重试，宿主接受不表示模型已消费；不授予新权限。",
         {"threadId": {"type": "string", "format": "uuid"},
          "message": {"type": "string", "minLength": 1, "maxLength": PROMPT_LIMIT}}, False),
    ]
    catalog = [{"name": name, "description": description + " 可选 workspace 仅在服务显式启用 --allow-worktrees 时选择同仓库已登记的工作树；默认固定根，不创建工作树。" + (
                 " 若已配置 --permission-source-thread，每次创建应用主窗口最新可读取宿主权限快照，并在派发前核对实际返回；不继承单次批准。" if name == "create_thread" else ""),
             "inputSchema": {"type": "object", "properties": {**properties,
                             "workspace": {"type": "string", "minLength": 1, "maxLength": 4096}},
                             "required": list(properties), "additionalProperties": False},
             "annotations": {"readOnlyHint": read_only, "destructiveHint": False,
                             "idempotentHint": read_only, "openWorldHint": True}}
            for name, description, properties, read_only in definitions]
    catalog.append({"name": "read_authorization", "description": "只读用户已绑定主窗口的原始user文本，至多8条/6000字符；用于语义核验原始授权，不把智能体创建消息或转述升级为人类授权。无需输入路径或thread ID，不返回工具/助手历史。未配置来源时失败。",
                    "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
                    "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True,
                                    "openWorldHint": False}})
    return catalog


def serve(sessions, source, sink):
    initialized = False
    while True:
        line = source.readline(MAX_FRAME + 1)
        if not line:
            return
        response = None
        request_id = None
        try:
            if len(line) > MAX_FRAME:
                raise ValueError("frame too large")
            message = parse_json(line)
            if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
                raise ValueError("invalid request")
            request_id = message.get("id")
            if request_id is not None and type(request_id) not in (int, str):
                request_id = None
                raise ValueError("invalid request id")
            method, params = message.get("method"), message.get("params", {})
            if method == "notifications/initialized":
                continue
            if request_id is None:
                continue
            if not isinstance(params, dict):
                raise ValueError("invalid params")
            if method == "initialize":
                protocol = params.get("protocolVersion")
                if not isinstance(protocol, str):
                    raise ValueError("invalid protocol version")
                if protocol not in PROTOCOLS:
                    protocol = "2025-06-18"
                initialized = True
                response = {"protocolVersion": protocol, "capabilities": {"tools": {}},
                            "serverInfo": {"name": "longtask_sessions", "version": "1.0.0"}}
            elif not initialized:
                raise AdapterError("mcp_initialize_required")
            elif method == "tools/list":
                response = {"tools": tools()}
            elif method == "ping":
                response = {}
            elif method == "tools/call":
                try:
                    result = sessions.call(params.get("name"), params.get("arguments", {}))
                    response = {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}],
                                "isError": result.get("status") == "unknown"}
                except AdapterError as exc:
                    response = {"content": [{"type": "text", "text": str(exc)}], "isError": True}
            else:
                raise AdapterError("method_unavailable")
            output = {"jsonrpc": "2.0", "id": request_id, "result": response}
        except (ValueError, UnicodeError, RecursionError, AdapterError):
            output = {"jsonrpc": "2.0", "id": request_id,
                      "error": {"code": -32600, "message": "Invalid or unsupported request"}}
        sink.write((json.dumps(output, ensure_ascii=False) + "\n").encode())
        sink.flush()
        if len(line) > MAX_FRAME:
            return  # 不把超长帧剩余内容当成另一请求。


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, help="默认固定的本地工作区")
    parser.add_argument("--allow-worktrees", action="store_true",
                        help="显式允许 workspace 选择同仓库已登记的隔离工作树；不创建工作树")
    parser.add_argument("--permission-source-thread", help="用户绑定的主窗口UUID；每次创建只读其宿主权限快照并验证实际新窗口，不接受工具参数覆盖")
    parser.add_argument("--codex", default="codex", help="已安装 Codex CLI 的程序路径")
    parser.add_argument("--probe", action="store_true", help="只读握手检测现有 daemon，不创建会话")
    args = parser.parse_args()
    root = Path(args.root)
    if not root.is_absolute() or not root.is_dir() or root.resolve() != root:
        parser.error("root 必须是已存在、规范化且无链接别名的绝对目录")
    if args.permission_source_thread:
        try:
            thread_id(args.permission_source_thread)
        except AdapterError:
            parser.error("permission-source-thread 必须是规范UUID")
    proxy = Proxy(args.codex, root)
    try:
        if args.probe:
            proxy.connect()
            print(json.dumps({"status": "connected", "cwd": str(root),
                              "assurance": "handshake_only_no_creation_or_takeover"}))
        else:
            serve(Sessions(root, proxy, args.allow_worktrees, args.permission_source_thread),
                  sys.stdin.buffer, sys.stdout.buffer)
    except (AdapterError, OSError):
        print(json.dumps({"error": "existing_daemon_unavailable"}), file=sys.stderr)
        return 1
    finally:
        proxy.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
