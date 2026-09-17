"""LSP client for `vsrocqtop`, the VsRocq language server.

There is no packaged Python client for Rocq the way `leanclient` exists for
Lean, so this module speaks the protocol directly. The behaviours encoded
here were established against vsrocq-language-server 2.4.3 and are easy to
get wrong:

* `initializationOptions` is decoded strictly. Every section must be
  present or the server drops the `initialize` request with no reply at
  all, leaving its settings at defaults.
* `textDocument/didChange` must carry range-based edits. A full-text
  change makes the server exit with status 0.
* `prover/interpretToEnd`, `prover/interpretToPoint` and the query
  requests require `textDocument.version`.
* In Manual mode the server queues `prover/proofView` at the lowest event
  priority, so it arrives only after everything an interpret command
  scheduled has run. That is the completion signal used here.
* Proof-body edits can leave stale executed states. If interpretation shows
  no activity covering an edit, replace the process and retry the command.
* A position outside the document makes the server exit, so positions are
  clamped.
* Positions are UTF-16 offsets, which matters for the Unicode notation
  common in Rocq developments.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

DEFAULT_TIMEOUT = float(os.environ.get("ROCQ_LSP_TIMEOUT", "300"))
# vsrocqtop answers quick requests promptly; only interpretation is slow.
QUICK_TIMEOUT = float(os.environ.get("ROCQ_LSP_QUICK_TIMEOUT", "30"))


class RocqLSPError(RuntimeError):
    """The server failed a request, died, or did not answer in time."""


class _Closed:
    """Sentinel marking the end of the server's output."""


_CLOSED = _Closed()


# --- position helpers -------------------------------------------------------


def utf16_len(text: str) -> int:
    """Length of `text` in UTF-16 code units, which is how LSP counts."""
    return len(text.encode("utf-16-le")) // 2


def utf16_to_index(line: str, utf16_col: int) -> int:
    """Python string index of a UTF-16 column within a single line."""
    if utf16_col <= 0:
        return 0
    units = 0
    for index, char in enumerate(line):
        if units >= utf16_col:
            return index
        units += 2 if ord(char) > 0xFFFF else 1
    return len(line)


def index_to_utf16(line: str, index: int) -> int:
    """UTF-16 column of a Python string index within a single line."""
    return utf16_len(line[:index])


def position_at(text: str, offset: int) -> Dict[str, int]:
    """LSP position of a Python string offset in `text`."""
    before = text[:offset]
    line = before.count("\n")
    column = index_to_utf16(before, len(before)) if line == 0 else utf16_len(
        before[before.rfind("\n") + 1 :]
    )
    return {"line": line, "character": column}


def _position_key(position: Dict[str, int]) -> Tuple[int, int]:
    return position["line"], position["character"]


def replacement_edit(old: str, new: str) -> Tuple[Dict[str, Any], str]:
    """The single edit turning `old` into `new`.

    Replaces the span between their common prefix and common suffix, which
    keeps the edit minimal so the server re-executes as little as possible.
    """
    prefix = 0
    limit = min(len(old), len(new))
    while prefix < limit and old[prefix] == new[prefix]:
        prefix += 1

    suffix = 0
    limit -= prefix
    while suffix < limit and old[len(old) - suffix - 1] == new[len(new) - suffix - 1]:
        suffix += 1

    rng = {
        "start": position_at(old, prefix),
        "end": position_at(old, len(old) - suffix),
    }
    return rng, new[prefix : len(new) - suffix]


