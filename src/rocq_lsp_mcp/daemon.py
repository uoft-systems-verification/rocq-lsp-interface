"""A small daemon holding the provers, so CLI commands stay cheap.

Starting a prover and checking a file costs the same as compiling it. A
one-shot command would pay that every time, which would defeat the point,
so the provers live here and commands talk to them over a unix socket.
"""

from __future__ import annotations

import json
import os
import queue
import signal
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict

from rocq_lsp_mcp import tools
from rocq_lsp_mcp.rocq_client import RocqLSPError
from rocq_lsp_mcp.workspace import Workspace

# Proof requests run in one worker because a prover is not safe to drive
# concurrently. The listener stays responsive to shutdown during a check.
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

    stopping = threading.Event()
    workspace = Workspace(cancel_event=stopping)
    pending: queue.Queue = queue.Queue()
    shutdown_connection = None
    last_activity = time.monotonic()

    def stop(signum, frame):
        stopping.set()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    def work():
        nonlocal last_activity
        while True:
            item = pending.get()
            try:
                if item is None:
                    return
                conn, request = item
                with conn:
                    response = _handle(workspace, request) if not stopping.is_set() else {}
                    if stopping.is_set():
                        response = {"ok": False, "text": "Session stopped; the operation was cancelled."}
                    _write_response(conn, response)
            finally:
                last_activity = time.monotonic()
                pending.task_done()

    worker = threading.Thread(target=work, name="rocq-lsp-requests", daemon=True)
    worker.start()
    print(f"rocq-lsp daemon listening on {path}", flush=True)

    try:
        while not stopping.is_set():
            try:
                conn, _ = server.accept()
            except socket.timeout:
                if (idle_timeout and not pending.unfinished_tasks
                        and time.monotonic() - last_activity > idle_timeout):
                    print("idle; shutting down", flush=True)
                    break
                continue
            except OSError:
                break

            request = _read_request(conn)
            if request is None:
                conn.close()
                continue
            if request.get("tool") == "__shutdown__":
                shutdown_connection = conn
                stopping.set()
                break
            pending.put((conn, request))
    finally:
        stopping.set()
        pending.put(None)
        # Cancellation wakes a worker waiting on prover output. Unwind its
        # request before touching the workspace and closing its processes.
        worker.join()
        workspace.close_all()
        server.close()
        try:
            path.unlink()
        except OSError:
            pass
        if shutdown_connection is not None:
            with shutdown_connection:
                _write_response(shutdown_connection, {"ok": True, "text": "Session stopped."})
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
