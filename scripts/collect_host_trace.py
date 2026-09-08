#!/usr/bin/env python3
"""Minimal real Codex stdio collector. Raw traces are private; no release-pass claims."""
import argparse
from datetime import datetime, timezone
import hashlib
import math
import re
import stat
import json
import os
from pathlib import Path
import queue
import signal
import subprocess
import threading
import time
import uuid


def open_directory(path):
    """Traverse every ancestor through dir fds; never resolve a symlink."""
    path = Path(os.path.abspath(path))
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in path.parts[1:]:
            nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = nxt
        return fd
    except BaseException:
        os.close(fd)
        raise


def private_output(path):
    # Reject lexical parent traversal before abspath can discard it.
    if '..' in Path(path).parts:
        raise ValueError('Output path must not contain parent traversal')
    output = Path(os.path.abspath(path))
    parent = open_directory(output.parent)
    try:
        os.mkdir(output.name, mode=0o700, dir_fd=parent)
        fd = os.open(output.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
        os.fchmod(fd, 0o700)
        info = os.fstat(fd)
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            os.close(fd)
            raise ValueError('Private output ownership or mode mismatch')
        return output, fd
    finally:
        os.close(parent)


def private_file(directory_fd, name):
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                 0o600, dir_fd=directory_fd)
    os.fchmod(fd, 0o600)
    return os.fdopen(fd, 'w', encoding='utf-8')


def retention_deadline(value):
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError
        if parsed <= datetime.now(timezone.utc):
            raise ValueError
    except (ValueError, TypeError, OverflowError):
        raise argparse.ArgumentTypeError('Retention must be a future absolute ISO 8601 time with timezone') from None
    return parsed.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')


def finite_timeout(value):
    try:
        parsed = float(value)
        if not math.isfinite(parsed) or parsed <= 0:
            raise ValueError
    except (ValueError, OverflowError):
        raise argparse.ArgumentTypeError('Timeout must be finite and positive') from None
    return parsed


class CollectionCancelled(Exception):
    """A host cancellation that still requires owned-process and trace cleanup."""


