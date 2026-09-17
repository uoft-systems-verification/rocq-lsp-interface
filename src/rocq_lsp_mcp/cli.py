"""`rocq-lsp`: Rocq prover tools for a coding agent, on the command line.

Every command talks to a background daemon that keeps the prover warm, so
only the first check of a file costs a full compile. The daemon starts on
demand; nothing needs setting up first.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from rocq_lsp_mcp.daemon import log_path, socket_path

CONNECT_TIMEOUT = 60.0


# --- talking to the daemon --------------------------------------------------


def _connect(path: Path) -> Optional[socket.socket]:
    try:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.connect(str(path))
        return client
    except OSError:
        return None


def _spawn_daemon() -> subprocess.Popen:
    """Start the daemon detached, so it outlives this command."""
    log = open(log_path(), "a", buffering=1)
    return subprocess.Popen(
        [sys.executable, "-m", "rocq_lsp_mcp.daemon"],
        stdout=log,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
        env=os.environ.copy(),
    )


NO_SESSION = "No session running. Start one with `rocq-lsp start`."


def call(tool: str, args: Dict[str, Any], autostart: bool = False) -> Tuple[bool, str]:
    """Send one request to the session.

    Only `start` and `restart` may bring a session up; every other command
    reports that none is running, so starting and stopping stay separate
    from working.
    """
    path = socket_path()
    client = _connect(path)

    if client is None:
        if not autostart:
            return False, NO_SESSION
        child = _spawn_daemon()
        deadline = time.monotonic() + 30
        while client is None and time.monotonic() < deadline:
            if child.poll() is not None:
                # It died on startup; the log says why, so show it rather
                # than waiting out the timeout.
                break
            time.sleep(0.1)
            client = _connect(path)
        if client is None:
            reason = ""
            try:
                reason = "\n" + log_path().read_text().strip().splitlines()[-1]
            except (OSError, IndexError):
                pass
            return False, f"Could not start the daemon (see {log_path()}):{reason}"

    with client:
        client.sendall(json.dumps({"tool": tool, "args": args}).encode() + b"\n")
        client.settimeout(CONNECT_TIMEOUT)
        chunks = []
        try:
            while b"\n" not in b"".join(chunks):
                chunk = client.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
        except socket.timeout:
            return False, (
                "The prover is still working. Checking a large file can take "
                "minutes; try again shortly, or raise ROCQ_LSP_TIMEOUT."
            )

    raw = b"".join(chunks).strip()
    if not raw:
        return False, "The daemon closed the connection without answering."
    try:
        response = json.loads(raw)
    except ValueError:
        return False, f"Malformed response: {raw[:200]!r}"
    return bool(response.get("ok")), response.get("text", "")


# --- argument helpers -------------------------------------------------------


def parse_location(text: str, want_column: bool = False) -> Dict[str, Any]:
    """Split `FILE:LINE` or `FILE:LINE:COLUMN`, which is how positions are given."""
    parts = text.rsplit(":", 2)
    if len(parts) >= 2 and parts[-1].isdigit() and parts[-2].isdigit():
        file_path, line, column = parts[0], int(parts[-2]), int(parts[-1])
    elif len(parts) >= 2 and parts[-1].isdigit():
        file_path, line, column = ":".join(parts[:-1]), int(parts[-1]), None
    else:
        raise argparse.ArgumentTypeError(
            f"`{text}` is not a location. Use FILE:LINE"
            + (" or FILE:LINE:COLUMN." if want_column else ".")
        )
    if want_column and column is None:
        raise argparse.ArgumentTypeError(f"`{text}` needs a column: FILE:LINE:COLUMN.")
    return {"file_path": str(Path(file_path).expanduser().resolve()), "line": line,
            "column": column}


def _abs(path: str) -> str:
    return str(Path(path).expanduser().resolve())


# --- commands ---------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rocq-lsp",
        description=(
            "Rocq prover tools. Positions are 1-indexed.\n\n"
            "Session commands (start, stop, restart) manage the background "
            "prover. Everything else works inside a running session and never "
            "starts or stops one, so run `rocq-lsp start` first."
        ),
        epilog=(
            "session:  start, stop, restart, status\n"
            "reading:  goal, diagnostics, outline, contents\n"
            "finding:  suggest, search, find, query, hover, declaration\n"
            "trying:   try, run-code\n"
            "project:  build, close"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    p = sub.add_parser("goal", help="proof state at a line (the main tool)")
    p.add_argument("location", help="FILE:LINE, or FILE:LINE:COLUMN")

    p = sub.add_parser("diagnostics", help="check a whole file")
    p.add_argument("file")

    p = sub.add_parser("outline", help="imports and declarations with line numbers")
    p.add_argument("file")

    p = sub.add_parser("suggest", help="lemmas that apply to the goal at a line")
    p.add_argument("location", help="FILE:LINE, or FILE:LINE:COLUMN")
    p.add_argument("--max", type=int, default=40, dest="max_results")

    p = sub.add_parser(
        "query", help="Check, Print, About or Locate at a line",
    )
    p.add_argument("location", help="FILE:LINE")
    p.add_argument("rocq_command", metavar="COMMAND",
                   choices=["Check", "Print", "About", "Locate",
                            "check", "print", "about", "locate"])
    p.add_argument("pattern")

    p = sub.add_parser(
        "search",
        help="Rocq Search; quote a name to match names, or give a pattern",
    )
    p.add_argument("file")
    p.add_argument("query", help='e.g. "app_nil" or (_ ++ nil = _)')
    p.add_argument("--line", type=int, default=None)
    p.add_argument("--max", type=int, default=30, dest="max_results")

    p = sub.add_parser("hover", help="type and full name of a symbol")
    p.add_argument("location", help="FILE:LINE:COLUMN")

    p = sub.add_parser("declaration", help="source file declaring a symbol")
    p.add_argument("location", help="FILE:LINE:COLUMN")

    p = sub.add_parser("try", help="try one-line tactics at a line and compare")
    p.add_argument("location", help="FILE:LINE")
    p.add_argument("snippets", nargs="+", help="one-line tactics, indented as written")

    p = sub.add_parser("run-code", help="check a self-contained snippet")
    p.add_argument("--code", default=None, help="the source; otherwise read stdin")
    p.add_argument("--project", default="", help="project root to check inside")

    p = sub.add_parser("find", help="confirm a declaration exists, by name")
    p.add_argument("name")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--project", default="")

    p = sub.add_parser("contents", help="file text with line numbers")
    p.add_argument("file")
    p.add_argument("--raw", action="store_true", help="omit line numbers")

    p = sub.add_parser("build", help="make the project, then reload the prover")
    p.add_argument("target", nargs="?", default="")
    p.add_argument("--project", default="")
    p.add_argument("--jobs", type=int, default=4)

    sub.add_parser("status", help="live provers, open files and memory")

    p = sub.add_parser("close", help="drop a file's prover state to free memory")
    p.add_argument("file")

    p = sub.add_parser(
        "start",
        help="start the session (provers stay warm until you stop them)",
    )
    p.add_argument(
        "path", nargs="?", default="",
        help="optional .v file or project directory to work in",
    )

    sub.add_parser("stop", help="end the session and free all prover memory")

    p = sub.add_parser("restart", help="end the session and start a fresh one")
    p.add_argument("path", nargs="?", default="")
    return parser


def to_request(args: argparse.Namespace) -> Tuple[str, Dict[str, Any]]:
    """Turn parsed arguments into a daemon request."""
    command = args.command

    if command in ("goal", "suggest"):
        request = parse_location(args.location)
        if command == "suggest":
            request["max_results"] = args.max_results
        return command, request

    if command in ("hover", "declaration"):
        return command, parse_location(args.location, want_column=True)

    if command == "query":
        located = parse_location(args.location)
        return "query", {
            "file_path": located["file_path"],
            "line": located["line"],
            "command": args.rocq_command,
            "pattern": args.pattern,
        }

    if command == "try":
        located = parse_location(args.location)
        return "try", {
            "file_path": located["file_path"],
            "line": located["line"],
            "snippets": args.snippets,
        }

    if command == "search":
        return "search", {
            "file_path": _abs(args.file),
            "query_text": args.query,
            "line": args.line,
            "max_results": args.max_results,
        }

    if command in ("diagnostics", "outline", "close"):
        return command, {"file_path": _abs(args.file)}

    if command == "contents":
        return "contents", {"file_path": _abs(args.file), "annotate": not args.raw}

    if command == "run-code":
        code = args.code if args.code is not None else sys.stdin.read()
        return "run-code", {"code": code, "project_path": args.project}

    if command == "find":
        return "find", {
            "name": args.name,
            "limit": args.limit,
            "project_path": args.project,
            # Where the user is, not where the daemon happens to be.
            "fallback_root": os.getcwd(),
        }

    if command == "build":
        return "build", {
            "target": args.target, "project_path": args.project, "jobs": args.jobs,
        }

    if command == "status":
        return "status", {}

    raise ValueError(f"unhandled command {command}")


def handle_start(project_path: str) -> int:
    """Start the session, or report the running one. Safe to repeat."""
    already = _connect(socket_path())
    if already is not None:
        already.close()
    ok, text = call("session", {"project_path": project_path}, autostart=True)
    if not ok:
        print(text)
        return 1
    print(f"{'Session already running.' if already else 'Session started.'}\n{text}")
    return 0


def handle_stop() -> int:
    """End the session. Succeeds whether or not one was running."""
    ok, text = call("__shutdown__", {})
    print(text if ok else "No session running.")
    return 0


def handle_restart(project_path: str) -> int:
    handle_stop()
    # Let the socket disappear before starting again.
    deadline = time.monotonic() + 10
    while socket_path().exists() and time.monotonic() < deadline:
        time.sleep(0.1)
    return handle_start(project_path)


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "start":
        return handle_start(args.path)
    if args.command == "stop":
        return handle_stop()
    if args.command == "restart":
        return handle_restart(args.path)

    try:
        tool, request = to_request(args)
    except argparse.ArgumentTypeError as exc:
        parser.error(str(exc))
        return 2

    ok, text = call(tool, request)
    print(text)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
