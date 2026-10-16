from __future__ import annotations

import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("codex_sessions", ROOT / "scripts/codex_sessions.py")
SESSION = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SESSION)
IDENTITY = "11111111-1111-4111-8111-111111111111"

# 仅合成原生协议夹具；不访问真实 daemon、用户对话、网络或模型。
FIXTURE = '''#!{python}
import json, os, sys
from pathlib import Path
assert sys.argv[1:] == ['app-server', 'proxy']
root=Path.cwd()
mode=(root/'mode').read_text() if (root/'mode').exists() else ''
identity='11111111-1111-4111-8111-111111111111'
import base64,hashlib,struct
source=sys.stdin.buffer
header=b''
while not header.endswith(b'\\r\\n\\r\\n'):
 b=source.read(1)
 if not b:sys.exit(0)
 header+=b
key=[l.split(b':',1)[1].strip() for l in header.split(b'\\r\\n') if l.lower().startswith(b'sec-websocket-key:')][0]
accept=base64.b64encode(hashlib.sha1(key+b'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').digest())
sys.stdout.buffer.write(b'HTTP/1.1 101 Switching Protocols\\r\\nUpgrade: websocket\\r\\nConnection: Upgrade\\r\\nSec-WebSocket-Accept: '+accept+b'\\r\\n\\r\\n');sys.stdout.buffer.flush()
def frame(data,opcode=1,final=True):
 size=len(data)
 head=bytes(((128 if final else 0)|opcode,min(size,126))) if size<65536 else bytes(((128 if final else 0)|opcode,127))
 if size>=65536:head+=struct.pack('!Q',size)
 elif size>=126:head+=struct.pack('!H',size)
 sys.stdout.buffer.write(head+data);sys.stdout.buffer.flush()
while True:
 head=source.read(2)
 if not head:break
 a,b=head
 assert b&128
 size=b&127
 if size==126:size=struct.unpack('!H',source.read(2))[0]
 elif size==127:size=struct.unpack('!Q',source.read(8))[0]
 mask=source.read(4)
 data=source.read(size)
 data=bytes(value^mask[i%4] for i,value in enumerate(data))
 if a&15==10:continue
 m=json.loads(data)
 with (root/'rpc-log').open('a') as f: f.write(json.dumps(m)+'\\n')
 method=m.get('method')
 if 'id' not in m: continue
 if method == 'thread/start' and mode=='lost-create': sys.exit(0)
 if method == 'turn/start' and mode=='lost-turn': sys.exit(0)
 if mode=='no-daemon': sys.exit(0)
 if method=='initialize': result={{'serverInfo':{{'version':'fixture'}}}}
 elif method=='thread/start': result={{'thread':{{'id':identity,'cwd':str(root)}}}}
 elif method=='thread/name/set': result={{}}
 elif method=='turn/start': result={{'turn':{{'id':'turn-fixture','status':'inProgress'}}}}
 elif method=='thread/read':
  result={{'thread':{{'id':identity,'cwd':'/outside' if mode=='outside' else str(root),
   'status':{{'type':'idle'}},'turns':[{{'id':'turn-fixture','status':'completed',
   'items':[{{'type':'userMessage','text':'PRIVATE USER'}},
    {{'type':'commandExecution','text':'PRIVATE TOOL'}},
    {{'type':'agentMessage','text':'baseline checked; implementing next slice'}}]}}]
    if m['params']['includeTurns'] else []}}}}
 else: result={{}}
 data=json.dumps({{'id':m['id'],'result':result}}).encode()
 if mode=='fragmented':
  frame(b'ping',9);frame(data[:len(data)//2],1,False);frame(data[len(data)//2:],0)
 else:frame(data)
'''


class SessionsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.codex = self.root / "fixture-codex"
        self.codex.write_text(FIXTURE.format(python=sys.executable))
        self.codex.chmod(0o700)
        self.proxy = SESSION.Proxy(str(self.codex), self.root)
        self.sessions = SESSION.Sessions(self.root, self.proxy)

    def tearDown(self):
        self.proxy.close()
        self.temp.cleanup()

    def requests(self):
        path = self.root / "rpc-log"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def test_creation_dispatch_snapshot_and_proxy_cleanup(self):
        result = self.sessions.call("create_thread", {"title": "续接夹具", "prompt": "原用户授权见原聊天；核对当前合同继续"})
        self.assertEqual(result["status"], "dispatched")
        self.assertEqual(result["threadId"], IDENTITY)
        self.assertFalse(result["takeoverConfirmed"])
        messages = self.requests()
        self.assertEqual([m.get("method") for m in messages],
                         ["initialize", "initialized", "thread/start", "thread/name/set", "turn/start"])
        self.assertEqual(messages[2]["params"], {"cwd": str(self.root), "ephemeral": False})
        snapshot = self.sessions.call("read_thread", {"threadId": IDENTITY})
        self.assertEqual(snapshot["turnStatus"], "completed")
        self.assertIn("implementing", snapshot["latestAssistantText"])
        self.assertNotIn("PRIVATE", json.dumps(snapshot))
        self.proxy.close()
        self.assertIsNotNone(self.proxy.process.poll())

    def test_lost_creation_is_unknown_and_never_replayed(self):
        (self.root / "mode").write_text("lost-create")
        args = {"title": "fixture", "prompt": "bounded synthetic prompt"}
        first = self.sessions.call("create_thread", args)
        self.assertEqual(first["status"], "unknown")
        self.assertIsNone(first["threadId"])
        self.assertFalse(first["retrySafe"])
        with self.assertRaisesRegex(SESSION.AdapterError, "do_not_retry"):
            self.sessions.call("create_thread", args)
        self.assertEqual(sum(m.get("method") == "thread/start" for m in self.requests()), 1)

    def test_lost_turn_preserves_real_identity_and_does_not_resend(self):
        (self.root / "mode").write_text("lost-turn")
        result = self.sessions.create("bounded fixture", "fixture")
        self.assertEqual(result["threadId"], IDENTITY)
        self.assertEqual(result["stage"], "turn/start")
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(sum(m.get("method") == "turn/start" for m in self.requests()), 1)

    def test_websocket_fragmentation_ping_and_large_masked_requests(self):
        (self.root / "mode").write_text("fragmented")
        result = self.sessions.create("界" * 6000, "fixture")
        self.assertEqual(result["status"], "dispatched")
        self.assertEqual(len([m for m in self.requests() if m.get("method") == "turn/start"]), 1)

    def test_backpressured_write_obeys_rpc_deadline_and_cleanup_reaps_proxy(self):
        # 只用自有停读子进程；小管道确保合法的大提示词实际触发背压。
        if os.name != "posix":
            self.skipTest("POSIX pipe contract")
        import fcntl
        import selectors
        import time
        command = [sys.executable, "-c", "import time; time.sleep(30)"]
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, bufsize=0)
        self.proxy.process = process
        try:
            if hasattr(fcntl, "F_SETPIPE_SZ"):
                fcntl.fcntl(process.stdin.fileno(), fcntl.F_SETPIPE_SZ, 4096)
            os.set_blocking(process.stdin.fileno(), False)
            self.proxy.writer.register(process.stdin, selectors.EVENT_WRITE)
            self.proxy.selector.register(process.stdout, selectors.EVENT_READ)
            start = time.monotonic()
            with self.assertRaises(SESSION.AdapterError):
                self.proxy.rpc("turn/start", {"input": [{"type": "text", "text": "界" * 6000}]}, timeout=.05)
            self.assertLess(time.monotonic() - start, 1)
            self.assertTrue(self.proxy.failed)
        finally:
            self.proxy.close()
        self.assertIsNotNone(process.poll())

    def test_missing_daemon_does_not_start_a_standalone_server(self):
        (self.root / "mode").write_text("no-daemon")
        with self.assertRaisesRegex(SESSION.AdapterError, "existing_daemon_unavailable"):
            self.sessions.create("fixture", "fixture")
        self.assertFalse(any(m.get("method") == "thread/start" for m in self.requests()))

    def test_rejects_escape_extra_arguments_and_oversize_before_transport(self):
        for name, args in [
            ("create_thread", {"prompt": "a" * 6001, "title": "fixture"}),
            ("create_thread", {"prompt": "fixture", "title": "bad\nname"}),
            ("create_thread", {"prompt": "fixture", "title": "bad\x7fname"}),
            ("create_thread", {"prompt": "fixture", "title": "fixture", "cwd": "/outside"}),
            ("create_thread", {"prompt": "fixture", "title": "fixture", "approvalPolicy": "never"}),
            ("read_thread", {"threadId": "../arbitrary"}),
            ("wait_threads", {"threadIds": [IDENTITY], "timeoutMs": True}),
            ("wait_threads", {"threadIds": [IDENTITY] * 2, "timeoutMs": 0}),
        ]:
            with self.subTest(name=name, args=args), self.assertRaises(SESSION.AdapterError):
                self.sessions.call(name, args)
        self.assertEqual(self.requests(), [])

    def test_read_denies_other_workspace_before_loading_turns(self):
        (self.root / "mode").write_text("outside")
        with self.assertRaisesRegex(SESSION.AdapterError, "outside_configured_workspace"):
            self.sessions.read(IDENTITY)
        reads = [m for m in self.requests() if m.get("method") == "thread/read"]
        self.assertEqual(len(reads), 1)
        self.assertFalse(reads[0]["params"]["includeTurns"])

    def test_immediate_wait_returns_actual_snapshot(self):
        result = self.sessions.call("wait_threads", {"threadIds": [IDENTITY], "timeoutMs": 0})
        self.assertEqual(result["threads"][0]["turnStatus"], "completed")

    def test_malformed_host_payload_is_bounded_error_and_creation_stays_unknown(self):
        self.proxy.connect()
        for payload in ({"thread": None}, {"thread": {"id": IDENTITY, "cwd": str(self.root),
                        "status": {"type": "idle"}, "turns": [None]}}):
            with self.subTest(payload=payload), mock.patch.object(self.proxy, "rpc", return_value=payload):
                with self.assertRaises(SESSION.AdapterError):
                    self.sessions.read(IDENTITY)
        with mock.patch.object(self.proxy, "rpc", return_value={"thread": None}):
            result = self.sessions.create("fixture", "fixture")
        self.assertEqual(result["status"], "unknown")
        self.assertFalse(result["retrySafe"])

    def test_wait_expiry_returns_last_snapshot_without_zero_budget_rpc(self):
        row = {"threadId": IDENTITY, "turnStatus": "inProgress"}
        with mock.patch.object(self.proxy, "connect"), mock.patch.object(self.sessions, "read", return_value=row) as read, mock.patch.object(SESSION.time, "monotonic", side_effect=[0, 0, 0, 0, 0, .01]):
            result = self.sessions.call("wait_threads", {"threadIds": [IDENTITY], "timeoutMs": 1})
        self.assertEqual(result["threads"], [row])
        self.assertEqual(read.call_count, 1)

    def test_mcp_handshake_catalog_call_and_duplicate_rejection(self):
        messages = [
            {"jsonrpc": "2.0", "id": 0, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26"}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "read_thread", "arguments": {"threadId": IDENTITY}}},
        ]
        source = io.BytesIO(("\n".join(json.dumps(m) for m in messages) + '\n{"jsonrpc":"2.0","id":4,"id":5}\n').encode())
        sink = io.BytesIO()
        SESSION.serve(self.sessions, source, sink)
        rows = [json.loads(line) for line in sink.getvalue().splitlines()]
        self.assertIn("error", rows[0])
        self.assertEqual(rows[1]["result"]["protocolVersion"], "2025-03-26")
        self.assertEqual([t["name"] for t in rows[2]["result"]["tools"]], ["create_thread", "read_thread", "wait_threads"])
        self.assertFalse(rows[3]["result"]["isError"])
        self.assertIn("error", rows[4])

    def test_invalid_mcp_types_return_protocol_errors_without_crashing(self):
        rows = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": []}},
            {"jsonrpc": "2.0", "id": {}, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
            {"jsonrpc": "2.0", "id": 2, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
        ]
        sink = io.BytesIO()
        SESSION.serve(self.sessions, io.BytesIO(("\n".join(json.dumps(row) for row in rows) + "\n").encode()), sink)
        observed = [json.loads(line) for line in sink.getvalue().splitlines()]
        self.assertTrue(all("error" in row for row in observed[:2]))
        self.assertIn("result", observed[2])
        self.assertEqual(self.requests(), [])

    def test_cli_probe_has_no_creation_and_stdio_has_no_nonprotocol_output(self):
        command = [sys.executable, "-B", str(ROOT / "scripts/codex_sessions.py"),
                   "--root", str(self.root), "--codex", str(self.codex)]
        probe = subprocess.run([*command, "--probe"], capture_output=True, text=True, timeout=5)
        self.assertEqual(probe.returncode, 0, probe.stderr)
        self.assertEqual(json.loads(probe.stdout)["status"], "connected")
        self.assertFalse(any(m.get("method") == "thread/start" for m in self.requests()))
        result = subprocess.run(command, input=json.dumps({"jsonrpc": "2.0", "id": 1,
                                "method": "initialize", "params": {"protocolVersion": "2025-06-18"}}) + "\n",
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(result.stdout.splitlines()), 1)
        self.assertEqual(json.loads(result.stdout)["id"], 1)


if __name__ == "__main__":
    unittest.main()
