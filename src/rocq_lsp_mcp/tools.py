"""The tools themselves, independent of how they are exposed.

Each function takes a `Workspace` and returns text, so the CLI, the daemon
and the MCP server all share one implementation.
"""

from __future__ import annotations

import subprocess
import uuid
from collections import Counter
from pathlib import Path
from typing import List, Optional

from rocq_lsp_mcp.file_utils import find_project_file, get_file_contents
from rocq_lsp_mcp.rocq_client import RocqLSPError
from rocq_lsp_mcp.search_utils import check_ripgrep_status
from rocq_lsp_mcp.search_utils import rocq_local_search as _ripgrep_declarations
from rocq_lsp_mcp.utils import (
    SEVERITY,
    annotate_lines,
    filter_diagnostics_by_position,
    format_diagnostics,
    format_line,
    format_proof_view,
)
from rocq_lsp_mcp.workspace import Workspace

_RG_AVAILABLE, _RG_MESSAGE = check_ripgrep_status()


def goal(ws: Workspace, file_path: str, line: int, column: Optional[int] = None) -> str:
    """Proof state at a line. Only checks up to that line."""
    client, rel_path, _ = ws.open(file_path)
    content = client.get_file_content(rel_path)
    lines = content.split("\n")
    if line < 1 or line > len(lines):
        return f"Line {line} is out of range; the file has {len(lines)} lines."
    text = lines[line - 1]

    if column is None:
        indent = len(text) - len(text.lstrip())
        before = format_proof_view(
            client.goals_at(rel_path, line - 1, indent), "No goals before this line."
        )
        after = format_proof_view(
            client.goals_at(rel_path, line - 1, None), "No goals after this line."
        )
        return f"Line {line}:\n{text}\n\n--- before ---\n{before}\n\n--- after ---\n{after}"

    view = client.goals_at(rel_path, line - 1, column - 1)
    return (
        f"Goals at:\n{format_line(content, line, column)}\n\n"
        f"{format_proof_view(view, 'Not a position with goals. Try inside a proof.')}"
    )


def diagnostics(ws: Workspace, file_path: str) -> str:
    """Check a whole file and report its errors and warnings."""
    client, rel_path, _ = ws.open(file_path)
    reported = client.check_file(rel_path)
    found = format_diagnostics(reported, content=client.get_file_content(rel_path))
    if not found:
        return "No diagnostics; the file checks cleanly."
    return f"{_count_diagnostics(reported)} in {rel_path}:\n\n" + "\n\n".join(found)


def _count_diagnostics(diagnostics: List[dict]) -> str:
    """A heading for a list of diagnostics, e.g. "3 diagnostics (2 errors, 1 warning)"."""
    counts = Counter(
        SEVERITY.get(diagnostic.get("severity"), "info") for diagnostic in diagnostics
    )
    total = sum(counts.values())
    breakdown = ", ".join(
        f"{n} {label}{'s' if n > 1 else ''}" for label, n in counts.most_common()
    )
    if len(counts) < 2:
        return breakdown
    return f"{total} diagnostics ({breakdown})"


def outline(ws: Workspace, file_path: str) -> str:
    """Imports and declarations of a file, with line numbers."""
    client, rel_path, _ = ws.open(file_path)
    content = client.get_file_content(rel_path)
    requires = [
        f"L{i + 1}: {line.strip()}"
        for i, line in enumerate(content.split("\n"))
        if line.strip().startswith(("Require", "From ", "Import ", "Export "))
    ]
    symbols = client.document_symbols(rel_path)

    # vsrocqtop's SymbolKind values, from VsRocq's to_document_symbol.
    tags = {12: "Thm", 13: "Def", 23: "Ind", 5: "Mod", 2: "Mod", 21: "Other"}

    def render(nodes, depth=0) -> List[str]:
        out = []
        for node in nodes:
            rng = node.get("range", {})
            start = rng.get("start", {}).get("line", 0) + 1
            end = rng.get("end", {}).get("line", 0) + 1
            where = f"L{start}" if start == end else f"L{start}-{end}"
            out.append(
                f"{'  ' * depth}[{tags.get(node.get('kind'), 'Other')} {where}] "
                f"{node.get('name')}"
            )
            out.extend(render(node.get("children") or [], depth + 1))
        return out

    parts = [f"# {rel_path}"]
    if requires:
        parts.append("## Imports\n" + "\n".join(requires))
    declarations = render(symbols)
    parts.append(
        "## Declarations\n" + ("\n".join(declarations) if declarations else "(none found)")
    )
    return "\n\n".join(parts)


