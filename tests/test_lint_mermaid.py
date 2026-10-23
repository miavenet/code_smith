import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("lint_mermaid", ROOT / "tools" / "lint_mermaid.py")
lint_mermaid = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = lint_mermaid
SPEC.loader.exec_module(lint_mermaid)


class MermaidLintTests(unittest.TestCase):
    def write(self, directory, text):
        path = Path(directory) / "guide.md"
        path.write_text(text, encoding="utf-8")
        return path

    def test_scan_preserves_diagram_source_location(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write(
                directory,
                "# Guide\n\n```mermaid\nflowchart LR\n    A --> B\n```\n",
            )
            diagrams, problems, changed = lint_mermaid.scan(path)

        self.assertEqual(problems, [])
        self.assertFalse(changed)
        self.assertEqual(len(diagrams), 1)
        self.assertEqual(diagrams[0].fence_line, 3)
        self.assertEqual(diagrams[0].source_line, 4)
        self.assertEqual(diagrams[0].source, "flowchart LR\n    A --> B\n")

    def test_fix_canonicalizes_only_mermaid_fence_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write(
                directory,
                "~~~ Mermaid  \nflowchart LR\n    A --> B\n~~~\n\n``` python\nprint(1)\n```\n",
            )
            _, first_problems, first_changed = lint_mermaid.scan(path, fix=True)
            after_first = path.read_text(encoding="utf-8")
            _, second_problems, second_changed = lint_mermaid.scan(path, fix=True)

        self.assertTrue(first_changed)
        self.assertEqual(first_problems, [])
        self.assertEqual(after_first, "~~~mermaid\nflowchart LR\n    A --> B\n~~~\n\n``` python\nprint(1)\n```\n")
        self.assertEqual(second_problems, [])
        self.assertFalse(second_changed)

    def test_fix_preserves_crlf_and_removes_mermaid_info_suffix(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "guide.md"
            path.write_bytes(b"``` Mermaid preview  \r\nflowchart LR\r\nA --> B\r\n```\r\n")

            _, problems, changed = lint_mermaid.scan(path, fix=True)

            self.assertEqual(problems, [])
            self.assertTrue(changed)
            self.assertEqual(path.read_bytes(), b"```mermaid\r\nflowchart LR\r\nA --> B\r\n```\r\n")

    def test_fixes_gantt_label_colon_without_changing_time(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write(
                directory,
                "```mermaid\n"
                "gantt\n"
                "    dateFormat YYYY-MM-DD HH:mm\n"
                "    review: principal engineer :r1, 2026-01-01 06:00, 4h\n"
                "    deploy at 08:00          :d1, 2026-01-01 10:00, 1h\n"
                "```\n",
            )
            _, before, _ = lint_mermaid.scan(path)
            _, after, changed = lint_mermaid.scan(path, fix=True)
            fixed = path.read_text(encoding="utf-8")
            _, repeated, changed_again = lint_mermaid.scan(path, fix=True)

        self.assertEqual(len(before), 1)
        self.assertEqual(before[0].line, 4)
        self.assertIn("Gantt task label", before[0].message)
        self.assertEqual(after, [])
        self.assertTrue(changed)
        self.assertIn("review - principal engineer :r1, 2026-01-01 06:00, 4h", fixed)
        self.assertIn("deploy at 08:00          :d1, 2026-01-01 10:00, 1h", fixed)
        self.assertEqual(repeated, [])
        self.assertFalse(changed_again)

    def test_rejects_numeric_gantt_date_format_without_guessing_a_fix(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write(
                directory,
                "```mermaid\ngantt\n    dateFormat X\n    work :w1, 0, 4\n```\n",
            )

            _, problems, changed = lint_mermaid.scan(path, fix=True)

        self.assertFalse(changed)
        self.assertEqual(len(problems), 1)
        self.assertEqual(problems[0].line, 3)
        self.assertIn("dateFormat X", problems[0].message)

    def test_adds_contrasting_text_to_hex_class_fills(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write(
                directory,
                "```mermaid\nflowchart LR\nA --> B\n"
                "classDef pale fill:#e8f4e8,stroke:#4a4\n"
                "classDef dark fill:#123,stroke:#456\n"
                "classDef ended fill:#eee,stroke:#444;\n"
                "classDef fillonly fill:#fff;\n"
                "classDef ready fill:#fff,color:#222\n```\n",
            )

            _, problems, changed = lint_mermaid.scan(path, fix=True)
            fixed = path.read_text(encoding="utf-8")
            _, repeated, changed_again = lint_mermaid.scan(path, fix=True)

        self.assertEqual(problems, [])
        self.assertTrue(changed)
        self.assertIn("classDef pale fill:#e8f4e8,stroke:#4a4,color:#000000", fixed)
        self.assertIn("classDef dark fill:#123,stroke:#456,color:#ffffff", fixed)
        self.assertIn("classDef ended fill:#eee,stroke:#444,color:#000000;", fixed)
        self.assertIn("classDef fillonly fill:#fff,color:#000000;", fixed)
        self.assertIn("classDef ready fill:#fff,color:#222", fixed)
        self.assertEqual(repeated, [])
        self.assertFalse(changed_again)

    def test_reports_named_class_fill_that_cannot_be_safely_fixed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write(
                directory,
                "```mermaid\nflowchart LR\nA --> B\nclassDef warning fill:red,stroke:#000\n```\n",
            )

            _, problems, changed = lint_mermaid.scan(path, fix=True)

        self.assertFalse(changed)
        self.assertEqual(len(problems), 1)
        self.assertEqual(problems[0].line, 4)
        self.assertIn("explicit color", problems[0].message)

    def test_reports_unterminated_and_empty_blocks(self):
        with tempfile.TemporaryDirectory() as directory:
            empty = self.write(directory, "before\n```mermaid\n\n```\n")
            _, empty_problems, _ = lint_mermaid.scan(empty)
            broken = Path(directory) / "broken.md"
            broken.write_text("one\ntwo\n```mermaid\nflowchart LR\n", encoding="utf-8")
            _, broken_problems, _ = lint_mermaid.scan(broken)

        self.assertEqual(empty_problems[0].line, 3)
        self.assertEqual(empty_problems[0].message, "empty Mermaid diagram")
        self.assertEqual(broken_problems[0].line, 3)
        self.assertEqual(broken_problems[0].message, "unterminated Mermaid code fence")

    @mock.patch.object(lint_mermaid.subprocess, "run")
    def test_renderer_maps_mermaid_parser_line_to_markdown(self, run):
        run.return_value = subprocess.CompletedProcess(
            [], 1, stdout="", stderr="Error: Parse error on line 2:\nunexpected token\n"
        )
        diagram = lint_mermaid.Diagram(Path("guide.md"), 10, 11, "flowchart LR\nA -x B\n")

        problem = lint_mermaid.MermaidCLI("mmdc", puppeteer_config="browser.json").check(diagram)

        self.assertIsNotNone(problem)
        self.assertEqual(problem.line, 12)
        self.assertIn("Parse error on line 2", problem.message)
        command = run.call_args.args[0]
        self.assertEqual(command[:2], ["mmdc", "--quiet"])
        self.assertEqual(command[2:4], ["--puppeteerConfigFile", "browser.json"])
        self.assertEqual(command[-2], "--output")

    @mock.patch.object(lint_mermaid.subprocess, "run", side_effect=subprocess.TimeoutExpired("mmdc", 3))
    def test_renderer_reports_timeout_at_diagram_line(self, run):
        diagram = lint_mermaid.Diagram(Path("guide.md"), 4, 5, "flowchart LR\nA --> B\n")

        problem = lint_mermaid.MermaidCLI("mmdc", timeout=3).check(diagram)

        self.assertEqual(problem.line, 5)
        self.assertEqual(problem.message, "Mermaid render timed out after 3 seconds")

    @mock.patch.object(lint_mermaid.subprocess, "run", side_effect=OSError("not executable"))
    def test_renderer_reports_launch_error_at_diagram_line(self, run):
        diagram = lint_mermaid.Diagram(Path("guide.md"), 4, 5, "flowchart LR\nA --> B\n")

        problem = lint_mermaid.MermaidCLI("mmdc").check(diagram)

        self.assertEqual(problem.line, 5)
        self.assertEqual(problem.message, "cannot run Mermaid CLI: not executable")

    def test_repository_tutorial_and_runbook_fences_are_well_formed(self):
        paths = lint_mermaid.markdown_paths(list(lint_mermaid.DEFAULT_PATHS))
        problems, count, _ = lint_mermaid.lint(paths, renderer=None)

        self.assertEqual(problems, [])
        self.assertGreaterEqual(count, 24)


if __name__ == "__main__":
    unittest.main()
