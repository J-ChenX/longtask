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
import subprocess
import sys
import time
import uuid
import unicodedata

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
    def __init__(self, root, proxy):
        self.root, self.proxy = root, proxy
        self.creation_uncertain = False

    def read(self, value, timeout=10):
        identity = thread_id(value)
        self.proxy.connect()
        deadline = time.monotonic() + timeout
        summary = self.proxy.rpc("thread/read", {"threadId": identity, "includeTurns": False}, timeout)
        observed = summary.get("thread")
        if not isinstance(observed, dict):
            raise AdapterError("invalid_host_response")
        if observed.get("id") != identity or observed.get("cwd") != str(self.root):
            raise AdapterError("thread_outside_configured_workspace")
        result = self.proxy.rpc("thread/read", {"threadId": identity, "includeTurns": True},
                                max(.01, deadline - time.monotonic()))
        thread = result.get("thread")
        if not isinstance(thread, dict):
            raise AdapterError("invalid_host_response")
        if thread.get("id") != identity or thread.get("cwd") != str(self.root):
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
        return {"threadId": identity, "cwd": str(self.root), "status": {"type": status["type"]},
                "turnId": turn.get("id"), "turnStatus": turn.get("status"),
                "latestAssistantText": text[-PROMPT_LIMIT:], "textTruncated": len(text) > PROMPT_LIMIT,
                "assurance": "observed_snapshot_not_takeover_approval"}

    def create(self, prompt, title):
        if (not isinstance(prompt, str) or not prompt.strip() or len(prompt) > PROMPT_LIMIT
                or not isinstance(title, str) or not title.strip() or len(title) > 80
                or any(unicodedata.category(ch).startswith("C") for ch in title)):
            raise AdapterError("invalid_creation_input")
        if self.creation_uncertain:
            raise AdapterError("prior_creation_unknown_do_not_retry")
        self.proxy.connect()  # 握手失败发生在创建前，不启动独立 app-server 兜底。
        identity = None
        stage = "thread/start"
        try:
            result = self.proxy.rpc(stage, {"cwd": str(self.root), "ephemeral": False})
            created = result.get("thread")
            if not isinstance(created, dict):
                raise AdapterError("invalid_host_response")
            identity = thread_id(created.get("id"))
            if created.get("cwd") != str(self.root):
                raise AdapterError("unexpected_creation_workspace")
            stage = "thread/name/set"
            self.proxy.rpc(stage, {"threadId": identity, "name": title})
            stage = "turn/start"
            started = self.proxy.rpc(stage, {"threadId": identity,
                                           "input": [{"type": "text", "text": prompt}]})
            turn = started.get("turn", {})
            if not isinstance(turn, dict) or not isinstance(turn.get("id"), str) or not turn["id"]:
                raise AdapterError("turn_identity_missing")
            return {"status": "dispatched", "threadId": identity, "turnId": turn["id"],
                    "cwd": str(self.root), "takeoverConfirmed": False,
                    "nextAction": "read_thread 核对基线与下一实际动作；派发成功不等于接管"}
        except (AdapterError, KeyError, TypeError):
            self.creation_uncertain = True
            return {"status": "unknown", "threadId": identity, "stage": stage,
                    "retrySafe": False, "takeoverConfirmed": False,
                    "nextAction": "停止创建；用原生聊天列表核对结果，已知 ID 用 read_thread 查询；不得重放提示词"}

    def call(self, name, args):
        if not isinstance(args, dict):
            raise AdapterError("invalid_arguments")
        if name == "create_thread" and set(args) == {"prompt", "title"}:
            return self.create(args["prompt"], args["title"])
        if name == "read_thread" and set(args) == {"threadId"}:
            return self.read(args["threadId"])
        if name == "wait_threads" and set(args) == {"threadIds", "timeoutMs"}:
            ids, timeout = args["threadIds"], args["timeoutMs"]
            if (not isinstance(ids, list) or not 1 <= len(ids) <= 8
                    or type(timeout) is not int or not 0 <= timeout <= 30000):
                raise AdapterError("invalid_wait_input")
            identities = [thread_id(value) for value in ids]
            if len(set(identities)) != len(identities):
                raise AdapterError("invalid_wait_input")
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
                    snapshots.append(self.read(value, min(10, remaining)))
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
    ]
    return [{"name": name, "description": description,
             "inputSchema": {"type": "object", "properties": properties,
                             "required": list(properties), "additionalProperties": False},
             "annotations": {"readOnlyHint": read_only, "destructiveHint": False,
                             "idempotentHint": read_only, "openWorldHint": True}}
            for name, description, properties, read_only in definitions]


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
    parser.add_argument("--root", required=True, help="固定允许的本地工作区；不在工具参数中接受其他目录")
    parser.add_argument("--codex", default="codex", help="已安装 Codex CLI 的程序路径")
    parser.add_argument("--probe", action="store_true", help="只读握手检测现有 daemon，不创建会话")
    args = parser.parse_args()
    root = Path(args.root)
    if not root.is_absolute() or not root.is_dir() or root.resolve() != root:
        parser.error("root 必须是已存在、规范化且无链接别名的绝对目录")
    proxy = Proxy(args.codex, root)
    try:
        if args.probe:
            proxy.connect()
            print(json.dumps({"status": "connected", "cwd": str(root),
                              "assurance": "handshake_only_no_creation_or_takeover"}))
        else:
            serve(Sessions(root, proxy), sys.stdin.buffer, sys.stdout.buffer)
    except (AdapterError, OSError):
        print(json.dumps({"error": "existing_daemon_unavailable"}), file=sys.stderr)
        return 1
    finally:
        proxy.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
