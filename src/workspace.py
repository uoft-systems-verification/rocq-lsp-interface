"""The set of live provers, one per Rocq project."""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from rocq_lsp_mcp.file_utils import (
    find_project_root,
    get_relative_file_path,
)
from rocq_lsp_mcp.rocq_client import RocqLSPClient, RocqLSPError


class Workspace:
    """Keeps one `vsrocqtop` per project and maps files into them.

    Provers are expensive to start and hold the checked state of every
    document they have open, so they are kept alive between commands. That
    is the whole point of running a daemon rather than a process per call.
    """

    def __init__(
        self, rocq_args: Optional[List[str]] = None,
        cancel_event: Optional[threading.Event] = None,
    ) -> None:
        self.cancel_event = cancel_event or threading.Event()
        self.rocq_args = list(
            rocq_args
            if rocq_args is not None
            else [a for a in os.environ.get("ROCQ_ARGS", "").split(" ") if a]
        )
        self.clients: Dict[Path, RocqLSPClient] = {}
        self.last_project: Optional[Path] = None

        configured = os.environ.get("ROCQ_PROJECT_PATH", "").strip()
        if configured:
            self.last_project = Path(configured).expanduser().resolve()

    # --- projects -----------------------------------------------------------

    def project_for(self, file_path: str) -> Optional[Path]:
        """The project root governing a file: the directory holding its
        `_RocqProject`/`_CoqProject`, or its own directory if there is none.
        """
        abs_path = Path(file_path).expanduser().resolve()
        root = find_project_root(abs_path)
        if root is None:
            if not abs_path.exists():
                return None
            root = abs_path.parent
        return root.resolve()

    def client_for_project(self, project: Path) -> RocqLSPClient:
        if self.cancel_event.is_set():
            raise RocqLSPError("Session stopped; the operation was cancelled.")
        client = self.clients.get(project)
        if client is not None and client.is_alive():
            return client
        if client is not None:
            client.close()
        client = RocqLSPClient(
            project, rocq_args=self.rocq_args, cancel_event=self.cancel_event,
        )
        self.clients[project] = client
        self.last_project = project
        return client

    def open(self, file_path: str) -> Tuple[RocqLSPClient, str, Path]:
        """Resolve a file to its prover, with the document open.

        Raises RocqLSPError with an actionable message if the file cannot be
        placed in a project.
        """
        if not Path(file_path).expanduser().exists():
            raise RocqLSPError(
                f"`{file_path}` does not exist. Give a path to a .v file."
            )
        project = self.project_for(file_path)
        if project is None:
            raise RocqLSPError(f"Could not place `{file_path}` in a project.")
        rel_path = get_relative_file_path(project, file_path)
        if rel_path is None:
            raise RocqLSPError(
                f"`{file_path}` is not inside the project at {project}."
            )
        client = self.client_for_project(project)
        client.open_file(rel_path)
        self.last_project = project
        return client, rel_path, project

    def resolve_project(self, project_path: str = "") -> Optional[Path]:
        """The project to act on: the one given, else the last one used."""
        if project_path:
            return Path(project_path).expanduser().resolve()
        return self.last_project

    # --- lifecycle ----------------------------------------------------------

    def close_all(self) -> None:
        for client in self.clients.values():
            try:
                client.close()
            except Exception:
                pass
        self.clients.clear()

    def drop_project(self, project: Path) -> None:
        client = self.clients.pop(project, None)
        if client is not None:
            client.close()
