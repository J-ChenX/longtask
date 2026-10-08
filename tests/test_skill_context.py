from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from scripts import measure_skill_context as measurement


class SkillContextTests(unittest.TestCase):
    @staticmethod
    def package(root: Path, description: str = "管理续接") -> None:
        entry = root / "skills/longtask-continue/SKILL.md"
        entry.parent.mkdir(parents=True)
        entry.write_text(f"---\nname: longtask-continue\ndescription: {description}\n---\n# Continue\n", encoding="utf-8")
        (root / "SKILL.md").write_bytes(
            "# 根\r\n[模式](skills/longtask-continue/SKILL.md)\r\n[资料][input]\r\n"
            "[input]: references/输入.md#合同\r\n```text\r\n[伪链接](missing.md)\r\n```\r\n".encode("utf-8")
        )
        (root / "references").mkdir()
        (root / "references/输入.md").write_text("# 合同\n", encoding="utf-8")

    def test_sizes_are_utf8_and_only_explicit_files_are_counted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.package(root)
            before = {str(path): path.read_bytes() for path in root.rglob("*") if path.is_file()}
            report = measurement.measure(root, ["SKILL.md", "SKILL.md"])
            self.assertEqual(len(report["selected_files"]), 1)
            self.assertEqual(report["selected_total"]["utf8_bytes"], len((root / "SKILL.md").read_bytes()))
            self.assertGreater(report["selected_total"]["utf8_bytes"], report["selected_total"]["characters"])
            self.assertEqual(report["discovery_description_total"], {"utf8_bytes": 12, "characters": 4})
            self.assertEqual(report["declared_local_reference_set"],
                             ["references/输入.md", "skills/longtask-continue/SKILL.md"])
            self.assertEqual(before, {str(path): path.read_bytes() for path in root.rglob("*") if path.is_file()})

    def test_aliases_count_one_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.package(root)
            (root / "alias.md").symlink_to(root / "SKILL.md")
            report = measurement.measure(root, ["SKILL.md", "./SKILL.md", "alias.md"])
            self.assertEqual([item["path"] for item in report["selected_files"]], ["SKILL.md"])
            self.assertEqual(report["selected_total"]["utf8_bytes"], len((root / "SKILL.md").read_bytes()))

    def test_comparison_has_explicit_selection_and_unknown_model_benefit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            baseline, candidate = Path(temporary) / "base", Path(temporary) / "candidate"
            self.package(baseline)
            self.package(candidate, "续接")
            output = io.StringIO()
            with redirect_stdout(output):
                status = measurement.main(["--baseline", str(baseline), "--candidate", str(candidate),
                                           "--mode", "continue", "--load", "references/输入.md"])
            result = json.loads(output.getvalue())
            self.assertEqual(status, 0)
            self.assertEqual(len(result["selection"]), 3)
            self.assertEqual(result["candidate_minus_baseline"]["discovery_description_total"]["characters"], -2)
            self.assertIsNone(result["model_tokens"])
            self.assertIsNone(result["runtime_benefit"])

    def test_exact_caller_selection_adds_no_root_or_reference(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.package(root)
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(measurement.main(["--candidate", str(root), "--only-loaded",
                                                   "--load", "references/输入.md"]), 0)
            report = json.loads(output.getvalue())
            self.assertEqual(report["selection"], ["references/输入.md"])
            self.assertEqual(report["selection_origin"], "caller_selected_paths")

    def test_missing_selected_input_is_failure_instead_of_zero(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.package(root)
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(measurement.main(["--candidate", str(root), "--load", "missing.md"]), 1)
            self.assertFalse(json.loads(output.getvalue())["valid"])

    def test_selection_rejects_escape_and_does_not_follow_reference_closure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "package"
            self.package(root)
            outside = Path(temporary) / "outside.md"
            outside.write_text("external input")
            (root / "escape.md").symlink_to(outside)
            for relative in ("../outside.md", str(outside), "escape.md"):
                with self.subTest(path=relative), self.assertRaises(ValueError):
                    measurement.measure(root, [relative])
            with (root / "SKILL.md").open("a") as handle:
                handle.write("[外部](../outside.md)\n[缺失](absent.md)\n")
            result = measurement.measure(root, ["SKILL.md"])
            links = result["selected_files"][0]["local_references"]
            self.assertTrue(any(link["path"] == "absent.md" and not link["exists"] for link in links))
            self.assertTrue(any(not link["within_package"] for link in links))
            self.assertEqual(len(result["selected_files"]), 1)


if __name__ == "__main__":
    unittest.main()
