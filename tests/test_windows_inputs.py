"""Native knowledge/required-input consumers: pinned reads, budgets and races."""
from __future__ import annotations

import json
import ctypes
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import knowledge_context as kc
import required_inputs as ri
import longtask_state as state
import acceptance_coverage as coverage


@unittest.skipUnless(os.name == "nt", "requires native Windows input consumers")
class WindowsInputTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="longtask-inputs-native-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.root = self.base / "workspace"
        self.root.mkdir()
        self.write("docs/ARCHITECTURE.md", "# 项目\n\n项目用途。\n\n## 边界\n\n原生读取😀。\n")
        self.write("docs/modules/模块.md", "# 模块\n\n模块职责。\n")
        self.write("source.py", "def example():\n    return '中文😀'\n")

    def write(self, path, content):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content.encode("utf-8"))
        return target

    def workspace(self):
        workspace = kc.Workspace(self.root)
        self.addCleanup(workspace.close)
        return workspace

    def session(self):
        session = ri.Session(self.root)
        self.addCleanup(session.close)
        return session

    def error(self, action, *codes):
        with self.assertRaises(kc.KnowledgeError) as raised:
            action()
        self.assertIn(raised.exception.code, codes)

    def junction(self, path, target):
        result = subprocess.run(["cmd", "/d", "/c", "mklink", "/J", str(path), str(target)],
                                capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.addCleanup(lambda: os.rmdir(path) if path.exists() else None)

    def test_index_read_diagnose_unicode_and_bindings(self):
        workspace = self.workspace()
        index = kc.index(workspace)
        self.assertTrue(index["complete"])
        self.assertEqual({e["path"] for e in index["entries"]},
                         {"docs/ARCHITECTURE.md", "docs/modules/模块.md"})
        read = kc.read(workspace, "docs/ARCHITECTURE.md#边界")
        self.assertIn("原生读取😀", read["text"])
        self.assertTrue(read["complete"])
        self.assertEqual(kc.diagnose(workspace)["total_findings"], 1)  # unlinked module
        self.error(lambda: kc.read(workspace, "docs/ARCHITECTURE.md", expected="0" * 64), "stale_binding")

    def test_required_document_and_code_inputs(self):
        session = self.session()
        result = ri.resolve(session, ["docs/ARCHITECTURE.md#边界", "code:source.py#example"], 16384, {})
        self.assertTrue(result["complete"])
        self.assertFalse(result["trust"]["grants_authorization"])
        self.assertIn("中文😀", json.dumps(result, ensure_ascii=False))

    def test_acceptance_consumer_uses_native_workspace(self):
        self.write("docs/ARCHITECTURE.md", "# 项目\n\n## 批准\n\n原生范围。\n\n## 验收\n\n"
                   "| ID | Outcome | Approval | Scope | Exclusion | Contract |\n"
                   "|---|---|---|---|---|---|\n"
                   "| AC1 | 中文😀 | docs/ARCHITECTURE.md#批准 | included | - | - |\n")
        session = self.session()
        result = coverage.query(session, "docs/ARCHITECTURE.md#验收")
        self.assertIn("中文😀", json.dumps(result, ensure_ascii=False))
        session.verify()
        process = subprocess.run([sys.executable, str(ROOT / "scripts/acceptance_coverage.py"),
                                  "query", "--root", str(self.root), "--ref", "docs/ARCHITECTURE.md#验收"],
                                 capture_output=True, env=dict(os.environ, PYTHONUTF8="0", PYTHONIOENCODING="cp936"))
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertNotIn(b"\r\n", process.stdout)
        self.assertIn("中文😀", process.stdout.decode("utf-8"))

    def test_required_state_observation_and_changed_checkpoint(self):
        args = [sys.executable, str(ROOT / "scripts/longtask_state.py"), "init", "--root", str(self.root),
                "--task-id", "inputs", "--mode", "continue", "--goal", "Native inputs"]
        result = subprocess.run(args, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        session = self.session()
        self.assertIsNotNone(session.observe())
        session.verify()
        path = self.root / ".longtask/state.json"
        data = path.read_bytes().replace(b"Native inputs", b"Changed goal!")
        path.write_bytes(data)
        self.error(session.verify, "read_race", "observation_changed")

    def test_junction_root_ancestor_and_tree_are_refused(self):
        self.junction(self.base / "alias", self.root)
        self.error(lambda: kc.Workspace(self.base / "alias"), "unsafe_root")
        self.error(lambda: kc.Workspace(self.base / "alias/docs"), "unsafe_root")
        self.junction(self.root / "docs/modules/link", self.base)
        self.error(lambda: kc.index(self.workspace()), "read_race", "unsafe_path")
        self.error(lambda: self.workspace().read("docs/modules/link/secret.md"), "unsafe_path")

    def test_file_and_directory_symlinks_are_refused_without_target_reads(self):
        outside = self.base / "outside.md"
        outside.write_bytes(b"secret")
        (self.root / "link.md").symlink_to(outside)
        (self.root / "linked").symlink_to(self.base, target_is_directory=True)
        workspace = self.workspace()
        for path in ("link.md", "linked/outside.md"):
            self.error(lambda: workspace.read(path), "unsafe_path")

    def test_hardlinked_documents_source_and_checkpoint_are_refused(self):
        for path in ("docs/ARCHITECTURE.md", "source.py", ".longtask/state.json"):
            if path.startswith(".longtask"):
                self.write(path, "{}")
            (self.base / (Path(path).name + ".alias")).hardlink_to(self.root / path)
            self.error(lambda: ri.safe_read(self.workspace(), path), "unsafe_path")
        self.error(lambda: self.workspace().read("docs/ARCHITECTURE.md"), "unsafe_path")

    def test_short_name_alias_cannot_bypass_excluded_knowledge_namespace(self):
        self.write(".longtask/hidden.md", "not project knowledge")
        directory = self.root / ".longtask"
        buffer = ctypes.create_unicode_buffer(32768)
        length = ctypes.windll.kernel32.GetShortPathNameW(str(directory), buffer, len(buffer))
        self.assertGreater(length, 0)
        self.assertLess(length, len(buffer))
        short = Path(buffer.value).name
        self.assertNotEqual(short.casefold(), directory.name.casefold(),
                            "native alias fixture requires DOS 8.3 creation on TEMP volume")
        self.error(lambda: self.workspace().read(short + "/hidden.md"), "unsafe_path")

    def test_namespace_case_aliases_cannot_expose_state_or_git_inputs(self):
        self.write(".git/hidden.md", "not knowledge")
        self.write(".git/hidden.py", "def hidden(): pass\n")
        self.write(".longtask/hidden.md", "not knowledge")
        self.write(".longtask/hidden.py", "def hidden(): pass\n")
        workspace = self.workspace()
        for path in (".GIT/hidden.md", ".LONGTASK/hidden.md"):
            self.error(lambda: workspace.read(path), "invalid_path")
        self.error(lambda: ri.safe_read(workspace, ".GIT/hidden.py"), "invalid_path")
        self.error(lambda: ri.symbol(self.session(), "code:.LONGTASK/hidden.py#hidden"), "invalid_path")

    def test_leaf_replacement_in_open_window_is_detected(self):
        workspace = self.workspace()
        target = self.root / "source.py"
        original = kc.fs.open
        changed = False
        def opening(name, flags, *args, **kwargs):
            nonlocal changed
            if name == "source.py" and not changed:
                changed = True
                target.rename(self.root / "old.py")
                target.write_bytes(b"new")
            return original(name, flags, *args, **kwargs)
        with mock.patch.object(kc.fs, "open", side_effect=opening):
            self.error(lambda: ri.safe_read(workspace, "source.py"), "read_race")

    def test_pinned_leaf_cannot_be_replaced_and_content_mutation_is_detected(self):
        workspace = self.workspace()
        workspace.read("docs/ARCHITECTURE.md")
        target = self.root / "docs/ARCHITECTURE.md"
        with self.assertRaises(OSError):
            target.rename(self.root / "moved.md")
        target.write_bytes(b"changed")
        self.error(workspace.verify, "read_race")

    def test_missing_path_and_directory_membership_changes_are_detected(self):
        workspace = self.workspace()
        self.assertFalse(workspace.exists("missing.md"))
        self.write("missing.md", "new")
        self.error(workspace.verify, "read_race")
        workspace = self.workspace()
        list(workspace.walk("docs/modules"))
        self.write("docs/modules/new.md", "new")
        self.error(workspace.verify, "read_race")

    def test_streaming_node_budget_stops_native_enumeration(self):
        workspace = self.workspace()
        for i in range(5):
            self.write(f"docs/modules/{i}.md", "data")
        with mock.patch.object(kc, "MAX_NODES", 2):
            self.error(lambda: list(workspace.walk("docs/modules")), "input_too_large")

    def test_cli_utf8_lf_without_utf8_mode(self):
        env = dict(os.environ, PYTHONUTF8="0", PYTHONIOENCODING="cp936")
        for script, args in (("knowledge_context.py", ["read", "--ref", "docs/ARCHITECTURE.md"]),
                             ("required_inputs.py", ["resolve", "--ref", "code:source.py#example"])):
            result = subprocess.run([sys.executable, str(ROOT / "scripts" / script), *args,
                                     "--root", str(self.root)], capture_output=True, env=env)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn(b"\r\n", result.stdout)
            self.assertIn("😀", result.stdout.decode("utf-8"))

    def test_planning_and_discovery_mutations_consume_utf8_with_ansi_defaults(self):
        project = self.base / "new-project"
        project.mkdir()
        env = dict(os.environ, PYTHONUTF8="0", PYTHONIOENCODING="cp936")
        result = subprocess.run([sys.executable, str(ROOT / "scripts/planning_checkpoint.py"),
                                 "begin", "--root", str(project), "--goal", "项目😀",
                                 "--next-action", "检查😀"], capture_output=True, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        current = json.loads(result.stdout)
        self.assertTrue(current["checkpoint_saved"])
        import discovery_checkpoint as discovery
        with mock.patch.object(subprocess, "_text_encoding", return_value="cp936"):
            updated = discovery.cli_mutation("checkpoint", str(project), current["task_id"],
                                             current["revision"], "native-fixture",
                                             ["--next-action", "继续😀"])
        self.assertEqual(updated["next_action"], "继续😀")


if __name__ == "__main__":
    unittest.main()
