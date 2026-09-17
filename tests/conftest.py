from __future__ import annotations

import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import AsyncContextManager

import pytest

from tests.helpers.mcp_client import MCPClient, connect_stdio_client
from tests.helpers.rocq_project import ensure_test_project


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


@pytest.fixture(scope="session")
def test_project_path(repo_root: Path) -> Path:
    try:
        return ensure_test_project(repo_root)
    except RuntimeError as exc:
        pytest.skip(str(exc))


@pytest.fixture
def mcp_client_factory(
    repo_root: Path, test_project_path: Path
) -> Callable[[], AsyncContextManager[MCPClient]]:
    pythonpath = [str(repo_root / "src")]
    if existing := os.environ.get("PYTHONPATH"):
        pythonpath.append(existing)
    env = {
        "PYTHONPATH": os.pathsep.join(pythonpath),
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "ROCQ_LOG_LEVEL": "ERROR",
        "ROCQ_PROJECT_PATH": str(test_project_path),
    }

    def factory() -> AsyncContextManager[MCPClient]:
        return connect_stdio_client(
            sys.executable,
            ["-m", "rocq_lsp_mcp", "--transport", "stdio"],
            env=env,
            cwd=str(repo_root),
        )

    return factory
