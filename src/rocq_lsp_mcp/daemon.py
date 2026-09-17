"""A small daemon holding the provers, so CLI commands stay cheap.

Starting a prover and checking a file costs the same as compiling it. A
one-shot command would pay that every time, which would defeat the point,
so the provers live here and commands talk to them over a unix socket.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import sys
import time
from pathlib import Path
from typing import Any, Dict

from rocq_lsp_mcp import tools
from rocq_lsp_mcp.rocq_client import RocqLSPError
from rocq_lsp_mcp.workspace import Workspace

# Requests are handled one at a time. An agent issues commands in sequence,
# and a prover is not safe to drive concurrently.
TOOLS = {
    "goal": tools.goal,
    "diagnostics": tools.diagnostics,
    "outline": tools.outline,
    "suggest": tools.suggest,
    "query": tools.query,
    "search": tools.search,
    "hover": tools.hover,
    "declaration": tools.declaration,
    "try": tools.try_snippets,
    "run-code": tools.run_code,
    "find": tools.find,
    "contents": tools.contents,
    "status": tools.status,
    "session": tools.session,
    "close": tools.close,
    "build": tools.build,
}

DEFAULT_IDLE_TIMEOUT = float(os.environ.get("ROCQ_LSP_IDLE_TIMEOUT", "3600"))


# A unix socket path must fit in sockaddr_un, about 104 bytes on macOS and
# 108 on Linux. A deep XDG_RUNTIME_DIR can overshoot it.
MAX_SOCKET_PATH = 100


def socket_path() -> Path:
    override = os.environ.get("ROCQ_LSP_SOCKET")
    if override:
        return Path(override)
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime and Path(runtime).is_dir():
        candidate = Path(runtime) / "rocq-lsp.sock"
        if len(str(candidate)) <= MAX_SOCKET_PATH:
            return candidate
    return Path(f"/tmp/rocq-lsp-{os.getuid()}.sock")


def log_path() -> Path:
    return socket_path().with_suffix(".log")


def serve(idle_timeout: float = DEFAULT_IDLE_TIMEOUT) -> int:
    """Run the daemon until asked to stop or left idle."""
    path = socket_path()
    if len(str(path)) > MAX_SOCKET_PATH:
        print(
            f"Socket path is too long for a unix socket ({len(str(path))} bytes):\n"
            f"  {path}\n"
            "Set ROCQ_LSP_SOCKET to something shorter, such as /tmp/rocq-lsp.sock.",
            file=sys.stderr,
        )
        return 1
    if path.exists():
        # A socket with nobody listening is left over from a crash.
        try:
            probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            probe.connect(str(path))
            probe.close()
            print(f"A daemon is already listening on {path}", file=sys.stderr)
            return 1
        except OSError:
            path.unlink()

    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(path))
    server.listen(16)
    server.settimeout(5.0)

    workspace = Workspace()
    stopping = {"now": False}

    def stop(signum, frame):
        stopping["now"] = True

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    print(f"rocq-lsp daemon listening on {path}", flush=True)
    last_activity = time.monotonic()

    try:
        while not stopping["now"]:
            try:
                conn, _ = server.accept()
            except socket.timeout:
                if idle_timeout and time.monotonic() - last_activity > idle_timeout:
                    print("idle; shutting down", flush=True)
                    break
                continue
            except OSError:
                break

            with conn:
                request = _read_request(conn)
                if request is None:
                    continue
                if request.get("tool") == "__shutdown__":
                    _write_response(conn, {"ok": True, "text": "Session stopped."})
                    stopping["now"] = True
                    break
                response = _handle(workspace, request)
                _write_response(conn, response)
            last_activity = time.monotonic()
    finally:
        workspace.close_all()
        try:
            path.unlink()
        except OSError:
            pass
        print("daemon stopped", flush=True)
    return 0


def _read_request(conn: socket.socket) -> Dict[str, Any] | None:
    chunks = []
    conn.settimeout(30.0)
    try:
        while b"\n" not in b"".join(chunks):
            chunk = conn.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
    except (socket.timeout, OSError):
        return None
    raw = b"".join(chunks).strip()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


def _write_response(conn: socket.socket, response: Dict[str, Any]) -> None:
    try:
        conn.sendall(json.dumps(response).encode("utf-8") + b"\n")
    except OSError:
        pass


def _handle(workspace: Workspace, request: Dict[str, Any]) -> Dict[str, Any]:
    name = request.get("tool", "")
    handler = TOOLS.get(name)
    if handler is None:
        return {"ok": False, "text": f"Unknown command `{name}`."}
    try:
        return {"ok": True, "text": handler(workspace, **(request.get("args") or {}))}
    except RocqLSPError as exc:
        return {"ok": False, "text": str(exc)}
    except TypeError as exc:
        return {"ok": False, "text": f"Bad arguments for `{name}`: {exc}"}
    except Exception as exc:  # a tool failing must not take the daemon down
        return {"ok": False, "text": f"{type(exc).__name__}: {exc}"}


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="rocq-lsp-daemon")
    parser.add_argument(
        "--idle-timeout",
        type=float,
        default=DEFAULT_IDLE_TIMEOUT,
        help="Exit after this many idle seconds, freeing prover memory. 0 disables.",
    )
    args = parser.parse_args()
    return serve(args.idle_timeout)


if __name__ == "__main__":
    sys.exit(main())
