#!/usr/bin/env python3
"""Lint Mermaid diagrams embedded in Markdown with Mermaid's official CLI.

The checker deliberately delegates Mermaid grammar and rendering to ``mmdc``.
Its own parser only locates CommonMark fenced blocks so renderer diagnostics can
be mapped back to the Markdown source.  ``--fix`` applies only narrow,
idempotent repairs: canonical fences, Gantt label separators, and contrasting
text colors for hexadecimal ``classDef`` fills.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Iterable, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PATHS = (PROJECT_ROOT / "docs" / "tutorial", PROJECT_ROOT / "docs" / "runbook.md")
OPEN_FENCE = re.compile(r"^(?P<indent> {0,3})(?P<marker>`{3,}|~{3,})(?P<info>.*)$")
PARSER_LINE = re.compile(r"(?:Parse error on line|line)\s+(\d+)", re.IGNORECASE)
GANTT_LABEL_COLON = re.compile(
    r"^(?P<prefix>\s*[^:\r\n]+): (?P<rest>[A-Za-z][^:\r\n]*?)(?P<spacing>\s+):"
    r"(?P<metadata>(?:(?:active|after|crit|done|milestone),\s*)*[A-Za-z][\w-]*,)",
    re.IGNORECASE,
)
CLASSDEF = re.compile(r"^(?P<prefix>\s*classDef\s+\S+\s+)(?P<styles>[^\r\n]+)$", re.IGNORECASE)


@dataclass(frozen=True)
class Diagram:
    path: Path
    fence_line: int
    source_line: int
    source: str


@dataclass(frozen=True)
class Problem:
    path: Path | None
    line: int | None
    message: str

    def display(self) -> str:
        if self.path is None:
            return f"lint-mermaid: {self.message}"
        location = str(self.path)
        if self.line is not None:
            location += f":{self.line}"
        return f"{location}: {self.message}"


def markdown_paths(inputs: Sequence[Path]) -> list[Path]:
    paths: set[Path] = set()
    for item in inputs:
        if item.is_dir():
            paths.update(path for path in item.rglob("*.md") if path.is_file())
        else:
            paths.add(item)
    return sorted(paths, key=lambda path: str(path))


def _is_close(line: str, marker: str) -> bool:
    match = re.match(r"^ {0,3}(`{3,}|~{3,})[ \t]*$", line.rstrip("\r\n"))
    return bool(match and match.group(1)[0] == marker[0] and len(match.group(1)) >= len(marker))


def _gantt_label_fixes(source: str) -> list[tuple[int, str]]:
    lines = source.splitlines(keepends=True)
    first = next((line.strip() for line in lines if line.strip()), "")
    if first != "gantt":
        return []
    fixes = []
    for line_number, line in enumerate(lines):
        match = GANTT_LABEL_COLON.match(line.rstrip("\r\n"))
        if match:
            newline = "\r\n" if line.endswith("\r\n") else "\n" if line.endswith("\n") else ""
            fixed = (
                f'{match.group("prefix")} - {match.group("rest")}'
                f'{match.group("spacing")}:{match.group("metadata")}'
            )
            fixed += line.rstrip("\r\n")[match.end() :] + newline
            fixes.append((line_number, fixed))
    return fixes


def _gantt_numeric_date_lines(source: str) -> list[int]:
    lines = source.splitlines()
    first = next((line.strip() for line in lines if line.strip()), "")
    if first != "gantt":
        return []
    return [
        line_number
        for line_number, line in enumerate(lines)
        if re.match(r"^\s*dateFormat\s+X\s*$", line, re.IGNORECASE)
    ]


def _contrast_color(fill: str) -> str | None:
    match = re.fullmatch(r"#([0-9a-f]{3}|[0-9a-f]{6})", fill, re.IGNORECASE)
    if not match:
        return None
    value = match.group(1)
    if len(value) == 3:
        value = "".join(character * 2 for character in value)
    channels = [int(value[index : index + 2], 16) / 255 for index in (0, 2, 4)]
    linear = [channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4 for channel in channels]
    luminance = 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]
    return "#000000" if luminance >= 0.179 else "#ffffff"


def _classdef_fill_fixes(source: str) -> list[tuple[int, str | None]]:
    fixes = []
    for line_number, line in enumerate(source.splitlines(keepends=True)):
        match = CLASSDEF.match(line.rstrip("\r\n"))
        if not match:
            continue
        properties = [part.strip() for part in match.group("styles").split(",")]
        values = {
            key.strip().lower(): value.strip().removesuffix(";").strip()
            for property_text in properties
            if ":" in property_text
            for key, value in [property_text.split(":", 1)]
        }
        if "fill" not in values or "color" in values:
            continue
        color = _contrast_color(values["fill"])
        fixed = None
        if color:
            newline = "\r\n" if line.endswith("\r\n") else "\n" if line.endswith("\n") else ""
            styles = match.group("styles").rstrip()
            terminator = ";" if styles.endswith(";") else ""
            styles = styles.removesuffix(";")
            fixed = f'{match.group("prefix")}{styles},color:{color}{terminator}{newline}'
        fixes.append((line_number, fixed))
    return fixes


def scan(path: Path, fix: bool = False) -> tuple[list[Diagram], list[Problem], bool]:
    try:
        with path.open("r", encoding="utf-8", newline="") as source_file:
            original = source_file.read()
    except (OSError, UnicodeError) as exc:
        return [], [Problem(path, None, f"cannot read Markdown: {exc}")], False

    lines = original.splitlines(keepends=True)
    diagrams: list[Diagram] = []
    problems: list[Problem] = []
    changed = False
    index = 0
    while index < len(lines):
        match = OPEN_FENCE.match(lines[index].rstrip("\r\n"))
        if not match:
            index += 1
            continue

        marker = match.group("marker")
        raw_info = match.group("info")
        info = raw_info.strip()
        mermaid = bool(info) and info.split(maxsplit=1)[0].lower() == "mermaid"
        canonical = raw_info == "mermaid"
        start = index
        index += 1
        content_start = index
        while index < len(lines) and not _is_close(lines[index], marker):
            index += 1

        if mermaid and index == len(lines):
            problems.append(Problem(path, start + 1, "unterminated Mermaid code fence"))
            break

        if not mermaid:
            if index < len(lines):
                index += 1
            continue

        if not canonical:
            if fix:
                newline = "\r\n" if lines[start].endswith("\r\n") else "\n" if lines[start].endswith("\n") else ""
                lines[start] = f'{match.group("indent")}{marker}mermaid{newline}'
                changed = True
            else:
                problems.append(
                    Problem(
                        path,
                        start + 1,
                        "non-canonical Mermaid fence; expected the language immediately after the marker",
                    )
                )

        source = "".join(lines[content_start:index])
        if not source.strip():
            problems.append(Problem(path, content_start + 1, "empty Mermaid diagram"))
        else:
            gantt_fixes = _gantt_label_fixes(source)
            for offset, fixed_line in gantt_fixes:
                if fix:
                    lines[content_start + offset] = fixed_line
                    changed = True
                else:
                    problems.append(
                        Problem(
                            path,
                            content_start + offset + 1,
                            "colon in Gantt task label is parsed as a field separator; use ' -' in the label",
                        )
                    )
            if gantt_fixes and fix:
                source = "".join(lines[content_start:index])
            for offset in _gantt_numeric_date_lines(source):
                problems.append(
                    Problem(
                        path,
                        content_start + offset + 1,
                        "Gantt dateFormat X collapses numeric start positions; use explicit dates or times with durations",
                    )
                )
            classdef_fixes = _classdef_fill_fixes(source)
            for offset, fixed_line in classdef_fixes:
                if fix and fixed_line is not None:
                    lines[content_start + offset] = fixed_line
                    changed = True
                else:
                    problems.append(
                        Problem(
                            path,
                            content_start + offset + 1,
                            "classDef with an explicit fill needs an explicit color for light and dark themes",
                        )
                    )
            if classdef_fixes and fix:
                source = "".join(lines[content_start:index])
            diagrams.append(Diagram(path, start + 1, content_start + 1, source))
        index += 1

    if fix and changed:
        with path.open("w", encoding="utf-8", newline="") as source_file:
            source_file.write("".join(lines))
    return diagrams, problems, changed


class MermaidCLI:
    def __init__(self, executable: str, timeout: float = 60, puppeteer_config: str | None = None):
        self.executable = executable
        self.timeout = timeout
        self.puppeteer_config = puppeteer_config

    def check(self, diagram: Diagram) -> Problem | None:
        with tempfile.TemporaryDirectory(prefix="lint-mermaid-") as directory:
            source = Path(directory) / "diagram.mmd"
            output = Path(directory) / "diagram.svg"
            source.write_text(diagram.source, encoding="utf-8")
            command = [self.executable, "--quiet"]
            if self.puppeteer_config:
                command.extend(["--puppeteerConfigFile", self.puppeteer_config])
            command.extend(["--input", str(source), "--output", str(output)])
            try:
                result = subprocess.run(
                    command,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    check=False,
                    timeout=self.timeout,
                )
            except subprocess.TimeoutExpired:
                return Problem(
                    diagram.path,
                    diagram.source_line,
                    f"Mermaid render timed out after {self.timeout:g} seconds",
                )
            except OSError as exc:
                return Problem(diagram.path, diagram.source_line, f"cannot run Mermaid CLI: {exc}")
        if result.returncode == 0:
            return None

        detail = _renderer_detail(result.stdout, result.stderr, source, output)
        parser_line = PARSER_LINE.search(detail)
        line = diagram.source_line
        if parser_line:
            line += max(0, int(parser_line.group(1)) - 1)
        return Problem(diagram.path, line, f"Mermaid render failed: {detail}")


def _renderer_detail(stdout: str, stderr: str, source: Path, output: Path) -> str:
    text = stderr.strip() or stdout.strip() or "mmdc exited unsuccessfully"
    text = text.replace(str(source), "<diagram>").replace(str(output), "<output>")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for number, line in enumerate(lines):
        if "parse error" in line.lower() or line.lower().startswith("error:"):
            lines = lines[number:]
            break
    return " | ".join(lines[:4])


def lint(
    paths: Iterable[Path], renderer: MermaidCLI | None, fix: bool = False
) -> tuple[list[Problem], int, int]:
    problems: list[Problem] = []
    diagram_count = 0
    changed_count = 0
    for path in paths:
        diagrams, found, changed = scan(path, fix=fix)
        problems.extend(found)
        diagram_count += len(diagrams)
        changed_count += int(changed)
        if renderer:
            for diagram in diagrams:
                problem = renderer.check(diagram)
                if problem:
                    problems.append(problem)
    return problems, diagram_count, changed_count


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*", type=Path, help="Markdown files or directories")
    parser.add_argument(
        "--fix",
        action="store_true",
        help="fix safe fence, Gantt-label, and classDef contrast defects in place",
    )
    parser.add_argument(
        "--mmdc",
        default=os.environ.get("MMDC", "mmdc"),
        help="path to the official Mermaid CLI (default: MMDC or mmdc on PATH)",
    )
    parser.add_argument(
        "--structure-only",
        action="store_true",
        help="check fences without invoking Mermaid (does not validate diagram syntax)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=60,
        help="seconds allowed for each Mermaid CLI render (default: 60)",
    )
    parser.add_argument(
        "--puppeteer-config",
        help="optional Puppeteer JSON config passed to mmdc (for example, browser launch arguments)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    paths = markdown_paths(args.paths or list(DEFAULT_PATHS))
    if not paths:
        print(Problem(None, None, "no Markdown files found").display(), file=sys.stderr)
        return 2
    missing = [path for path in paths if not path.is_file()]
    if missing:
        for path in missing:
            print(Problem(path, None, "Markdown path does not exist").display(), file=sys.stderr)
        return 2

    renderer = None
    if not args.structure_only:
        executable = shutil.which(args.mmdc) if not os.path.dirname(args.mmdc) else args.mmdc
        if not executable or not Path(executable).is_file():
            print(
                Problem(
                    None,
                    None,
                    f"Mermaid CLI not found: {args.mmdc!r}; install @mermaid-js/mermaid-cli or pass --mmdc",
                ).display(),
                file=sys.stderr,
            )
            return 2
        if args.timeout <= 0:
            print(Problem(None, None, "--timeout must be greater than zero").display(), file=sys.stderr)
            return 2
        if args.puppeteer_config and not Path(args.puppeteer_config).is_file():
            print(
                Problem(None, None, f"Puppeteer config does not exist: {args.puppeteer_config}").display(),
                file=sys.stderr,
            )
            return 2
        renderer = MermaidCLI(
            executable,
            timeout=args.timeout,
            puppeteer_config=args.puppeteer_config,
        )

    problems, count, changed = lint(paths, renderer, fix=args.fix)
    for problem in problems:
        print(problem.display(), file=sys.stderr)
    if problems:
        return 1
    suffix = f"; fixed {changed} file(s)" if args.fix else ""
    print(f"Mermaid lint passed: {count} diagram(s) in {len(paths)} Markdown file(s){suffix}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
