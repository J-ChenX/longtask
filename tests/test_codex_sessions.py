from __future__ import annotations

import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
SPEC = importlib.util.spec_from_file_location("codex_sessions", ROOT / "scripts/codex_sessions.py")
SESSION = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SESSION)
IDENTITY = "11111111-1111-4111-8111-111111111111"
SOURCE_ID = "22222222-2222-4222-8222-222222222222"

# 仅合成原生协议夹具；不访问真实 daemon、用户对话、网络或模型。
FIXTURE = '''#!{python}
import json, os, sys
from pathlib import Path
assert sys.argv[1:] == ['app-server', 'proxy']
root=Path.cwd()
mode=(root/'mode').read_text() if (root/'mode').exists() else ''
identity='11111111-1111-4111-8111-111111111111'
source_id='22222222-2222-4222-8222-222222222222'
thread_cwd=str(root)
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
 if method == 'turn/steer' and mode=='active-lost-steer': sys.exit(0)
 if mode=='no-daemon': sys.exit(0)
 if method=='initialize': result={{'serverInfo':{{'version':'fixture'}}}}
 elif method=='thread/start':
  thread_cwd=m['params']['cwd']
  permissions={{'approvalPolicy':'on-request','approvalsReviewer':'user',
   'sandbox':{{'type':'dangerFullAccess'}},'activePermissionProfile':{{'id':':danger-full-access'}}}}
  if (root/'permissions').exists():permissions=json.loads((root/'permissions').read_text())
  result={{'thread':{{'id':identity,'cwd':thread_cwd}},**permissions}}
 elif method=='thread/name/set': result={{}}
 elif method=='turn/start': result={{'turn':{{'id':'turn-fixture','status':'inProgress'}}}}
 elif method=='turn/steer': result={{'turnId':'wrong-turn' if mode=='active-wrong-steer' else 'turn-fixture'}}
 elif method=='thread/read':
  read_cwd=str(root) if m['params']['threadId']==source_id else thread_cwd
  result={{'thread':{{'id':m['params']['threadId'],'cwd':'/outside' if mode=='outside' else read_cwd,
   'path':str(root/'synthetic-source.jsonl'),
   'status':{{'type':'active' if mode.startswith('active') else 'idle'}},
   'turns':[{{'id':'turn-fixture','status':'inProgress' if mode.startswith('active') or mode=='inconsistent' else 'completed',
   'items':[{{'type':'userMessage','text':'PRIVATE USER'}},
    {{'type':'commandExecution','text':'PRIVATE TOOL'}},
    {{'type':'agentMessage','text':'baseline checked; implementing next slice'}}]}}]
    if m['params']['includeTurns'] else []}}}}
 else: result={{}}
 response={{'id':m['id'],'result':result}}
 if method=='turn/steer' and mode=='active-rejected-steer':
  response={{'id':m['id'],'error':{{'code':-1,'message':'synthetic stale turn'}}}}
 data=json.dumps(response).encode()
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

    def bound_sessions(self, **kwargs):
        return SESSION.Sessions(self.root, self.proxy, permission_source_thread=SOURCE_ID, **kwargs)

    def permission_snapshot(self):
        return {"start": {"approvalPolicy": "on-request", "approvalsReviewer": "user",
                          "permissions": ":danger-full-access"},
                "expected": {"approvalPolicy": "on-request", "approvalsReviewer": "user",
                             "sandbox": {"type": "dangerFullAccess"},
                             "activePermissionProfile": {"id": ":danger-full-access"}},
                "sourceThreadId": SOURCE_ID, "observation": "host_recorded_snapshot"}

    def git(self, *args, cwd=None):
        if shutil.which("git") is None:
            self.skipTest("Git worktree contract")
        return subprocess.run(["git", "-C", str(cwd or self.root), *args],
                              check=True, capture_output=True, text=True, timeout=5)

    def worktree(self):
        self.git("init", "-q")
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                 "commit", "-q", "--allow-empty", "-m", "fixture")
        target = self.root / "linked worktree"
        self.git("worktree", "add", "-q", "--detach", str(target), "HEAD")
        return target

    def assert_workspace_rejected_before_rpc(self, sessions, workspace):
        for name, args in [
            ("create_thread", {"prompt": "fixture", "title": "fixture"}),
            ("read_thread", {"threadId": IDENTITY}),
            ("wait_threads", {"threadIds": [IDENTITY], "timeoutMs": 0}),
            ("send_message_to_thread", {"threadId": IDENTITY, "message": "fixture"}),
        ]:
            with self.subTest(name=name, workspace=workspace), self.assertRaises(SESSION.AdapterError):
                sessions.call(name, {**args, "workspace": workspace})
        self.assertEqual(self.requests(), [])
        self.assertIsNone(self.proxy.process)

    def test_creation_dispatch_snapshot_and_proxy_cleanup(self):
        result = self.sessions.call("create_thread", {"title": "续接夹具", "prompt": "原用户授权见原聊天；核对当前合同继续"})
        self.assertEqual(result["status"], "dispatched")
        self.assertEqual(result["threadId"], IDENTITY)
        self.assertFalse(result["takeoverConfirmed"])
        self.assertEqual(result["permissionSync"], "host_default_unverified")
        self.assertIsNone(result["permissionSourceThreadId"])
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
            ("create_thread", {"prompt": "fixture", "title": "fixture", "sourceThreadId": SOURCE_ID}),
            ("create_thread", {"prompt": "fixture", "title": "fixture", "permissions": ":danger-full-access"}),
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

    def test_registered_worktree_requires_opt_in_for_all_tools(self):
        target = self.worktree()
        self.assert_workspace_rejected_before_rpc(self.sessions, str(target))

    def test_opted_in_worktree_dispatch_read_wait_and_root_binding(self):
        target = self.worktree()
        sessions = SESSION.Sessions(self.root, self.proxy, allow_worktrees=True)
        result = sessions.call("create_thread", {"prompt": "fixture", "title": "fixture",
                                                  "workspace": str(target)})
        self.assertEqual(result["status"], "dispatched")
        self.assertEqual(result["cwd"], str(target))
        started = next(row for row in self.requests() if row.get("method") == "thread/start")
        self.assertEqual(started["params"]["cwd"], str(target))
        snapshot = sessions.call("read_thread", {"threadId": IDENTITY, "workspace": str(target)})
        self.assertEqual(snapshot["cwd"], str(target))
        waited = sessions.call("wait_threads", {"threadIds": [IDENTITY], "timeoutMs": 0,
                                                "workspace": str(target)})
        self.assertEqual(waited["threads"][0]["cwd"], str(target))
        sent = sessions.call("send_message_to_thread", {"threadId": IDENTITY, "message": "fixture",
                                                       "workspace": str(target)})
        self.assertEqual(sent["cwd"], str(target))
        self.assertFalse(sent["receivedByModel"])
        for name, args in [("read_thread", {"threadId": IDENTITY}),
                           ("wait_threads", {"threadIds": [IDENTITY], "timeoutMs": 0}),
                           ("send_message_to_thread", {"threadId": IDENTITY, "message": "fixture"})]:
            before = len(self.requests())
            with self.subTest(name=name), self.assertRaisesRegex(SESSION.AdapterError, "outside_configured_workspace"):
                sessions.call(name, args)
            self.assertEqual(len(self.requests()) - before, 1)
            self.assertFalse(self.requests()[-1]["params"]["includeTurns"])

    def test_explicit_root_is_supported_without_worktree_opt_in(self):
        result = self.sessions.call("create_thread", {"prompt": "fixture", "title": "fixture",
                                                      "workspace": str(self.root)})
        self.assertEqual(result["cwd"], str(self.root))
        self.assertEqual(self.sessions.call("read_thread", {"threadId": IDENTITY,
                                                            "workspace": str(self.root)})["cwd"], str(self.root))

    def test_worktree_selection_rejects_unregistered_foreign_and_alias_paths(self):
        target = self.worktree()
        sessions = SESSION.Sessions(self.root, self.proxy, allow_worktrees=True)
        unregistered = self.root / "unregistered"
        unregistered.mkdir()
        foreign = self.root / "foreign"
        foreign.mkdir()
        self.git("init", "-q", cwd=foreign)
        alias = self.root / "linked-alias"
        alias.symlink_to(target, target_is_directory=True)
        for workspace in [str(unregistered), str(foreign), "linked worktree", str(alias),
                          str(self.root / "missing"), "", None, 1]:
            self.assert_workspace_rejected_before_rpc(sessions, workspace)

    def test_stale_registration_does_not_allow_replaced_worktree_directory(self):
        target = self.worktree()
        (target / ".git").unlink()
        sessions = SESSION.Sessions(self.root, self.proxy, allow_worktrees=True)
        self.assert_workspace_rejected_before_rpc(sessions, str(target))
        self.git("init", "-q", cwd=target)
        self.assert_workspace_rejected_before_rpc(sessions, str(target))

    def test_unknown_creation_disables_retry_across_worktrees_and_default_root(self):
        target = self.worktree()
        sessions = SESSION.Sessions(self.root, self.proxy, allow_worktrees=True)
        (self.root / "mode").write_text("lost-turn")
        result = sessions.call("create_thread", {"prompt": "fixture", "title": "fixture",
                                                 "workspace": str(target)})
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["cwd"], str(target))
        before = len(self.requests())
        for workspace in (str(target), str(self.root), None):
            args = {"prompt": "fixture", "title": "fixture"}
            if workspace is not None:
                args["workspace"] = workspace
            with self.subTest(workspace=workspace), self.assertRaisesRegex(SESSION.AdapterError, "do_not_retry"):
                sessions.call("create_thread", args)
        self.assertEqual(len(self.requests()), before)

    def test_bound_creation_uses_verified_permissions_before_naming_or_dispatch(self):
        sessions = self.bound_sessions()
        snapshot = self.permission_snapshot()
        with mock.patch.object(SESSION, "read_source_snapshot", return_value=snapshot) as reader:
            result = sessions.call("create_thread", {"prompt": "bounded continuation", "title": "fixture"})
        self.assertEqual(result["status"], "dispatched")
        self.assertEqual(result["permissionSync"], "verified_host_snapshot")
        self.assertEqual(result["permissionSourceThreadId"], SOURCE_ID)
        messages = self.requests()
        self.assertEqual([m.get("method") for m in messages], ["initialize", "initialized", "thread/read",
                         "thread/start", "thread/read", "thread/name/set", "turn/start"])
        self.assertEqual(messages[3]["params"], {"cwd": str(self.root), "ephemeral": False,
                                                **snapshot["start"]})
        for row in (messages[2], messages[4]):
            self.assertEqual(row["params"], {"threadId": SOURCE_ID, "includeTurns": False})
        self.assertEqual(reader.call_count, 2)
        reader.assert_called_with(str(self.root / "synthetic-source.jsonl"), SOURCE_ID, self.root,
                                  include_user_messages=False)

    def test_bound_creation_reads_current_policy_for_each_new_thread(self):
        sessions = self.bound_sessions()
        first = self.permission_snapshot()
        second = self.permission_snapshot()
        second["start"]["approvalPolicy"] = second["expected"]["approvalPolicy"] = "never"
        with mock.patch.object(SESSION, "read_source_snapshot", side_effect=[first, first, second, second]):
            self.assertEqual(sessions.create("first", "first")["status"], "dispatched")
            (self.root / "permissions").write_text(json.dumps(second["expected"]))
            self.assertEqual(sessions.create("second", "second")["status"], "dispatched")
        starts = [row["params"] for row in self.requests() if row.get("method") == "thread/start"]
        self.assertEqual([row["approvalPolicy"] for row in starts], ["on-request", "never"])

    def test_missing_or_different_created_permissions_never_dispatch_and_block_global_retry(self):
        snapshot = self.permission_snapshot()
        variants = []
        for field in snapshot["expected"]:
            missing = json.loads(json.dumps(snapshot["expected"]))
            del missing[field]
            variants.append(missing)
        for field, value in [("approvalPolicy", "never"), ("approvalsReviewer", "auto_review"),
                             ("sandbox", {"type": "workspaceWrite"}),
                             ("activePermissionProfile", {"id": ":other"}),
                             ("activePermissionProfile", {"id": ":danger-full-access", "extends": "base"})]:
            different = {**snapshot["expected"], field: value}
            variants.append(different)
        for permissions in variants:
            with self.subTest(permissions=permissions):
                sessions = self.bound_sessions()
                (self.root / "permissions").write_text(json.dumps(permissions))
                before = len(self.requests())
                with mock.patch.object(SESSION, "read_source_snapshot", return_value=snapshot):
                    result = sessions.create("must not dispatch", "fixture")
                self.assertEqual(result["status"], "unknown")
                self.assertEqual(result["threadId"], IDENTITY)
                self.assertEqual(result["stage"], "permissions/verify")
                self.assertFalse(result["retrySafe"])
                mutations = [row["method"] for row in self.requests()[before:]
                             if row.get("method") in {"thread/start", "thread/name/set", "turn/start"}]
                self.assertEqual(mutations, ["thread/start"])
                before_retry = len(self.requests())
                with self.assertRaisesRegex(SESSION.AdapterError, "do_not_retry"):
                    sessions.create("different prompt", "different title", workspace=str(self.root))
                with self.assertRaisesRegex(SESSION.AdapterError, "do_not_retry"):
                    sessions.send(IDENTITY, "must not bypass permission verification")
                self.assertEqual(len(self.requests()), before_retry)

    def test_unknown_initial_turn_cannot_be_replayed_through_message_tool(self):
        original_rpc = self.proxy.rpc

        def missing_turn_identity(method, params, *args, **kwargs):
            if method == "turn/start":
                return {"turn": {}}
            return original_rpc(method, params, *args, **kwargs)

        with mock.patch.object(self.proxy, "rpc", side_effect=missing_turn_identity):
            result = self.sessions.create("possibly already consumed", "fixture")
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["threadId"], IDENTITY)
        before = len(self.requests())
        with self.assertRaisesRegex(SESSION.AdapterError, "do_not_retry"):
            self.sessions.call("send_message_to_thread", {"threadId": IDENTITY,
                                                         "message": "possibly already consumed"})
        self.assertEqual(len(self.requests()), before)
        self.assertEqual(self.sessions.read(IDENTITY)["threadId"], IDENTITY)

    def test_source_change_or_loss_after_creation_retains_identity_without_dispatch(self):
        for second in (SESSION.SourceError("permission_source_unavailable"),
                       {**self.permission_snapshot(), "start": {"approvalPolicy": "never"}},
                       {**self.permission_snapshot(), "expected": {"approvalPolicy": "never"}}):
            with self.subTest(second=second):
                sessions = self.bound_sessions()
                before = len(self.requests())
                with mock.patch.object(SESSION, "read_source_snapshot",
                                       side_effect=[self.permission_snapshot(), second]):
                    result = sessions.create("must not dispatch", "fixture")
                self.assertEqual(result["status"], "unknown")
                self.assertEqual(result["threadId"], IDENTITY)
                self.assertEqual(result["stage"], "permissions/verify")
                self.assertFalse(result["retrySafe"])
                self.assertFalse(any(row.get("method") in {"thread/name/set", "turn/start"}
                                     for row in self.requests()[before:]))
                with self.assertRaisesRegex(SESSION.AdapterError, "do_not_retry"):
                    sessions.create("fixture", "fixture")

    def test_source_metadata_must_match_bound_identity_and_root_before_log_access(self):
        sessions = self.bound_sessions()
        valid = {"id": SOURCE_ID, "cwd": str(self.root), "path": "/synthetic-only"}
        for source in (None, {**valid, "id": IDENTITY}, {**valid, "cwd": "/outside"},
                       {key: value for key, value in valid.items() if key != "path"}):
            with self.subTest(source=source), mock.patch.object(self.proxy, "connect"), \
                    mock.patch.object(self.proxy, "rpc", return_value={"thread": source}) as rpc, \
                    mock.patch.object(SESSION, "read_source_snapshot") as reader:
                with self.assertRaisesRegex(SESSION.AdapterError, "permission_source_identity_unavailable"):
                    sessions.create("fixture", "fixture")
                reader.assert_not_called()
                rpc.assert_called_once_with("thread/read", {"threadId": SOURCE_ID, "includeTurns": False})

    def test_unavailable_bound_source_fails_before_any_creation(self):
        sessions = self.bound_sessions()
        with mock.patch.object(SESSION, "read_source_snapshot",
                               side_effect=SESSION.SourceError("permission_source_policy_unsupported")):
            with self.assertRaisesRegex(SESSION.AdapterError, "permission_source_policy_unsupported"):
                sessions.create("fixture", "fixture")
        self.assertFalse(any(row.get("method") == "thread/start" for row in self.requests()))
        with self.assertRaisesRegex(SESSION.AdapterError, "invalid_thread_id"):
            SESSION.Sessions(self.root, self.proxy, permission_source_thread="not-a-uuid")

    def test_authorization_is_bound_no_argument_read_and_excludes_permission_or_other_history(self):
        sessions = self.bound_sessions()
        source = {**self.permission_snapshot(), "userMessages": ["[truncated]原始用户授权"],
                  "historyTruncated": True, "assistantText": "PRIVATE ASSISTANT",
                  "toolText": "PRIVATE TOOL"}
        with mock.patch.object(SESSION, "read_source_snapshot", return_value=source) as reader:
            result = sessions.call("read_authorization", {})
        self.assertEqual(result, {"sourceThreadId": SOURCE_ID, "observation": "host_recorded_snapshot",
                                  "userMessages": source["userMessages"], "historyTruncated": True})
        reader.assert_called_once_with(str(self.root / "synthetic-source.jsonl"), SOURCE_ID, self.root,
                                       include_user_messages=True)
        before = len(self.requests())
        for args in ({"threadId": IDENTITY}, {"sourceThreadId": IDENTITY}, {"workspace": str(self.root)},
                     {"path": "/outside"}):
            with self.subTest(args=args), self.assertRaises(SESSION.AdapterError):
                sessions.call("read_authorization", args)
        with self.assertRaisesRegex(SESSION.AdapterError, "permission_source_not_configured"):
            self.sessions.call("read_authorization", {})
        self.assertEqual(len(self.requests()), before)

    def test_direct_message_steers_active_turn_and_starts_idle_turn_without_queue(self):
        for mode, method, status in (("active", "turn/steer", "steered"), ("", "turn/start", "dispatched")):
            with self.subTest(mode=mode):
                # 每个模式使用独立连接，模拟宿主当时的活动状态。
                proxy = SESSION.Proxy(str(self.codex), self.root)
                sessions = SESSION.Sessions(self.root, proxy)
                (self.root / "mode").write_text(mode)
                before = len(self.requests())
                try:
                    result = sessions.call("send_message_to_thread", {"threadId": IDENTITY, "message": "bounded update"})
                finally:
                    proxy.close()
                self.assertEqual(result["status"], status)
                self.assertFalse(result["receivedByModel"])
                self.assertEqual(result["turnId"], "turn-fixture")
                rows = self.requests()[before:]
                self.assertFalse(any("queue" in row.get("method", "") for row in rows))
                deliveries = [row for row in rows if row.get("method", "").startswith("turn/")]
                self.assertEqual(len(deliveries), 1)
                self.assertEqual(deliveries[0]["method"], method)
                expected = {"threadId": IDENTITY, "input": [{"type": "text", "text": "bounded update"}]}
                if mode:
                    expected["expectedTurnId"] = "turn-fixture"
                self.assertEqual(deliveries[0]["params"], expected)

    def test_unconfirmed_message_is_never_retried_or_delivered_by_fallback(self):
        for mode, method in (("active-lost-steer", "turn/steer"), ("active-rejected-steer", "turn/steer"),
                             ("active-wrong-steer", "turn/steer"), ("lost-turn", "turn/start")):
            with self.subTest(mode=mode):
                proxy = SESSION.Proxy(str(self.codex), self.root)
                sessions = SESSION.Sessions(self.root, proxy)
                (self.root / "mode").write_text(mode)
                before = len(self.requests())
                try:
                    result = sessions.call("send_message_to_thread", {"threadId": IDENTITY, "message": "once"})
                    self.assertEqual(result["status"], "unknown")
                    self.assertEqual(result["stage"], method)
                    self.assertFalse(result["retrySafe"])
                    self.assertFalse(result["receivedByModel"])
                    after = len(self.requests())
                    with self.assertRaisesRegex(SESSION.AdapterError, "do_not_retry"):
                        sessions.call("send_message_to_thread", {"threadId": IDENTITY, "message": "different text"})
                    self.assertEqual(len(self.requests()), after)
                finally:
                    proxy.close()
                deliveries = [row["method"] for row in self.requests()[before:]
                              if row.get("method", "").startswith("turn/")]
                self.assertEqual(deliveries, [method])
                self.assertFalse(any("queue" in row.get("method", "") for row in self.requests()[before:]))

    def test_message_rejects_invalid_input_workspace_and_inconsistent_state_before_delivery(self):
        for args in ({"threadId": IDENTITY, "message": " "}, {"threadId": IDENTITY, "message": "a" * 6001},
                     {"threadId": "../other", "message": "fixture"},
                     {"threadId": IDENTITY, "message": "fixture", "queue": True},
                     {"threadId": IDENTITY, "message": "fixture", "workspace": "/outside"}):
            with self.subTest(args=args), self.assertRaises(SESSION.AdapterError):
                self.sessions.call("send_message_to_thread", args)
        self.assertEqual(self.requests(), [])
        (self.root / "mode").write_text("inconsistent")
        with self.assertRaisesRegex(SESSION.AdapterError, "thread_delivery_state_unknown"):
            self.sessions.call("send_message_to_thread", {"threadId": IDENTITY, "message": "fixture"})
        self.assertFalse(any(row.get("method", "").startswith("turn/") for row in self.requests()))

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
        with mock.patch.object(self.proxy, "connect"), mock.patch.object(self.sessions, "_read", return_value=row) as read, mock.patch.object(SESSION.time, "monotonic", side_effect=[0, 0, 0, 0, 0, .01]):
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
        self.assertEqual([t["name"] for t in rows[2]["result"]["tools"]],
                         ["create_thread", "read_thread", "wait_threads", "send_message_to_thread", "read_authorization"])
        for tool in rows[2]["result"]["tools"]:
            self.assertFalse(tool["inputSchema"]["additionalProperties"])
            if tool["name"] == "read_authorization":
                self.assertEqual(tool["inputSchema"]["properties"], {})
                self.assertTrue(tool["annotations"]["readOnlyHint"])
                continue
            self.assertIn("workspace", tool["inputSchema"]["properties"])
            self.assertNotIn("workspace", tool["inputSchema"]["required"])
            if tool["name"] == "create_thread":
                self.assertEqual(set(tool["inputSchema"]["properties"]), {"prompt", "title", "workspace"})
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

    def test_cli_worktree_opt_in_controls_workspace_selection(self):
        target = self.worktree()
        command = [sys.executable, "-B", str(ROOT / "scripts/codex_sessions.py"),
                   "--root", str(self.root), "--codex", str(self.codex)]
        messages = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2025-06-18"}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
             "params": {"name": "create_thread", "arguments": {
                 "prompt": "fixture", "title": "fixture", "workspace": str(target)}}},
        ]
        payload = "\n".join(json.dumps(row) for row in messages) + "\n"
        denied = subprocess.run(command, input=payload, capture_output=True, text=True, timeout=5)
        self.assertEqual(denied.returncode, 0, denied.stderr)
        denied_result = json.loads(denied.stdout.splitlines()[-1])["result"]
        self.assertTrue(denied_result["isError"])
        self.assertEqual(self.requests(), [])
        allowed = subprocess.run([*command, "--allow-worktrees"], input=payload,
                                 capture_output=True, text=True, timeout=5)
        self.assertEqual(allowed.returncode, 0, allowed.stderr)
        allowed_result = json.loads(allowed.stdout.splitlines()[-1])["result"]
        self.assertFalse(allowed_result["isError"])
        self.assertEqual(json.loads(allowed_result["content"][0]["text"])["cwd"], str(target))

    def test_cli_permission_source_is_validated_and_bound_to_authorization_reader(self):
        command = [sys.executable, "-B", str(ROOT / "scripts/codex_sessions.py"),
                   "--root", str(self.root), "--codex", str(self.codex), "--permission-source-thread"]
        invalid = subprocess.run([*command, "not-a-uuid", "--probe"], capture_output=True, text=True, timeout=5)
        self.assertNotEqual(invalid.returncode, 0)
        self.assertEqual(self.requests(), [])
        messages = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
             "params": {"name": "read_authorization", "arguments": {}}},
        ]
        # 合成宿主路径不在任何真实会话目录；读取由helper明确拒绝。
        result = subprocess.run([*command, SOURCE_ID], input="\n".join(json.dumps(row) for row in messages) + "\n",
                                capture_output=True, text=True, timeout=5,
                                env={**os.environ, "CODEX_HOME": str(self.root / "isolated-codex-home")})
        self.assertEqual(result.returncode, 0, result.stderr)
        reply = json.loads(result.stdout.splitlines()[-1])["result"]
        self.assertTrue(reply["isError"])
        self.assertEqual(reply["content"][0]["text"], "permission_source_unavailable")
        reads = [row for row in self.requests() if row.get("method") == "thread/read"]
        self.assertEqual([row["params"] for row in reads], [{"threadId": SOURCE_ID, "includeTurns": False}])
        self.assertFalse(any(row.get("method", "").startswith("turn/") for row in self.requests()))


if __name__ == "__main__":
    unittest.main()
