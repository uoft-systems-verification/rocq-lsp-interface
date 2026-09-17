"""Formatting helpers shared by the tools."""

from __future__ import annotations

from typing import Dict, List, Optional

from rocq_lsp_mcp.rocq_client import render_pp, utf16_to_index

SEVERITY = {1: "error", 2: "warning", 3: "info", 4: "hint"}


def format_diagnostics(
    diagnostics: List[Dict],
    select_line: int = -1,
    content: Optional[str] = None,
) -> List[str]:
    """Render diagnostics with an explicit position, optionally only those
    touching a line.

    `select_line` is 0-indexed, matching the protocol. `content` is the text
    that was checked; when given, the offending source line is shown with a
    caret marker and columns are counted in characters. Without it columns
    are the protocol's own UTF-16 units. The two agree on the notation Rocq
    developments normally use, since `forall` as a symbol and friends live in
    the BMP; they diverge only past it, on the likes of blackboard bold.
    """
    if select_line != -1:
        diagnostics = filter_diagnostics_by_position(diagnostics, select_line, None)

    lines = content.split("\n") if content is not None else None
    messages = []
    for diagnostic in diagnostics:
        severity = SEVERITY.get(diagnostic.get("severity"), "info")
        span = _diagnostic_span(diagnostic.get("range"), lines)
        parts = [f"{severity} {_describe_span(span)}:"]
        excerpt = _excerpt_span(span, lines)
        if excerpt:
            parts.append(excerpt)
        parts.append(diagnostic.get("message", "").strip())
        messages.append("\n".join(parts))
    return messages


def _diagnostic_span(rng: Optional[Dict], lines: Optional[List[str]]) -> Optional[Dict]:
    """A diagnostic range as 1-indexed, inclusive, character-counted bounds.

    Returns None when the server sent no usable range. `last_column` is None
    for a range that ends where a line ends, and equals `first_column - 1`
    for an empty range, which points between two characters.
    """
    if not rng:
        return None
    start, end = rng.get("start") or {}, rng.get("end") or {}
    if start.get("line") is None or end.get("line") is None:
        return None

    first_line, last_line = start["line"], end["line"]
    first_utf16 = start.get("character", 0)
    last_utf16 = end.get("character", first_utf16)

    # Protocol ranges are half-open, so the last character covered sits one
    # unit before the end. A range stopping at column 0 therefore covers
    # through the end of the line before it.
    first_column = _character_column(lines, first_line, first_utf16)
    if last_utf16 == 0 and last_line > first_line:
        last_line -= 1
        last_column = None
    elif last_line == first_line and last_utf16 <= first_utf16:
        last_column = first_column - 1
    else:
        last_column = _character_column(lines, last_line, last_utf16 - 1)

    return {
        "first_line": first_line + 1,
        "last_line": last_line + 1,
        "first_column": first_column,
        "last_column": last_column,
    }


def _character_column(
    lines: Optional[List[str]], line_index: int, utf16_column: int
) -> int:
    """1-indexed character column for a 0-indexed UTF-16 column on a line."""
    if lines is None or not 0 <= line_index < len(lines):
        return utf16_column + 1
    return utf16_to_index(lines[line_index], utf16_column) + 1


def _describe_span(span: Optional[Dict]) -> str:
    if span is None:
        return "at an unreported position"

    first_line, last_line = span["first_line"], span["last_line"]
    first_column, last_column = span["first_column"], span["last_column"]

    if first_line != last_line:
        where = f"from line {first_line}, column {first_column} to line {last_line}, "
        return where + ("end of line" if last_column is None else f"column {last_column}")
    if last_column is None:
        return f"at line {first_line}, column {first_column} to end of line"
    if last_column < first_column:
        return f"at line {first_line}, before column {first_column}"
    if last_column == first_column:
        return f"at line {first_line}, column {first_column}"
    return f"at line {first_line}, columns {first_column}-{last_column}"


def _excerpt_span(span: Optional[Dict], lines: Optional[List[str]]) -> Optional[str]:
    """The first line of a span, with carets under the part it covers."""
    if span is None or lines is None:
        return None
    line_index = span["first_line"] - 1
    if not 0 <= line_index < len(lines):
        return None

    text = lines[line_index].replace("\t", " ")
    start = min(span["first_column"], len(text) + 1) - 1
    if span["first_line"] != span["last_line"] or span["last_column"] is None:
        width = max(len(text) - start, 1)
    else:
        width = max(span["last_column"] - span["first_column"] + 1, 1)

    gutter = " " * len(str(span["first_line"]))
    marker = " " * start + "^" * width
    more = " ..." if span["last_line"] != span["first_line"] else ""
    return f"{span['first_line']} | {text}\n{gutter} | {marker}{more}"


