"""Locating Rocq projects and files within them."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

PROJECT_FILES = ("_RocqProject", "_CoqProject")


def find_project_file(directory: Path) -> Optional[Path]:
    """The project file governing `directory`, searching upward.

    This mirrors how vsrocqtop itself looks for one, so the load path the
    server uses matches what we report.
    """
    for parent in [directory, *directory.parents]:
        for name in PROJECT_FILES:
            candidate = parent / name
            if candidate.is_file():
                return candidate
    return None


def find_project_root(file_path: Path) -> Optional[Path]:
    """The root of the Rocq project containing `file_path`."""
    directory = file_path.parent if file_path.is_file() else file_path
    project_file = find_project_file(directory.resolve())
    return project_file.parent if project_file else None


def valid_rocq_project_path(path: Path | str) -> bool:
    path_obj = Path(path)
    return any((path_obj / name).is_file() for name in PROJECT_FILES)


def get_relative_file_path(project_path: Path, file_path: str) -> Optional[str]:
    """Express `file_path` relative to the project root, or None if outside it."""
    candidates = []
    path_obj = Path(file_path)
    if path_obj.is_absolute():
        candidates.append(path_obj)
    else:
        candidates.append(project_path / file_path)
        candidates.append(Path.cwd() / file_path)

    for candidate in candidates:
        if not candidate.exists():
            continue
        try:
            return str(candidate.resolve().relative_to(project_path.resolve()))
        except ValueError:
            continue
    return None


def get_file_contents(abs_path: str | Path) -> str:
    for encoding in ("utf-8", "latin-1"):
        try:
            return Path(abs_path).read_text(encoding=encoding)
        except UnicodeDecodeError:
            continue
    return Path(abs_path).read_text(errors="replace")
