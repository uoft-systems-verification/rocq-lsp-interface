"""Local declaration search, so an agent can confirm a name exists."""

from __future__ import annotations

import platform
import re
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Dict, List, Optional

INSTALL_URL = "https://github.com/BurntSushi/ripgrep#installation"

_INSTRUCTIONS = {
    "Darwin": ("brew install ripgrep",),
    "Linux": ("sudo apt-get install ripgrep", "sudo dnf install ripgrep"),
    "Windows": ("winget install BurntSushi.ripgrep.MSVC", "choco install ripgrep"),
}

# The vernacular commands that introduce a name.
DECLARATION_KEYWORDS = (
    "Theorem", "Lemma", "Corollary", "Remark", "Fact", "Proposition",
    "Definition", "Fixpoint", "CoFixpoint", "Inductive", "CoInductive",
    "Record", "Structure", "Class", "Instance", "Variant", "Axiom",
    "Parameter", "Notation", "Ltac",
)


def check_ripgrep_status() -> tuple[bool, str]:
    if shutil.which("rg"):
        return True, ""
    hints = _INSTRUCTIONS.get(platform.system(), ("Check your package manager.",))
    lines = [
        "ripgrep (rg) was not found on PATH. rocq_local_search uses it for fast "
        "declaration search.",
        "",
        "Installation options:",
        *(f"  - {hint}" for hint in hints),
        f"More options: {INSTALL_URL}",
    ]
    return False, "\n".join(lines)


def rocq_local_search(
    query: str, limit: int = 32, project_root: Optional[Path] = None
) -> List[Dict[str, str]]:
    """Find Rocq declarations whose name contains `query`."""
    root = (project_root or Path.cwd()).resolve()
    keywords = "|".join(DECLARATION_KEYWORDS)
    pattern = rf"^\s*(?:{keywords})\s+(?:[A-Za-z0-9_'.]+\.)*\w*{re.escape(query)}\w*"

    command = [
        "rg", "--json", "--smart-case", "--no-messages", "--color", "never",
        "-g", "*.v", "-g", "!.git/**", pattern, str(root),
    ]
    command.extend(_library_paths())

    result = subprocess.run(command, capture_output=True, text=True, cwd=str(root))

    matches: List[Dict[str, str]] = []
    for line in result.stdout.splitlines():
        if not line:
            continue
        try:
            event = __import__("json").loads(line)
        except ValueError:
            continue
        if event.get("type") != "match":
            continue
        data = event["data"]
        parts = data["lines"]["text"].strip().split(maxsplit=2)
        if len(parts) < 2:
            continue
        kind, name = parts[0], parts[1].rstrip(":.")
        path = Path(data["path"]["text"])
        try:
            display = str(path.resolve().relative_to(root))
        except ValueError:
            display = str(path)
        matches.append(
            {"name": name, "kind": kind, "file": display,
             "line": str(data.get("line_number", ""))}
        )
        if len(matches) >= limit:
            break

    if result.returncode not in (0, 1) and not matches:
        message = f"ripgrep exited with code {result.returncode}"
        if result.stderr:
            message += f"\n{result.stderr}"
        raise RuntimeError(message)
    return matches


@lru_cache(maxsize=1)
def _library_paths() -> tuple[str, ...]:
    """Installed Rocq sources, so library names are searchable too.

    Rocq 9 split the standard library out of `theories` into its own
    package under `user-contrib`, which is also where other installed
    libraries live, so both directories have to be searched.
    """
    for args in (["rocq", "c", "-where"], ["coqc", "-where"]):
        try:
            done = subprocess.run(args, capture_output=True, text=True)
        except (FileNotFoundError, OSError):
            continue
        prefix = done.stdout.strip()
        if not prefix:
            continue
        found = [
            str(Path(prefix) / name)
            for name in ("theories", "user-contrib")
            if (Path(prefix) / name).is_dir()
        ]
        if found:
            return tuple(found)
    return ()