def render_pp(pp: Any) -> str:
    """Flatten a Rocq `Pp.t` document, serialized as nested JSON arrays.

    Boxes and tags are ignored, breaks become spaces. Used when the server
    is in `Pp` goal mode; `String` mode needs no rendering.
    """
    out: List[str] = []

    def walk(node: Any) -> None:
        if not isinstance(node, list) or not node:
            return
        tag = node[0] if isinstance(node[0], str) else ""
        if tag == "Ppcmd_string":
            out.append(node[1] if len(node) > 1 and isinstance(node[1], str) else "")
        elif tag == "Ppcmd_glue":
            for child in node[1] if len(node) > 1 and isinstance(node[1], list) else []:
                walk(child)
        elif tag in ("Ppcmd_box", "Ppcmd_tag"):
            if len(node) > 2:
                walk(node[2])
        elif tag == "Ppcmd_print_break":
            out.append(" " * (node[1] if len(node) > 1 and isinstance(node[1], int) else 1))
        elif tag == "Ppcmd_force_newline":
            out.append("\n")
        elif tag == "Ppcmd_comment":
            for child in node[1] if len(node) > 1 and isinstance(node[1], list) else []:
                if isinstance(child, str):
                    out.append(child)

    walk(pp)
    return "".join(out)


def _settings(goal_mode: str = "String") -> Dict[str, Any]:
    """Complete settings object; a missing section makes `initialize` fail.

    Manual mode (`proof.mode` 0) is required: it is what makes an interpret
    command end with a `proofView` notification.
    """
    return {
        "proof": {
            "mode": 0,
            "delegation": "None",
            "workers": 1,
            "block": True,
            "pointInterpretationMode": 0,
        },
        "goals": {"messages": {"full": True}, "ppmode": goal_mode},
        "completion": {
            "enable": True,
            "algorithm": 1,
            "unificationLimit": 100,
            "atomicFactor": 5.0,
            "sizeFactor": 5.0,
        },
        "diagnostics": {"enable": True, "full": False},
        "memory": {"limit": 4},
    }


