"""Long checks keep their connection; only explicit deadlines end checking."""

import json
import socket
import threading
import time
from unittest.mock import Mock

import pytest

from rocq_lsp_mcp import cli, rocq_client
from rocq_lsp_mcp.rocq_client import RocqLSPClient, RocqLSPError


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(RocqLSPClient, "_start_process", Mock())
    monkeypatch.setattr(RocqLSPClient, "close", Mock())
    monkeypatch.setattr(RocqLSPClient, "_notify", Mock())
    monkeypatch.setattr(RocqLSPClient, "_ensure_parsed", Mock())
    client = RocqLSPClient(tmp_path)
    client.open_file("Proof.v", "Lemma one : True. Proof. exact I. Qed.")
    return client


def test_check_waits_past_the_old_prover_deadline(client, monkeypatch):
    elapsed = [0.0]
    monkeypatch.setattr(rocq_client.time, "monotonic", lambda: elapsed[0])
    events = iter([
        {"method": "prover/updateHighlights", "params": {}},
        None,
        {"method": "prover/proofView", "params": {"proof": None}},
    ])

    def read(wait):
        elapsed[0] += 600
        return next(events)

    monkeypatch.setattr(client, "_read", read)
    assert client.check_file("Proof.v") == []
    assert elapsed[0] > 300
    client.close.assert_not_called()


@pytest.mark.parametrize("started", [False, True])
def test_explicit_deadline_closes_the_prover(client, monkeypatch, started):
    elapsed = [0.0]
    monkeypatch.setattr(rocq_client.time, "monotonic", lambda: elapsed[0])

    def read(wait):
        elapsed[0] += wait
        return {"method": "prover/updateHighlights", "params": {}} if started else None

    monkeypatch.setattr(client, "_read", read)
    with pytest.raises(RocqLSPError, match="timed out after 1s"):
        client.check_file("Proof.v", timeout=1)
    client.close.assert_called_once()


def test_cli_keeps_waiting_for_the_result(monkeypatch):
    client, server = socket.socketpair()
    client.settimeout(0.01)
    monkeypatch.setattr(cli, "_connect", lambda path: client)
    # Accelerate the former fixed cutoff: the old implementation would fail.
    monkeypatch.setattr(cli, "CONNECT_TIMEOUT", 0.01, raising=False)

    def reply():
        with server:
            server.recv(4096)
            time.sleep(0.05)
            try:
                server.sendall(json.dumps({"ok": True, "text": "finished"}).encode() + b"\n")
            except BrokenPipeError:
                pass

    worker = threading.Thread(target=reply)
    worker.start()
    try:
        assert cli.call("diagnostics", {"file_path": "Proof.v"}) == (True, "finished")
    finally:
        worker.join(timeout=2)
        client.close()


def test_stop_does_not_hide_a_failed_shutdown(monkeypatch):
    monkeypatch.setattr(cli, "call", lambda *args: (False, "daemon disconnected"))
    output = Mock()
    monkeypatch.setattr("builtins.print", output)
    assert cli.handle_stop() == 1
    output.assert_called_once_with("daemon disconnected")
