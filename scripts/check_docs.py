"""Documentation checks: Mermaid syntax, referenced paths, and commands.

There is no Node on this machine, so Mermaid cannot be rendered here. This does
the next most useful thing: it parses every fenced ```mermaid block structurally
-- declared diagram type, balanced brackets and quotes, every arrow's endpoints
declared, no stray tabs -- and it checks that every path, module and function the
docs name actually exists at HEAD.

A diagram that renders but describes a module nobody wrote is worse than no
diagram, so the second check matters more than the first.

    uv run python scripts/check_docs.py
"""

from __future__ import annotations

import ast
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
DOCS = [ROOT / "README.md", *sorted((ROOT / "docs").glob("*.md")),
        ROOT / "deploy" / "beta.md"]

VALID_HEADERS = (
    "flowchart", "graph", "sequenceDiagram", "stateDiagram-v2", "stateDiagram",
    "erDiagram", "classDiagram", "journey", "gantt", "pie", "mindmap",
)
ARROW = re.compile(r"(-{2,3}>|-{2,3}\||==>|-\.->|--)")


def mermaid_blocks(text: str):
    for match in re.finditer(r"```mermaid\n(.*?)```", text, re.S):
        yield match.start(), match.group(1)


def check_mermaid(path: pathlib.Path, problems: list[str]) -> int:
    text = path.read_text()
    count = 0
    for offset, block in mermaid_blocks(text):
        count += 1
        line_no = text[:offset].count("\n") + 1
        where = f"{path.relative_to(ROOT)}:{line_no}"
        lines = [l for l in block.splitlines() if l.strip()]
        if not lines:
            problems.append(f"{where}: empty diagram")
            continue
        header = lines[0].strip()
        if not header.startswith(VALID_HEADERS):
            problems.append(f"{where}: unknown diagram type {header!r}")
        # `erDiagram` has its own grammar -- `||--o{` is a cardinality marker,
        # not a brace, and its lines are not arrows in the flowchart sense. The
        # structural checks below are about flowchart-family syntax only.
        if header.startswith(("erDiagram", "classDiagram", "sequenceDiagram",
                              "gantt", "pie", "journey", "mindmap")):
            continue
        for symbol, opener, closer in (("[]", "[", "]"), ("()", "(", ")"),
                                       ("{}", "{", "}")):
            if block.count(opener) != block.count(closer):
                problems.append(
                    f"{where}: unbalanced {symbol} "
                    f"({block.count(opener)} vs {block.count(closer)})")
        if block.count('"') % 2:
            problems.append(f"{where}: odd number of double quotes")
        if "\t" in block:
            problems.append(f"{where}: contains a tab; Mermaid wants spaces")
        # Every node an arrow touches must be introduced somewhere in the block.
        # A node is declared the first time it appears with a shape, wherever
        # that is -- `A["x"]` on its own line, or inline as `--> A(["x"])`.
        declared = set(re.findall(r"(?<![\w.])([A-Za-z][\w]*)\s*[\[\(\{>]", block))
        declared |= set(re.findall(r"(?:subgraph|end)\s+([A-Za-z][\w]*)", block))
        for line in lines[1:]:
            if not ARROW.search(line) or line.strip().startswith("%%"):
                continue
            for name in re.findall(r"(?<![\w.])([A-Za-z][\w]*)(?=\s*(?:-{2,3}|==|-\.))",
                                   line):
                declared.add(name)
        for line in lines[1:]:
            if line.strip().startswith("%%") or not ARROW.search(line):
                continue
            tail = ARROW.split(line)[-1].strip()
            target = re.match(r"([A-Za-z][\w]*)", tail.lstrip("|").split("|")[-1].strip())
            if target and target.group(1) not in declared:
                if target.group(1) not in {"end"}:
                    problems.append(
                        f"{where}: arrow points at undeclared node "
                        f"{target.group(1)!r}")
    return count


CODE_REF = re.compile(r"`(src/[\w/]+\.py|tests/[\w/]+\.py|scripts/[\w/]+\.\w+|"
                      r"deploy/[\w/.]+|docs/[\w.]+\.md)`")
LINK_REF = re.compile(r"\]\((?!https?:)([^)#]+)")
SYMBOL_REF = re.compile(r"`([a-z_][\w]*)\(\)`")


def without_diagrams(text: str) -> str:
    """Prose only. Labels inside a diagram are pictures of code, not references
    to it -- `min(market low, blended low)` names no function this repo defines."""
    return re.sub(r"```mermaid\n.*?```", "", text, flags=re.S)


def check_references(path: pathlib.Path, problems: list[str]) -> int:
    text = without_diagrams(path.read_text())
    checked = 0
    for match in CODE_REF.finditer(text):
        checked += 1
        if not (ROOT / match.group(1)).exists():
            problems.append(f"{path.relative_to(ROOT)}: no such path {match.group(1)}")
    for match in LINK_REF.finditer(text):
        target = match.group(1).strip().split(":")[0]
        if not target or target.startswith("#"):
            continue
        checked += 1
        candidate = (path.parent / target).resolve()
        if not candidate.exists() and not (ROOT / target).exists():
            problems.append(f"{path.relative_to(ROOT)}: broken link {target}")
    return checked


def public_symbols() -> set[str]:
    names: set[str] = set()
    for file in (ROOT / "src").rglob("*.py"):
        try:
            tree = ast.parse(file.read_text())
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(node.name)
    return names


def check_symbols(path: pathlib.Path, known: set[str], problems: list[str]) -> int:
    checked = 0
    for match in SYMBOL_REF.finditer(without_diagrams(path.read_text())):
        checked += 1
        if match.group(1) not in known:
            problems.append(
                f"{path.relative_to(ROOT)}: no such function {match.group(1)}()")
    return checked


def main() -> int:
    problems: list[str] = []
    known = public_symbols()
    diagrams = refs = symbols = 0
    for path in DOCS:
        if not path.exists():
            continue
        diagrams += check_mermaid(path, problems)
        refs += check_references(path, problems)
        symbols += check_symbols(path, known, problems)
    print(f"  {len(DOCS)} document(s), {diagrams} diagram(s), "
          f"{refs} path/link reference(s), {symbols} function reference(s)")
    for problem in problems:
        print(f"  FAIL {problem}")
    print("  all checks passed" if not problems else f"  {len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
