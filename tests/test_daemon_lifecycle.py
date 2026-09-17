"""Stopping an active proof check must release its actual prover process."""

import json
import os
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

LAUNCHER = """
import sys
from pathlib import Path
from rocq_lsp_mcp import daemon
from rocq_lsp_mcp.rocq_client import RocqLSPClient

marker = Path(sys.argv[1])
original = RocqLSPClient._on_notification
def notification(self, method, params):
    if method == 'prover/updateHighlights' and params.get('processingRange'):
        marker.write_text(str(self.proc.pid))
    return original(self, method, params)
RocqLSPClient._on_notification = notification
raise SystemExit(daemon.serve(idle_timeout=0.1))
"""


@pytest.fixture
def session(tmp_path, repo_root, test_project_path):
    socket_path = Path(f"/tmp/rocq-stop-{uuid.uuid4().hex[:12]}.sock")
    marker = tmp_path / "prover.pid"
    env = {
        **os.environ, "ROCQ_LSP_SOCKET": str(socket_path),
        "PYTHONPATH": str(repo_root / "src"), "ROCQ_LSP_TIMEOUT": "300",
    }
    children = []

    def start(timeout="300"):
        env["ROCQ_LSP_TIMEOUT"] = timeout
        daemon = subprocess.Popen(
            [sys.executable, "-c", LAUNCHER, str(marker)], env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        children.append(daemon)
        deadline = time.monotonic() + 10
        while not socket_path.exists():
            assert daemon.poll() is None, "daemon exited during startup"
            assert time.monotonic() < deadline, "daemon did not create its socket"
            time.sleep(0.02)
        return daemon

    def command(*args, background=False):
        child = subprocess.Popen(
            [sys.executable, "-m", "rocq_lsp_mcp.cli", *map(str, args)], env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        children.append(child)
        if background:
            return child
        output, _ = child.communicate(timeout=15)
        return child.returncode, output

    slow = tmp_path / "Slow.v"
    # Bound the fixture even if cancellation is broken. The production check
    # has no timeout; this Rocq sentence has its own independent safety limit.
    slow.write_text(
        "Lemma slow : True.\nProof.\n"
        "Timeout 30 (let rec loop x := loop x in loop constr:(0)).\nQed.\n"
    )
    try:
        yield start, command, slow, marker, socket_path
    finally:
        for child in children:
            if child.poll() is None:
                child.terminate()
        for child in children:
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=5)
            if child.stdout:
                child.stdout.close()
        # Only the fixture's own prover PID is ever targeted on failure.
        if marker.exists():
            try:
                os.kill(int(marker.read_text()), 9)
            except ProcessLookupError:
                pass
        socket_path.unlink(missing_ok=True)


def wait_for_prover(marker, check):
    deadline = time.monotonic() + 10
    while not marker.exists() or not marker.read_text():
        assert check.poll() is None, "check ended before execution began"
        assert time.monotonic() < deadline, "prover never began executing"
        time.sleep(0.02)
    return int(marker.read_text())


def test_stop_interrupts_check_and_waits_for_process_cleanup(session):
    start, command, slow, marker, socket_path = session
    daemon = start()
    check = command("diagnostics", slow, background=True)
    pid = wait_for_prover(marker, check)
    # Cross the listener's idle-timeout poll while checking remains active.
    time.sleep(5.5)
    assert daemon.poll() is None
    assert check.poll() is None
    # Establish the queued connection before launching the shutdown command.
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as queued:
        queued.settimeout(15)
        queued.connect(str(socket_path))
        queued.sendall(b'{"tool":"status","args":{}}\n')
        code, output = command("stop")
        response = json.loads(queued.recv(4096))
        assert response["ok"] is False and "cancelled" in response["text"]
    assert code == 0 and "Session stopped." in output
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    assert not socket_path.exists()
    text, _ = check.communicate(timeout=5)
    assert check.returncode == 1
    assert "cancelled" in text
    assert daemon.wait(timeout=5) == 0