def filter_diagnostics_by_position(
    diagnostics: List[Dict], line: Optional[int], column: Optional[int]
) -> List[Dict]:
    """Diagnostics whose range covers the given 0-indexed position."""
    if line is None:
        return list(diagnostics)

    matches = []
    for diagnostic in diagnostics:
        rng = diagnostic.get("range")
        if not rng:
            continue
        start, end = rng.get("start", {}), rng.get("end", {})
        if start.get("line") is None or end.get("line") is None:
            continue
        if line < start["line"] or line > end["line"]:
            continue
        if column is not None:
            if line == start["line"] and column < start.get("character", 0):
                continue
            if line == end["line"] and column >= end.get("character", column + 1):
                continue
        matches.append(diagnostic)
    return matches


def _format_hypothesis(hypothesis: Dict) -> str:
    ids = ", ".join(hypothesis.get("ids") or [])
    type_text = hypothesis.get("_type") or ""
    if isinstance(type_text, list):
        type_text = render_pp(type_text)
    body = hypothesis.get("body")
    if isinstance(body, list):
        body = render_pp(body)
    if body:
        return f"{ids} := {body} : {type_text}"
    return f"{ids} : {type_text}"


def _format_goal(goal: Dict, index: int, total: int) -> str:
    lines = []
    if total > 1:
        name = goal.get("name")
        lines.append(f"goal {index + 1} of {total}" + (f" ({name})" if name else ""))
    for hypothesis in goal.get("hypotheses") or []:
        lines.append(_format_hypothesis(hypothesis))
    statement = goal.get("goal")
    if isinstance(statement, list):
        statement = render_pp(statement)
    lines.append(f"⊢ {statement}")
    return "\n".join(lines)


def format_proof_view(view: Optional[Dict], default: str = "No goals here.") -> str:
    """Render a `prover/proofView` payload as text.

    Prefers the plain-string form the server sends in `String` goal mode,
    falling back to rendering the `Pp` form.
    """
    if not view:
        return default

    state = view.get("pp_proof") or view.get("proof")
    if not state:
        return "No goals. The proof is complete at this point."

    goals = state.get("goals") or []
    if not goals:
        parts = ["No goals. The proof is complete at this point."]
    else:
        parts = [_format_goal(goal, i, len(goals)) for i, goal in enumerate(goals)]

    shown_ids = {goal.get("id") for goal in goals}
    for label, key in (
        ("shelved", "shelvedGoals"),
        ("given up", "givenUpGoals"),
        ("unfocused", "unfocusedGoals"),
    ):
        # The server lists the focused goal under unfocusedGoals too; counting
        # it twice would suggest work that is not there.
        extra = [g for g in (state.get(key) or []) if g.get("id") not in shown_ids]
        if extra:
            parts.append(f"({len(extra)} {label} goal(s))")

    messages = view.get("pp_messages") or []
    for entry in messages:
        if isinstance(entry, list) and len(entry) == 2:
            severity, text = entry
            if isinstance(text, list):
                text = render_pp(text)
            parts.append(f"[{SEVERITY.get(severity, 'info')}] {text}")

    return "\n\n".join(parts)


def format_line(content: str, line: int, column: Optional[int] = None,
                cursor: str = "<cursor>") -> str:
    """Show a 1-indexed line, optionally marking a 1-indexed column."""
    lines = content.split("\n")
    index = line - 1
    if index < 0 or index >= len(lines):
        return "Line number out of range"
    text = lines[index]
    if column is None:
        return text
    at = utf16_to_index(text, column - 1)
    if at < 0 or at > len(text):
        return "Invalid column number"
    return f"{text[:at]}{cursor}{text[at:]}"


def annotate_lines(content: str) -> str:
    lines = content.split("\n")
    width = len(str(len(lines)))
    return "".join(f"{i + 1:>{width}}: {line}\n" for i, line in enumerate(lines))


def search_symbols(symbols: List[Dict], name: str) -> Optional[Dict]:
    for symbol in symbols:
        if symbol.get("name") == name:
            return symbol
        found = search_symbols(symbol.get("children") or [], name)
        if found:
            return found
    return None
