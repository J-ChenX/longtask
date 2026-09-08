"""Fake backend protocol tests only. These results are NOT host execution evidence."""
import hashlib
import json
import os
import signal
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

COLLECTOR = Path(__file__).resolve().parents[1] / 'scripts' / 'collect_host_trace.py'
DIGEST = 'a' * 64
FAKE = r'''#!/usr/bin/env python3
import json,sys,time,subprocess,signal,os
from pathlib import Path
if '--version' in sys.argv:
 print('fake-codex-for-protocol-tests'); raise SystemExit(0)
for arg in sys.argv:
 if arg.startswith('test_child='):
  child=subprocess.Popen([sys.executable,'-c','import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(60)'])
  Path(arg.split('=',1)[1]).write_text(str(child.pid))
if 'test_hang=true' in sys.argv:
 time.sleep(30); raise SystemExit(0)
def emit(msg): print(json.dumps(msg),flush=True)
def notify(method,params): emit({'method':method,'params':params})
thread_id='synthetic-thread-001';session_id='synthetic-session-001'
for line in sys.stdin:
 m=json.loads(line);method=m.get('method');p=m.get('params',{});rid=m.get('id')
 if method is None:
  if rid=='approval-001':
   if m.get('result')!={'decision':'decline'}: raise SystemExit(3)
   notify('turn/completed',{'threadId':thread_id,'turn':{'id':'turn-001','status':'completed'}})
  continue
 if method=='initialized': continue
 if method=='initialize': result={'userAgent':'fake','codexHome':'synthetic','platformFamily':'unix','platformOs':'linux'}
 elif method in ('thread/start','thread/resume'):
  thread_id=p.get('threadId',thread_id)
  if p['sandbox'] not in ('read-only','workspace-write'): raise SystemExit(4)
  result={'thread':{'id':thread_id,'sessionId':session_id},'model':'synthetic-model'}
 elif method=='turn/start':
  if 'test_override=true' not in sys.argv: raise SystemExit(5)
  notify('thread/tokenUsage/updated',{'threadId':thread_id,'turnId':'turn-001','tokenUsage':{'last':{'inputTokens':4,'outputTokens':2,'cachedInputTokens':0},'total':{'inputTokens':4,'outputTokens':2,'cachedInputTokens':0}}})
  emit({'id':'approval-001','method':'item/commandExecution/requestApproval','params':{'threadId':thread_id,'turnId':'turn-001','itemId':'tool-001'}})
  result={'turn':{'id':'turn-001'}}
 elif method=='thread/compact/start':
  notify('item/completed',{'threadId':thread_id,'turnId':'compact-turn-001','item':{'type':'contextCompaction','id':'compact-item-001'}})
  notify('turn/completed',{'threadId':thread_id,'turn':{'id':'compact-turn-001','status':'completed'}})
  result={}
 elif method=='thread/read': result={'thread':{'id':thread_id,'sessionId':session_id}}
 else: raise SystemExit(6)
 emit({'id':rid,'result':result})
'''


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='host-collector-fake-tests-')
        self.root = Path(self.tmp.name)
        backend = self.root / 'codex'
        backend.write_text(FAKE)
        backend.chmod(0o700)
        self.env = dict(os.environ, PATH=str(self.root) + os.pathsep + os.environ['PATH'])
        self.prompt = self.root / 'synthetic.txt'
        self.prompt.write_text('Synthetic fixture. No real user content.')
        self.seq = 0

    def tearDown(self):
        self.tmp.cleanup()

    def invoke(self, extra=(), *, output=None, timeout=10):
        self.seq += 1
        output = output or self.root / ('run-' + str(self.seq))
        cmd = [sys.executable, str(COLLECTOR), '--out', str(output),
               '--cwd', str(self.root), '--retention-until', '2099-01-01T00:00:00Z',
               '--evaluated-archive-sha256', DIGEST, '--synthetic-only', *extra]
        result = subprocess.run(cmd, env=self.env, capture_output=True, text=True, timeout=timeout)
        return result, output

    def test_start_turn_decline_compact_trace_binding(self):
        result, out = self.invoke(['--prompt-file', str(self.prompt), '--compact',
                                  '--approval-policy', 'on-request',
                                  '--config', 'test_override=true', '--config', 'plugins.synthetic.enabled=false'])
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        summary = json.loads((out / 'summary.json').read_text())
        self.assertEqual(summary['evaluated_archive_sha256'], DIGEST)
        self.assertEqual(summary['assurance']['release_gate'], 'not_evaluated')
        self.assertEqual(summary['automated_declines'], 1)
        self.assertIsNone(summary['human_interventions'])
        self.assertFalse(summary['grader_calibrated'])
        self.assertEqual(summary['configuration']['explicit_override_count'], 2)
        self.assertEqual(summary['session_id'], 'synthetic-session-001')
        self.assertEqual(summary['compaction_events'][0]['itemId'], 'compact-item-001')
        self.assertEqual(summary['usage_updates'][0]['tokenUsage']['total']['inputTokens'], 4)
        trace = (out / 'trace.jsonl').read_bytes()
        self.assertEqual(hashlib.sha256(trace).hexdigest(), summary['trace_sha256'])
        frames = [json.loads(line) for line in trace.splitlines()]
        self.assertEqual(frames[0]['message']['run_id'], summary['collector_run_id'])
        self.assertEqual(frames[0]['message']['evaluated_archive_sha256'], DIGEST)
        self.assertNotIn(b'plugins.synthetic.enabled=false', trace)
        self.assertNotIn('plugins.synthetic.enabled=false', result.stdout)
        self.assertEqual(out.stat().st_mode & 0o777, 0o700)
        for name in ('trace.jsonl','stderr.txt','summary.json'):
            self.assertEqual((out / name).stat().st_mode & 0o777, 0o600)

    def test_resume_routes_actual_thread_identifier(self):
        result, out = self.invoke(['--thread-id','synthetic-resume-002'])
        self.assertEqual(result.returncode, 0, result.stdout)
        frames = [json.loads(line)['message'] for line in (out/'trace.jsonl').read_text().splitlines()]
        resume = [m for m in frames if m.get('method') == 'thread/resume']
        self.assertEqual(resume[0]['params']['threadId'], 'synthetic-resume-002')
        self.assertFalse(any(m.get('method') == 'thread/start' for m in frames))

    def test_timeout_preserves_partial_trace_and_fails(self):
        before = time.monotonic()
        result, out = self.invoke(['--probe','--timeout','0.05','--config','test_hang=true'])
        self.assertEqual(result.returncode, 1)
        summary = json.loads((out/'summary.json').read_text())
        self.assertEqual(summary['failure_class'], 'TimeoutError')
        self.assertEqual(summary['status'], 'unable_to_verify')
        self.assertLess(time.monotonic()-before, 8)
        self.assertGreater((out/'trace.jsonl').stat().st_size, 0)
        self.assertEqual(hashlib.sha256((out/'trace.jsonl').read_bytes()).hexdigest(), summary['trace_sha256'])

    def assert_not_running(self, pid):
        # A killed orphan can remain briefly as an init-owned zombie.
        stat_path = Path('/proc') / str(pid) / 'stat'
        for _ in range(50):
            if not stat_path.exists() or stat_path.read_text().split(') ', 1)[1].startswith('Z'):
                return
            time.sleep(0.02)
        self.fail('Owned descendant survived collector cleanup')

    def test_exited_leader_cannot_leave_term_ignoring_descendant(self):
        pidfile = self.root / 'child-pid'
        result, out = self.invoke(['--probe', '--config', 'test_child=' + str(pidfile)], timeout=15)
        pid = int(pidfile.read_text())
        try:
            self.assert_not_running(pid)
            summary = json.loads((out / 'summary.json').read_text())
            self.assertEqual(hashlib.sha256((out / 'trace.jsonl').read_bytes()).hexdigest(), summary['trace_sha256'])
        finally:
            try: os.kill(pid, signal.SIGKILL)
            except ProcessLookupError: pass

    def test_cancellation_cleans_group_and_preserves_nonpassing_trace(self):
        for sig in (signal.SIGTERM, signal.SIGINT):
            with self.subTest(signal=sig):
                pidfile = self.root / ('child-' + str(sig))
                out = self.root / ('cancel-' + str(sig))
                cmd = [sys.executable, str(COLLECTOR), '--probe', '--out', str(out),
                       '--retention-until', '2099-01-01T00:00:00Z',
                       '--config', 'test_child=' + str(pidfile), '--config', 'test_hang=true']
                process = subprocess.Popen(cmd, env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                pid = None
                try:
                    deadline = time.monotonic() + 5
                    while not pidfile.exists() and time.monotonic() < deadline:
                        time.sleep(0.02)
                    self.assertTrue(pidfile.exists())
                    pid = int(pidfile.read_text())
                    process.send_signal(sig)
                    stdout, stderr = process.communicate(timeout=12)
                    self.assertEqual(process.returncode, 1, stdout + stderr)
                    self.assert_not_running(pid)
                    summary = json.loads((out / 'summary.json').read_text())
                    self.assertEqual(summary['failure_class'], 'CollectionCancelled')
                    self.assertEqual(summary['status'], 'unable_to_verify')
                    self.assertEqual(hashlib.sha256((out / 'trace.jsonl').read_bytes()).hexdigest(), summary['trace_sha256'])
                finally:
                    if process.poll() is None: process.kill(); process.communicate()
                    if pid:
                        try: os.kill(pid, signal.SIGKILL)
                        except ProcessLookupError: pass

    def test_rejects_nonfinite_timeout_and_bad_bindings_before_output(self):
        for extra in (['--timeout','nan'], ['--timeout','inf'], ['--timeout','0'],
                      ['--evaluated-archive-sha256','not-a-digest'],
                      ['--retention-until','2099-01-01T00:00:00'],
                      ['--retention-until','2000-01-01T00:00:00Z']):
            with self.subTest(extra=extra):
                result, out = self.invoke(extra)
                self.assertEqual(result.returncode, 2)
                self.assertFalse(out.exists())

    def test_output_existing_directory_and_symlink_ancestors_rejected(self):
        existing = self.root/'existing'; existing.mkdir()
        result, _ = self.invoke(output=existing)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(list(existing.iterdir()), [])
        link = self.root/'alias'; link.symlink_to(existing, target_is_directory=True)
        result, _ = self.invoke(output=link/'new')
        self.assertEqual(result.returncode, 1)
        self.assertFalse((existing/'new').exists())

    def test_nonprobe_requires_archive_and_synthetic_attestation(self):
        base = [sys.executable, str(COLLECTOR), '--out', str(self.root/'missing'),
                '--retention-until', '2099-01-01T00:00:00Z']
        for extra in ([], ['--synthetic-only'], ['--evaluated-archive-sha256', DIGEST]):
            with self.subTest(extra=extra):
                result = subprocess.run(base + extra, env=self.env, capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 2)
                self.assertFalse((self.root/'missing').exists())

    def test_probe_may_omit_archive_but_not_retention(self):
        cmd=[sys.executable,str(COLLECTOR),'--probe','--out',str(self.root/'probe'),
             '--retention-until','2099-01-01T00:00:00+01:00']
        result=subprocess.run(cmd,env=self.env,capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        summary=json.loads(result.stdout)
        self.assertIsNone(summary['evaluated_archive_sha256'])
        self.assertEqual(summary['retention_until'],'2098-12-31T23:00:00Z')
        result=subprocess.run(cmd[:-2],env=self.env,capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,2)


if __name__ == '__main__':
    unittest.main()