class RocqLSPClient:
    """A `vsrocqtop` process managing the documents of one project.

    One process serves every file of the project; each open document costs
    memory (often 1-4 GB for a large proof), so callers should close files
    they no longer need.
    """

    def __init__(
        self,
        project_path: Path | str,
        rocq_args: Optional[List[str]] = None,
        executable: str = "vsrocqtop",
        goal_mode: str = "String",
    ) -> None:
        self.project_path = Path(project_path).resolve()
        self.rocq_args = list(rocq_args or [])
        self.goal_mode = goal_mode
        self._lock = threading.RLock()
        self._next_id = 0

        # Per-document state, keyed by project-relative path.
        self._docs: Dict[str, Dict[str, Any]] = {}
        # Server-pushed state, keyed by URI where the protocol provides one.
        self._diagnostics: Dict[str, List[Dict]] = {}
        self._highlights: Dict[str, Dict] = {}
        self._proof_view: Optional[Dict] = None
        self._proof_view_seq = 0
        self._checking_doc: Optional[Dict[str, Any]] = None
        self._search_results: Dict[str, List[Dict]] = {}
        self._responses: Dict[Any, Dict] = {}

        self._executable = executable
        self._start_process()

    def _start_process(self) -> None:
        try:
            self.proc = subprocess.Popen(
                [self._executable, *self.rocq_args],
                cwd=str(self.project_path),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                # Unread stderr would eventually fill its pipe and deadlock.
                stderr=subprocess.DEVNULL,
            )
        except FileNotFoundError as exc:
            raise RocqLSPError(
                f"`{self._executable}` not found on PATH. Install the "
                "vsrocq-language-server opam package."
            ) from exc

        self._queue: "queue.Queue[Any]" = queue.Queue()
        self._reader = threading.Thread(
            target=self._reader_loop, args=(self.proc.stdout, self._queue),
            name="vsrocqtop-reader", daemon=True
        )
        self._reader.start()
        self._initialize()

    # --- transport ----------------------------------------------------------

    def _write(self, message: Dict[str, Any]) -> None:
        if self.proc.poll() is not None:
            raise RocqLSPError(
                f"vsrocqtop exited with status {self.proc.returncode}; "
                "invalid Rocq options are the usual cause."
            )
        body = json.dumps(message).encode("utf-8")
        header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
        try:
            self.proc.stdin.write(header + body)
            self.proc.stdin.flush()
        except (BrokenPipeError, ValueError) as exc:
            raise RocqLSPError(
                f"vsrocqtop closed its input (status {self.proc.poll()})."
            ) from exc

    def _reader_loop(self, stdout, messages: "queue.Queue[Any]") -> None:
        """Read messages off stdout forever, on a thread of their own.

        The stream is buffered, so it cannot be polled with `select`: once a
        large message lands in Python's buffer the kernel has nothing left
        to report, and a caller polling the descriptor would wait out its
        timeout with a complete message already in hand. Reading blocks
        here instead and hands whole messages to `_read` through a queue,
        which also keeps notifications drained so the pipe cannot fill.
        """
        # Bind both to this process: a retiring reader must never publish
        # its messages (especially _CLOSED) into a replacement's queue.
        try:
            while True:
                header = stdout.readline()
                if not header:
                    break
                length = 0
                while header not in (b"\r\n", b"\n", b""):
                    name, _, value = header.decode("ascii", "replace").partition(":")
                    if name.strip().lower() == "content-length":
                        length = int(value.strip())
                    header = stdout.readline()
                if not length:
                    continue
                body = stdout.read(length)
                if not body:
                    break
                try:
                    messages.put(json.loads(body))
                except ValueError:
                    continue
        except (OSError, ValueError):
            pass
        finally:
            messages.put(_CLOSED)

    def _read(self, timeout: float) -> Optional[Dict[str, Any]]:
        """Take the next message, or None if none arrived within `timeout`."""
        try:
            message = self._queue.get(timeout=max(0.0, timeout))
        except queue.Empty:
            return None
        if message is _CLOSED:
            # Put it back so every later read fails the same way.
            self._queue.put(_CLOSED)
            raise RocqLSPError(
                f"vsrocqtop exited with status {self.proc.poll()}."
            )
        return message

    def _dispatch(self, message: Dict[str, Any]) -> None:
        method = message.get("method")
        if method and "id" not in message:
            self._on_notification(method, message.get("params") or {})
        elif "id" in message and method is None:
            self._responses[message["id"]] = message
        # Server-to-client requests (workspace/configuration) are ignored:
        # answering them would re-apply settings and can flip the server out
        # of Manual mode, which the completion signal depends on.

    def _on_notification(self, method: str, params: Dict[str, Any]) -> None:
        if method == "textDocument/publishDiagnostics":
            self._diagnostics[params.get("uri", "")] = params.get("diagnostics", [])
        elif method == "prover/updateHighlights":
            self._highlights[params.get("uri", "")] = params
            doc = self._checking_doc
            if doc is not None and params.get("uri") == doc["uri"]:
                # Activity elsewhere (e.g. an appended definition) does not
                # establish that an edited proof was invalidated.
                edit = doc["dirty_range"]
                start = _position_key(edit["start"])
                end = _position_key(edit["end"])
                ranges = (params.get("processingRange") or []) + (
                    params.get("preparedRange") or []
                )
                for rng in sorted(ranges, key=lambda r: _position_key(r["start"])):
                    first = _position_key(rng["start"])
                    last = _position_key(rng["end"])
                    if first <= start and end <= last and start < last:
                        doc["edit_executed"] = True
                    # Point queries need a checked prefix, not coverage of
                    # the entire replacement (which may extend to EOF).
                    through = doc.get("point_activity_to")
                    if through is not None and first <= through < last:
                        doc["point_activity_to"] = last
        elif method == "prover/proofView":
            self._proof_view = params
            self._proof_view_seq += 1
        elif method == "prover/searchResult":
            self._search_results.setdefault(params.get("id", ""), []).append(params)

    def _pump(self, until: Callable[[], bool], timeout: float, what: str) -> None:
        """Process messages until `until()` holds or `timeout` elapses."""
        deadline = time.monotonic() + timeout
        while not until():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RocqLSPError(
                    f"timed out after {timeout:g}s waiting for {what}. "
                    "Large proof files can legitimately take minutes; raise "
                    "ROCQ_LSP_TIMEOUT if this is expected."
                )
            message = self._read(remaining)
            if message is not None:
                self._dispatch(message)

    def _request(
        self, method: str, params: Dict[str, Any], timeout: float = QUICK_TIMEOUT
    ) -> Any:
        with self._lock:
            self._next_id += 1
            request_id = self._next_id
            self._write(
                {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
            )
            self._pump(
                lambda: request_id in self._responses, timeout, f"a {method} response"
            )
            response = self._responses.pop(request_id)
        if "error" in response and response["error"]:
            raise RocqLSPError(f"{method} failed: {response['error'].get('message')}")
        return response.get("result")

    def _notify(self, method: str, params: Optional[Dict[str, Any]] = None) -> None:
        message: Dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        with self._lock:
            self._write(message)

    # --- lifecycle ----------------------------------------------------------

    def _initialize(self) -> None:
        self._request(
            "initialize",
            {
                "processId": os.getpid(),
                "capabilities": {},
                "rootUri": self._uri(self.project_path),
                "initializationOptions": _settings(self.goal_mode),
            },
            timeout=QUICK_TIMEOUT,
        )
        self._notify("initialized", {})

    def close(self) -> None:
        """Ask the server to exit, killing it if it does not comply."""
        try:
            self._notify("exit")
        except RocqLSPError:
            pass
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=5)
        self._reader.join(timeout=5)
        for stream in (self.proc.stdin, self.proc.stdout):
            try:
                stream.close()
            except Exception:
                pass

    def is_alive(self) -> bool:
        return self.proc.poll() is None

    def memory_bytes(self) -> Optional[int]:
        """Resident memory of the server process, via `ps`."""
        try:
            out = subprocess.run(
                ["ps", "-o", "rss=", "-p", str(self.proc.pid)],
                capture_output=True,
                text=True,
            ).stdout.strip()
            return int(out) * 1024 if out else None
        except (ValueError, OSError):
            return None

    # --- documents ----------------------------------------------------------

    @staticmethod
    def _uri(path: Path | str) -> str:
        return "file://" + str(path)

    def abs_path(self, rel_path: str) -> Path:
        path = Path(rel_path)
        return path if path.is_absolute() else self.project_path / path

    def _doc(self, rel_path: str) -> Dict[str, Any]:
        doc = self._docs.get(rel_path)
        if doc is None:
            raise RocqLSPError(f"`{rel_path}` is not open.")
        return doc

    def _restart(self, keep: str) -> None:
        """Replace the server process, reopening one document in the new one.

        vsrocqtop can be left holding execution states that no longer match
        the text (see `_interpret`), and nothing in the protocol clears them
        reliably: `didClose` and `didOpen` leave the old text in place, and
        `prover/resetRocq` sometimes takes the process down. Starting again
        is the only way to be sure the answer describes the current file.
        """
        content = self._docs[keep]["content"]
        try:
            self.close()
        except RocqLSPError:
            pass
        self._docs.clear()
        self._diagnostics.clear()
        self._highlights.clear()
        self._proof_view = None
        self._proof_view_seq = 0
        self._checking_doc = None
        self._search_results.clear()
        self._responses.clear()
        self._start_process()
        self.open_file(keep, content)

    def _ensure_parsed(self, rel_path: str, timeout: float = 60.0) -> None:
        """Block until the server has finished parsing the document.

        Parsing runs asynchronously after `didOpen` and `didChange`. Until it
        finishes, `documentSymbol` is refused outright and an interpret
        command finds no sentence to run, so it schedules nothing and never
        answers. The server reports the state through that refusal, which is
        what this polls.
        """
        doc = self._doc(rel_path)
        deadline = time.monotonic() + timeout
        delay = 0.05
        while time.monotonic() < deadline:
            try:
                self._request(
                    "textDocument/documentSymbol",
                    {"textDocument": {"uri": doc["uri"]}},
                )
                return
            except RocqLSPError as exc:
                if "Parsing not finished" not in str(exc):
                    # Any other failure is the caller's to deal with.
                    return
            time.sleep(delay)
            delay = min(delay * 2, 0.5)
        raise RocqLSPError(
            f"vsrocqtop is still parsing `{rel_path}` after {timeout:g}s."
        )

    def open_file(self, rel_path: str, content: Optional[str] = None) -> None:
        """Open a document, reloading it if it changed on disk."""
        abs_path = self.abs_path(rel_path)
        if content is None:
            content = abs_path.read_text(encoding="utf-8", errors="replace")

        doc = self._docs.get(rel_path)
        if doc is None:
            self._docs[rel_path] = {
                "uri": self._uri(abs_path),
                "content": content,
                "version": 1,
                "dirty": False,
            }
            self._notify(
                "textDocument/didOpen",
                {
                    "textDocument": {
                        "uri": self._uri(abs_path),
                        "languageId": "rocq",
                        "version": 1,
                        "text": content,
                    }
                },
            )
            self._ensure_parsed(rel_path)
        elif doc["content"] != content:
            self.update_file(rel_path, content)

    def update_file(self, rel_path: str, new_content: str) -> None:
        """Send the minimal range edit turning the open document into `new_content`."""
        doc = self._doc(rel_path)
        if doc["content"] == new_content:
            return
        rng, text = replacement_edit(doc["content"], new_content)
        doc["version"] += 1
        self._notify(
            "textDocument/didChange",
            {
                "textDocument": {"uri": doc["uri"], "version": doc["version"]},
                "contentChanges": [{"range": rng, "text": text}],
            },
        )
        if not doc["dirty"]:
            doc["dirty_content"] = doc["content"]
            doc["point_checked_to"] = _position_key(rng["start"])
        else:
            # Coordinates before the new edit remain valid. Anything at or
            # after it must be established again, even if visited earlier.
            doc["point_checked_to"] = min(
                doc["point_checked_to"], _position_key(rng["start"])
            )
        # Reverse the diff to express the entire pending edit in the NEW
        # document's coordinates, including multiple updates before a check.
        doc["dirty_range"], _ = replacement_edit(new_content, doc["dirty_content"])
        doc["content"] = new_content
        doc["dirty"] = True
        self._ensure_parsed(rel_path)

    def close_file(self, rel_path: str) -> None:
        doc = self._docs.pop(rel_path, None)
        if doc is None:
            return
        self._notify("textDocument/didClose", {"textDocument": {"uri": doc["uri"]}})
        self._diagnostics.pop(doc["uri"], None)

    def open_files(self) -> List[str]:
        return sorted(self._docs)

    def get_file_content(self, rel_path: str) -> str:
        return self._doc(rel_path)["content"]

    # --- positions ----------------------------------------------------------

    def clamp_position(self, rel_path: str, line: int, character: Optional[int]) -> Dict[str, int]:
        """Clamp a 0-indexed position into the document.

        A position past the end makes vsrocqtop exit, so this is not
        optional. `character` of None means the end of the line.
        """
        lines = self._doc(rel_path)["content"].split("\n")
        line = max(0, min(line, len(lines) - 1))
        text = lines[line]
        if character is None:
            character = utf16_len(text)
        return {"line": line, "character": max(0, min(character, utf16_len(text)))}

    # --- checking -----------------------------------------------------------

    def _interpret(
        self, rel_path: str, position: Optional[Dict[str, int]],
        timeout: float, what: str,
    ) -> Optional[Dict]:
        """Interpret, retrying once in a fresh process if an edit was skipped.

        VsRocq can retain executed proof bodies after didChange. A completed
        proof view alone is therefore insufficient for whole-file checking.
        Point queries validate only the prefix through the returned sentence;
        edits later in the document do not require earlier states to execute.
        """
        with self._lock:
            doc = self._doc(rel_path)
            doc["edit_executed"] = False
            doc["point_activity_to"] = (
                doc["point_checked_to"] if doc["dirty"] and position is not None else None
            )
            self._checking_doc = doc if doc["dirty"] else None
            try:
                view = None
                if has_code(doc["content"]):
                    params: Dict[str, Any] = {
                        "textDocument": {"uri": doc["uri"], "version": doc["version"]}
                    }
                    method = "prover/interpretToEnd"
                    if position is not None:
                        method = "prover/interpretToPoint"
                        params["position"] = position
                    self._notify(method, params)
                    view = self._await_proof_view(timeout, what)
            finally:
                self._checking_doc = None

            checked = doc["edit_executed"]
            if doc["dirty"] and position is not None:
                # A cursor in whitespace (or before a tactic) observes the
                # preceding sentence. Reusing that sentence needs no activity.
                target = _position_key(position)
                if view and view.get("range"):
                    target = min(target, _position_key(view["range"]["end"]))
                checked = target <= doc["point_activity_to"]
                if checked:
                    doc["point_checked_to"] = max(doc["point_checked_to"], target)

            if doc["dirty"] and not checked:
                self._restart(rel_path)
                # didOpen creates a clean document, so this cannot retry again.
                return self._interpret(rel_path, position, timeout, what)

            # A changed tactic may execute successfully while the old Qed
            # remains cached. A point check therefore cannot validate the
            # whole document, even when its activity covers the edit.
            if position is None:
                doc["dirty"] = False
                doc.pop("dirty_content", None)
                doc.pop("dirty_range", None)
                doc.pop("point_checked_to", None)
            return view

    def _await_proof_view(
        self, timeout: float, what: str, startup: float = 5.0
    ) -> Optional[Dict]:
        """Run an interpret command to completion.

        In Manual mode the proof view is queued behind every execution
        event, so its arrival means the command finished. An interpret
        command that schedules nothing, such as one aimed before the first
        sentence, emits no events at all; that is reported as no proof view
        rather than by waiting out `timeout`. Real execution always sends a
        highlight update immediately, well before any slow sentence runs,
        so `startup` only has to cover scheduling.
        """
        seen = self._proof_view_seq
        deadline = time.monotonic() + timeout
        started = False

        while self._proof_view_seq <= seen:
            now = time.monotonic()
            if now >= deadline:
                raise RocqLSPError(
                    f"timed out after {timeout:g}s waiting for {what}. Large "
                    "proof files can legitimately take minutes; raise "
                    "ROCQ_LSP_TIMEOUT if this is expected."
                )
            message = self._read(min(0.5, deadline - now) if started else startup)
            if message is None:
                if not started:
                    return None
                continue
            started = True
            self._dispatch(message)

        return self._proof_view

    def check_file(self, rel_path: str, timeout: float = DEFAULT_TIMEOUT) -> List[Dict]:
        """Check the whole document and return its diagnostics."""
        self._interpret(rel_path, None, timeout, f"the check of {rel_path}")
        return self.get_diagnostics(rel_path)

    def goals_at(
        self,
        rel_path: str,
        line: int,
        character: Optional[int] = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> Optional[Dict]:
        """Check up to a position and return the proof view there.

        Only the sentences up to that point are executed, which is much
        cheaper than checking the whole file.
        """
        position = self.clamp_position(rel_path, line, character)
        return self._interpret(
            rel_path, position, timeout, f"the goal at {rel_path}:{line + 1}"
        )

    def get_diagnostics(self, rel_path: str) -> List[Dict]:
        """Diagnostics from the last check, without starting a new one."""
        return self._diagnostics.get(self._doc(rel_path)["uri"], [])

    # --- queries ------------------------------------------------------------

    def query(
        self,
        kind: str,
        rel_path: str,
        line: int,
        character: Optional[int] = None,
        pattern: str = "",
        timeout: float = QUICK_TIMEOUT,
    ) -> str:
        """Run `Check`, `Print`, `About` or `Locate` against the state at a position.

        The position must already have been checked; `goals_at` or
        `check_file` does that.
        """
        method = {
            "check": "prover/check",
            "print": "prover/print",
            "about": "prover/about",
            "locate": "prover/locate",
        }.get(kind.lower())
        if method is None:
            raise RocqLSPError(f"unsupported query `{kind}`")
        doc = self._doc(rel_path)
        result = self._request(
            method,
            {
                "textDocument": {"uri": doc["uri"], "version": doc["version"]},
                "position": self.clamp_position(rel_path, line, character),
                "pattern": pattern,
            },
            timeout=timeout,
        )
        return render_pp(result) if not isinstance(result, str) else result

    def search(
        self,
        rel_path: str,
        pattern: str,
        line: int,
        character: Optional[int] = None,
        quiet_seconds: float = 1.5,
        timeout: float = QUICK_TIMEOUT,
    ) -> List[Dict[str, str]]:
        """Run Rocq's `Search`; results stream in as notifications.

        The protocol has no end-of-results marker, so collection stops
        after `quiet_seconds` without a new result.
        """
        doc = self._doc(rel_path)
        search_id = f"rocq-lsp-mcp-{self._next_id}-{int(time.time() * 1000)}"
        self._search_results[search_id] = []
        self._request(
            "prover/search",
            {
                "textDocument": {"uri": doc["uri"], "version": doc["version"]},
                "position": self.clamp_position(rel_path, line, character),
                "pattern": pattern,
                "id": search_id,
            },
            timeout=timeout,
        )

        deadline = time.monotonic() + timeout
        last_seen = time.monotonic()
        while time.monotonic() < deadline:
            count = len(self._search_results[search_id])
            message = self._read(min(quiet_seconds, deadline - time.monotonic()))
            if message is not None:
                self._dispatch(message)
            if len(self._search_results[search_id]) > count:
                last_seen = time.monotonic()
            elif time.monotonic() - last_seen >= quiet_seconds:
                break

        results = self._search_results.pop(search_id, [])
        return [
            {
                "name": render_pp(item.get("name")),
                "statement": render_pp(item.get("statement")),
            }
            for item in results
        ]

    # --- standard LSP -------------------------------------------------------

    def _text_document_request(
        self, method: str, rel_path: str, line: int, character: Optional[int]
    ) -> Any:
        doc = self._doc(rel_path)
        return self._request(
            method,
            {
                "textDocument": {"uri": doc["uri"]},
                "position": self.clamp_position(rel_path, line, character),
            },
        )

    def hover(self, rel_path: str, line: int, character: Optional[int] = None) -> Any:
        return self._text_document_request("textDocument/hover", rel_path, line, character)

    def definition(self, rel_path: str, line: int, character: Optional[int] = None) -> Any:
        return self._text_document_request(
            "textDocument/definition", rel_path, line, character
        )

    def completions(self, rel_path: str, line: int, character: Optional[int] = None) -> Any:
        result = self._text_document_request(
            "textDocument/completion", rel_path, line, character
        )
        if isinstance(result, dict):
            return result.get("items", [])
        return result or []

    def document_symbols(self, rel_path: str) -> Any:
        self._ensure_parsed(rel_path)
        doc = self._doc(rel_path)
        return self._request(
            "textDocument/documentSymbol", {"textDocument": {"uri": doc["uri"]}}
        ) or []

    def document_proofs(self, rel_path: str) -> Any:
        """Structured proof blocks; covers theorem-like blocks only."""
        doc = self._doc(rel_path)
        result = self._request(
            "prover/documentProofs", {"textDocument": {"uri": doc["uri"]}}
        )
        return (result or {}).get("proofs", [])


def has_code(text: str) -> bool:
    """Whether `text` holds anything but whitespace and (nested) comments.

    A document with no sentences never yields a proof view, so checking one
    would only time out.
    """
    depth = 0
    index = 0
    while index < len(text):
        char = text[index]
        nxt = text[index + 1] if index + 1 < len(text) else ""
        if char == "(" and nxt == "*":
            depth += 1
            index += 2
            continue
        if char == "*" and nxt == ")" and depth:
            depth -= 1
            index += 2
            continue
        if not depth and not char.isspace():
            return True
        index += 1
    return False