def suggest(
    ws: Workspace, file_path: str, line: int, column: Optional[int] = None,
    max_results: int = 40,
) -> str:
    """Lemmas applicable to the goal at a line, ranked by the prover.

    With no column, uses the goal at the start of the line, which is the
    one you still have to prove there.
    """
    client, rel_path, _ = ws.open(file_path)
    if column is None:
        # The goal you must discharge *at* this line, not the one after it.
        # Interpreting to the end of the line would step past the tactic
        # there, and past a closing `Qed.`/`Admitted.` there would be no
        # goal left at all.
        lines = client.get_file_content(rel_path).split("\n")
        if line < 1 or line > len(lines):
            return f"Line {line} is out of range; the file has {len(lines)} lines."
        text = lines[line - 1]
        col = len(text) - len(text.lstrip())
    else:
        col = column - 1
    client.goals_at(rel_path, line - 1, col)
    items = client.completions(rel_path, line - 1, col)

    labels = [item.get("label") for item in items if item.get("label")]
    if not labels:
        return (
            "No suggestions here. The engine needs an open goal, so use a line "
            "inside a proof, after a tactic."
        )
    shown = labels[:max_results]
    text = "\n".join(shown)
    if len(labels) > len(shown):
        text += f"\n... and {len(labels) - len(shown)} more, ranked lower"
    return f"Lemmas applicable to the goal at line {line} ({len(labels)} found):\n{text}"


def query(ws: Workspace, file_path: str, line: int, command: str, pattern: str) -> str:
    """`Check`, `Print`, `About` or `Locate` against the state at a line."""
    if command.strip().lower() not in ("check", "print", "about", "locate"):
        return f"Unsupported command `{command}`. Use Check, Print, About or Locate."
    client, rel_path, _ = ws.open(file_path)
    client.goals_at(rel_path, line - 1, None)
    answer = client.query(command.strip().lower(), rel_path, line - 1, None, pattern)
    return answer.strip() or f"{command} {pattern} produced no output."


def search(
    ws: Workspace, file_path: str, query_text: str, line: Optional[int] = None,
    max_results: int = 30,
) -> str:
    """Rocq's `Search` over everything loaded at a line."""
    client, rel_path, _ = ws.open(file_path)
    target = (line - 1) if line else len(client.get_file_content(rel_path).split("\n")) - 1
    client.goals_at(rel_path, target, None)
    try:
        results = client.search(rel_path, query_text, target, None)
    except RocqLSPError as exc:
        if "not found in the current environment" in str(exc):
            return (
                f"`Search {query_text}` failed: {query_text} is not a known constant.\n"
                f'To search by name, quote it: \'"{query_text}"\' (mind the shell, the '
                "quotes must reach Rocq). To search by shape, give a pattern such as "
                "'(_ ++ nil = _)'."
            )
        raise

    if not results:
        return (
            f"`Search {query_text}` found nothing. A bare name matches references to "
            'it; quote it ("app_nil") to match names, or give a pattern '
            "((_ ++ nil = _))."
        )
    lines = [
        f"{item['name']}\n    {item['statement']}" for item in results[:max_results]
    ]
    more = len(results) - len(lines)
    text = "\n".join(lines)
    if more > 0:
        text += f"\n... and {more} more"
    return text


def hover(ws: Workspace, file_path: str, line: int, column: int) -> str:
    """Type and full name of the symbol at a position."""
    client, rel_path, _ = ws.open(file_path)
    content = client.get_file_content(rel_path)
    # Hover reads the executed state, so the sentence holding the cursor must
    # have run; interpreting to the exact column stops just before it.
    client.goals_at(rel_path, line - 1, None)
    info = client.hover(rel_path, line - 1, column - 1)

    if not info:
        return (
            f"No hover information at:\n{format_line(content, line, column)}\n"
            "Point at the start of a symbol."
        )
    contents = info.get("contents") or {}
    text = contents.get("value") if isinstance(contents, dict) else str(contents)
    text = (text or "").replace("```rocq\n", "").replace("```coq\n", "").replace("```", "").strip()

    message = f"At:\n{format_line(content, line, column)}\n\n{text}"
    here = filter_diagnostics_by_position(
        client.get_diagnostics(rel_path), line - 1, column - 1
    )
    if here:
        message += "\n\nDiagnostics here:\n" + "\n".join(
            format_diagnostics(here, content=content)
        )
    return message


def declaration(ws: Workspace, file_path: str, line: int, column: int) -> str:
    """Source file declaring the symbol at a position."""
    client, rel_path, _ = ws.open(file_path)
    client.goals_at(rel_path, line - 1, None)
    location = client.definition(rel_path, line - 1, column - 1)

    if not location:
        return (
            "No declaration found. vsrocqtop resolves only symbols from compiled "
            "libraries with installed sources; try `query ... Locate` instead."
        )
    entries = location if isinstance(location, list) else [location]
    found = []
    for entry in entries:
        uri = entry.get("uri") or entry.get("targetUri") or ""
        rng = entry.get("range") or entry.get("targetRange") or {}
        start = rng.get("start", {}).get("line")
        path = uri[len("file://"):] if uri.startswith("file://") else uri
        found.append(path + (f":{start + 1}" if start is not None else ""))
    return "Declared in:\n" + "\n".join(found)