class Harness:
    def __init__(self, args):
        self.args = args
        self.out, self.directory_fd = private_output(args.out)
        self.trace_path = self.out / 'trace.jsonl'
        self.trace = private_file(self.directory_fd, 'trace.jsonl')
        self.err = private_file(self.directory_fd, 'stderr.txt')
        self.trace_hash = hashlib.sha256()
        self.run_id = str(uuid.uuid4())
        self.started_at = datetime.now(timezone.utc).isoformat()
        self.start = time.monotonic()
        self.deadline = self.start + args.timeout
        self.lock = threading.Lock()
        self.q = queue.Queue()
        self.counter = 0
        self.events = []
        self.requests = []
        self.thread_id = None
        self.session_id = None
        self.turn_id = None
        self.configured_model = None
        self.route_changes = []
        self.usage = []
        self.completed_turns = []
        self.compactions = []
        self.tool_ids = set()
        self.guardrail_ids = set()
        self.handoffs = []
        self.turn_status = None
        command = ['codex', 'app-server', '--stdio']
        for override in args.config:
            command.extend(['--config', override])
        self.record('collector', {'run_id': self.run_id,
                    'evaluated_archive_sha256': args.evaluated_archive_sha256,
                    'retention_until': args.retention_until,
                    'synthetic_only_attested': args.synthetic_only,
                    'probe': args.probe, 'config_override_count': len(args.config)})
        self.command = command
        self.process = None
        self.reader = None

    def start_backend(self):
        self.process = subprocess.Popen(
            self.command, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=self.err, text=True,
            encoding='utf-8', bufsize=1, start_new_session=True)
        self.reader = threading.Thread(target=self.read, daemon=True)
        self.reader.start()

    def record(self, direction, message):
        with self.lock:
            encoded = json.dumps({
                'timestamp_unix_ns': time.time_ns(),
                'elapsed_ms': round((time.monotonic() - self.start) * 1000),
                'direction': direction, 'message': message,
            }, ensure_ascii=False) + '\n'
            self.trace.write(encoded)
            self.trace.flush()
            self.trace_hash.update(encoded.encode('utf-8'))

    def read(self):
        try:
            for line in self.process.stdout:
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    self.record('server_unparsed', {'line': line.rstrip('\n')})
                    continue
                self.record('server_to_client', msg)
                self.q.put(msg)
        finally:
            self.q.put(None)

    def send(self, message):
        self.record('client_to_server', message)
        self.process.stdin.write(json.dumps(message, ensure_ascii=False) + '\n')
        self.process.stdin.flush()

    def pump(self):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('Global harness deadline exceeded')
        try:
            msg = self.q.get(timeout=remaining)
        except queue.Empty:
            raise TimeoutError('Global harness deadline exceeded') from None
        if msg is None:
            raise RuntimeError('App Server stdout closed')
        if 'method' in msg:
            method, params = msg['method'], msg.get('params') or {}
            self.events.append(msg)
            if 'id' in msg:
                # This collector never fabricates user approvals or grader decisions.
                self.requests.append({'method': method, 'id': msg['id'],
                                      'actor': 'collector', 'human': False,
                                      'response': 'decline' if method in
                                      ('item/commandExecution/requestApproval', 'item/fileChange/requestApproval')
                                      else 'unsupported'})
                if method in ('item/commandExecution/requestApproval', 'item/fileChange/requestApproval'):
                    self.send({'id': msg['id'], 'result': {'decision': 'decline'}})
                else:
                    self.send({'id': msg['id'], 'error': {
                        'code': -32601, 'message': 'Unsupported interactive request in synthetic collector'}})
            if method == 'thread/tokenUsage/updated':
                self.usage.append(params)
            elif method == 'model/rerouted':
                self.route_changes.append(params)
            elif method == 'turn/completed':
                self.completed_turns.append(params)
                self.turn_status = params.get('turn', {}).get('status')
            elif method == 'turn/started':
                self.turn_id = params.get('turn', {}).get('id')
            elif method == 'item/completed':
                item = params.get('item') or {}
                kind = item.get('type')
                if kind == 'contextCompaction':
                    self.compactions.append({'threadId': params.get('threadId'),
                                             'turnId': params.get('turnId'), 'itemId': item.get('id')})
                if kind in ('commandExecution', 'mcpToolCall', 'dynamicToolCall', 'fileChange',
                            'webSearch', 'collabAgentToolCall') and item.get('id'):
                    self.tool_ids.add(item['id'])
                if kind == 'collabAgentToolCall':
                    self.handoffs.append({k: item[k] for k in
                        ('id', 'tool', 'senderThreadId', 'receiverThreadIds', 'status') if k in item})
            if 'ApprovalReview' in method and params.get('reviewId'):
                self.guardrail_ids.add(params['reviewId'])
            if method.endswith('/requestApproval'):
                self.guardrail_ids.add(str(params.get('approvalId') or params.get('itemId') or msg.get('id')))
        return msg

    def rpc(self, method, params):
        self.counter += 1
        request_id = self.counter
        self.send({'id': request_id, 'method': method, 'params': params})
        while True:
            msg = self.pump()
            if msg.get('id') == request_id and 'method' not in msg:
                if 'error' in msg:
                    raise RuntimeError('RPC failed: ' + method)
                return msg.get('result', {})

    def wait_turn(self, turn_id, since):
        while True:
            for params in self.completed_turns[since:]:
                if params.get('threadId') == self.thread_id and params.get('turn', {}).get('id') == turn_id:
                    status = params['turn'].get('status')
                    if status != 'completed':
                        raise RuntimeError('Turn did not complete: ' + str(status))
                    return
            self.pump()

    def run(self):
        self.start_backend()
        self.rpc('initialize', {'clientInfo': {'name': 'longtask_host_collector',
                  'version': '0.1.0'}, 'capabilities': {'experimentalApi': True}})
        self.send({'method': 'initialized', 'params': {}})
        if self.args.probe:
            return
        params = {'cwd': str(Path(self.args.cwd).resolve()),
                  'approvalPolicy': self.args.approval_policy, 'sandbox': self.args.sandbox}
        if self.args.model:
            params['model'] = self.args.model
        if self.args.thread_id:
            params['threadId'] = self.args.thread_id
            result = self.rpc('thread/resume', params)
        else:
            params['ephemeral'] = self.args.ephemeral
            result = self.rpc('thread/start', params)
        thread = result['thread']
        self.thread_id = thread['id']
        self.session_id = thread.get('sessionId')
        self.configured_model = result.get('model')
        if self.args.prompt_file:
            prompt = Path(self.args.prompt_file).read_text(encoding='utf-8')
            since = len(self.completed_turns)
            result = self.rpc('turn/start', {'threadId': self.thread_id,
                     'input': [{'type': 'text', 'text': prompt}]})
            self.turn_id = result['turn']['id']
            self.wait_turn(self.turn_id, since)
        if self.args.compact:
            before = len(self.compactions)
            since = len(self.completed_turns)
            self.rpc('thread/compact/start', {'threadId': self.thread_id})
            while len(self.compactions) == before:
                # A failed compaction is a failure, not an absent metric interpreted as zero.
                for params in self.completed_turns[since:]:
                    if params.get('turn', {}).get('status') in ('failed', 'interrupted'):
                        raise RuntimeError('Compaction failed or interrupted')
                self.pump()
            cid = self.compactions[-1].get('turnId')
            if cid:
                self.wait_turn(cid, since)
        self.rpc('thread/read', {'threadId': self.thread_id, 'includeTurns': False})

    def close(self):
        if self.process is None:
            self.trace.close()
            self.err.close()
            return
        # The process group is ours even when its leader has already exited.
        # Descendants may still hold stdout or continue tool side effects.
        if self.process.poll() is None:
            try:
                self.process.stdin.close()
                self.process.wait(timeout=3)
            except (subprocess.TimeoutExpired, BrokenPipeError):
                pass
        try:
            os.killpg(self.process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            self.process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            pass
        self.reader.join(timeout=1)
        # Do not rely on an open pipe to detect descendants that ignore TERM.
        try:
            os.killpg(self.process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        self.process.wait(timeout=3)
        self.reader.join(timeout=2)
        if self.reader.is_alive():
            raise RuntimeError('Trace reader did not terminate')
        self.process.stdout.close()
        self.trace.close()
        self.err.close()

    def summary(self, error):
        try:
            version = subprocess.run(['codex', '--version'], capture_output=True, text=True,
                                     timeout=5, check=True).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            version = None
        return {'collector_run_id': self.run_id, 'codex_version': version,
                'started_at': self.started_at,
                'evaluated_archive_sha256': self.args.evaluated_archive_sha256,
                'archive_binding_assurance': 'Caller-supplied digest; installation and archive bytes are not verified',
                'retention_until': self.args.retention_until,
                'synthetic_only_attested': self.args.synthetic_only,
                'configuration': {'sandbox': self.args.sandbox,
                    'approval_policy': self.args.approval_policy,
                    'explicit_override_count': len(self.args.config),
                    'override_values_recorded': False,
                    'runtime_uses_unmodified_defaults': not bool(self.args.config)},
                'assurance': {'release_gate': 'not_evaluated',
                    'identity_source': 'Observed App Server payloads',
                    'model_scope': 'Configured model plus observed rerouting; not attested per-request inference identity',
                    'trace_scope': 'Collector frames and actual JSON-RPC transport',
                    'human_calibration': 'not_performed',
                    'synthetic_scope': 'Caller attestation; collector cannot establish fixture provenance'},
                'status': 'collected' if error is None else 'unable_to_verify',
                'failure_class': type(error).__name__ if error else None,
                'backend_exit_status': self.process.returncode if self.process else None,
                'thread_id': self.thread_id, 'session_id': self.session_id,
                'last_turn_id': self.turn_id, 'last_turn_status': self.turn_status,
                'configured_model': self.configured_model,
                'model_rerouting_events': self.route_changes,
                'usage_updates': self.usage,
                'tool_ids': sorted(self.tool_ids), 'guardrail_ids': sorted(self.guardrail_ids),
                'handoff_events': self.handoffs, 'compaction_events': self.compactions,
                'interactive_requests': self.requests,
                'human_interventions': None, 'grader_calibrated': False,
                'automated_declines': sum(r['response'] == 'decline' for r in self.requests),
                'latency_ms': round((time.monotonic() - self.start) * 1000),
                'trace_path': str(self.trace_path),
                'trace_sha256': self.trace_hash.hexdigest(),
                'trace_scope': 'Private raw JSON-RPC requests and responses; not release evidence by itself'}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--out', required=True, help='New private output directory; must not exist')
    ap.add_argument('--cwd', default='.')
    ap.add_argument('--thread-id', help='Resume this actual persisted thread')
    ap.add_argument('--prompt-file', help='Optional UTF-8 synthetic task input')
    ap.add_argument('--compact', action='store_true', help='Request and await real compaction')
    ap.add_argument('--ephemeral', action='store_true', help='Disable persistence; cannot later resume')
    ap.add_argument('--sandbox', choices=['read-only', 'workspace-write'], default='read-only',
                    help='Default read-only; explicitly select workspace-write for authorized isolated fixtures')
    ap.add_argument('--model', help='Optional explicit model; omitted preserves configured default')
    ap.add_argument('--timeout', type=finite_timeout, default=60)
    ap.add_argument('--evaluated-archive-sha256', help='Current evaluated archive digest; no installation attestation')
    ap.add_argument('--retention-until', type=retention_deadline, required=True)
    ap.add_argument('--synthetic-only', action='store_true',
                    help='Attest that this new/resumed task and all fixture data are synthetic')
    ap.add_argument('--config', action='append', default=[],
                    help='Explicit caller-supplied Codex override; repeatable; never include secrets')
    ap.add_argument('--approval-policy', choices=['never', 'on-request'], default='never')
    ap.add_argument('--probe', action='store_true', help='Initialize transport only; no thread or model turn')
    args = ap.parse_args()
    if args.ephemeral and args.thread_id:
        ap.error('Ephemeral cannot be combined with resume')
    if args.evaluated_archive_sha256 is not None and not re.fullmatch(r'[0-9a-fA-F]{64}', args.evaluated_archive_sha256):
        ap.error('Archive SHA-256 must contain exactly 64 hexadecimal characters')
    if args.evaluated_archive_sha256:
        args.evaluated_archive_sha256 = args.evaluated_archive_sha256.lower()
    if not args.probe and (not args.evaluated_archive_sha256 or not args.synthetic_only):
        ap.error('Non-probe collection requires archive SHA-256 and synthetic-only attestation')
    if args.probe and (args.thread_id or args.prompt_file or args.compact):
        ap.error('Probe cannot include a task, resume, or compaction')
    if any('=' not in value or not value.split('=', 1)[0].strip() for value in args.config):
        ap.error('Each explicit config override must have key=value syntax')
    harness = None
    error = None
    def cancel(signum, frame):
        raise CollectionCancelled('Collection cancelled by signal ' + str(signum))
    previous_handlers = {sig: signal.signal(sig, cancel) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        harness = Harness(args)
        harness.run()
    except Exception as exc:
        error = exc
    finally:
        # A second cancellation must not interrupt bounded cleanup.
        for sig in previous_handlers:
            signal.signal(sig, signal.SIG_IGN)
        if harness is not None:
            try:
                harness.close()
            except Exception as exc:
                error = error or exc
        for sig, handler in previous_handlers.items():
            signal.signal(sig, handler)
    if harness is None:
        print(json.dumps({'status': 'unable_to_verify', 'failure_class': type(error).__name__,
                          'release_gate': 'not_evaluated'}))
        return 1
    try:
        locator_fd = open_directory(harness.out)
        try:
            located, original = os.fstat(locator_fd), os.fstat(harness.directory_fd)
            if (located.st_dev, located.st_ino) != (original.st_dev, original.st_ino):
                error = error or RuntimeError('Output locator no longer resolves to original directory')
        finally:
            os.close(locator_fd)
    except OSError as exc:
        error = error or exc
    if harness.process is not None and harness.process.returncode != 0:
        error = error or RuntimeError('Backend exited unsuccessfully')
    summary = harness.summary(error)
    try:
        with private_file(harness.directory_fd, 'summary.json') as dest:
            dest.write(json.dumps(summary, indent=2, ensure_ascii=False) + '\n')
    finally:
        os.close(harness.directory_fd)
    print(json.dumps(summary, ensure_ascii=False))
    return 0 if error is None else 1


if __name__ == '__main__':
    raise SystemExit(main())