def try_snippets(ws: Workspace, file_path: str, line: int, snippets: List[str]) -> str:
    """Try one-line tactics at a line and compare; the file is never modified."""
    client, rel_path, _ = ws.open(file_path)
    original = client.get_file_content(rel_path)
    lines = original.split("\n")
    if line < 1 or line > len(lines):
        return f"Line {line} is out of range; the file has {len(lines)} lines."

    results = []
    try:
        for snippet in snippets:
            text = snippet.rstrip("\n")
            attempt = lines.copy()
            attempt[line - 1] = text
            client.update_file(rel_path, "\n".join(attempt))
            try:
                goals = format_proof_view(
                    client.goals_at(rel_path, line - 1, None), "No goals."
                )
            except RocqLSPError as exc:
                goals = f"(no goal: {exc})"
            here = format_diagnostics(
                client.get_diagnostics(rel_path),
                select_line=line - 1,
                content="\n".join(attempt),
            )
            reported = "\n".join(here) if here else "no diagnostics"
            results.append(f"=== {text}\n{reported}\n{goals}")
    finally:
        client.update_file(rel_path, original)
    return "\n\n".join(results)


def run_code(ws: Workspace, code: str, project_path: str = "") -> str:
    """Check a self-contained snippet inside a project's load path."""
    root = ws.resolve_project(project_path)
    if root is None:
        return "No project known yet. Pass --project, or run a file command first."

    rel_path = f"_rocq_lsp_snippet_{uuid.uuid4().hex}.v"
    abs_path = root / rel_path
    abs_path.write_text(code, encoding="utf-8")
    try:
        client = ws.client_for_project(root)
        client.open_file(rel_path)
        try:
            found = format_diagnostics(client.check_file(rel_path), content=code)
        finally:
            client.close_file(rel_path)
    finally:
        try:
            abs_path.unlink()
        except OSError:
            pass
    return "\n\n".join(found) if found else "The snippet checks cleanly."


def find(
    ws: Workspace, name: str, limit: int = 20, project_path: str = "",
    fallback_root: str = "",
) -> str:
    """Confirm a declaration exists, by name, in project and library sources.

    `fallback_root` is where the caller ran the command, used when no
    project is known yet. The daemon's own working directory is not a
    sensible default, since it is wherever it happened to be started.
    """
    if not _RG_AVAILABLE:
        return _RG_MESSAGE
    root = ws.resolve_project(project_path) or (
        Path(fallback_root) if fallback_root else Path.cwd()
    )
    if not root.exists():
        return f"`{root}` does not exist."

    matches = _ripgrep_declarations(query=name.strip(), limit=limit, project_root=root)
    if not matches:
        return f"No declaration matching `{name}` found under {root}."
    return "\n".join(
        f"{m['kind']:<12} {m['name']:<40} {m['file']}:{m['line']}" for m in matches
    )


def contents(ws: Workspace, file_path: str, annotate: bool = True) -> str:
    """File text, with line numbers by default."""
    path = Path(file_path).expanduser()
    if not path.exists():
        return f"File `{file_path}` does not exist."
    text = get_file_contents(path)
    return annotate_lines(text) if annotate else text


def session(ws: Workspace, project_path: str = "") -> str:
    """Report the session, optionally pointing it at a project first."""
    if project_path:
        target = Path(project_path).expanduser().resolve()
        if not target.exists():
            return f"`{project_path}` does not exist."
        root = ws.project_for(str(target)) if target.is_file() else target
        ws.last_project = root
        return f"Session ready.\nproject: {root}\n\n{status(ws)}"
    return f"Session ready.\n\n{status(ws)}"


def status(ws: Workspace) -> str:
    """Live provers, their open documents and their memory."""
    if not ws.clients:
        return "No provers running. They start on the first file command."
    parts = []
    for project, client in sorted(ws.clients.items()):
        project_file = find_project_file(project)
        memory = client.memory_bytes()
        parts.append(
            f"{project}\n"
            f"  project file: {project_file if project_file else 'none (no load paths)'}\n"
            f"  memory: {memory // (1024 * 1024) if memory else '?'} MB\n"
            f"  open: {', '.join(client.open_files()) or 'none'}"
        )
    return "\n".join(parts)


def close(ws: Workspace, file_path: str) -> str:
    """Drop a document's prover state to free memory."""
    client, rel_path, _ = ws.open(file_path)
    client.close_file(rel_path)
    return f"Closed `{rel_path}`. It is checked from scratch when next used."


def build(ws: Workspace, target: str = "", project_path: str = "", jobs: int = 4) -> str:
    """`make` the project, then drop its prover so it reloads the new `.vo` files."""
    root = ws.resolve_project(project_path)
    if root is None:
        return "No project known yet. Pass --project, or run a file command first."
    if not (root / "Makefile").is_file():
        return f"No Makefile in {root}. Build the project yourself, then re-run."

    ws.drop_project(root)
    command = ["make", f"-j{max(1, jobs)}"] + ([target] if target else [])
    done = subprocess.run(command, cwd=str(root), capture_output=True, text=True)
    output = (done.stdout + done.stderr).strip()
    if len(output) > 8000:
        output = output[:4000] + "\n...\n" + output[-4000:]
    state = "succeeded" if done.returncode == 0 else f"failed ({done.returncode})"
    return f"`{' '.join(command)}` {state}:\n{output}"
