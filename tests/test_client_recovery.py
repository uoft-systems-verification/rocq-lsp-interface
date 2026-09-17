"""Protocol boundary cases for stale-check recovery, without a prover."""

from unittest.mock import Mock

import pytest

from rocq_lsp_mcp.rocq_client import RocqLSPClient, RocqLSPError


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(RocqLSPClient, "_start_process", Mock())
    monkeypatch.setattr(RocqLSPClient, "close", Mock())
    monkeypatch.setattr(RocqLSPClient, "_notify", Mock())
    monkeypatch.setattr(RocqLSPClient, "_ensure_parsed", Mock())
    client = RocqLSPClient(tmp_path)
    client.open_file("Proof.v", "Lemma one : 1 = 1. Proof. reflexivity. Qed.")
    client.update_file("Proof.v", "Lemma one : 1 = 1. Proof. discriminate. Qed.")
    return client


@pytest.mark.parametrize("kind,other_uri,restarts", [
    ("processingRange", False, False),
    ("preparedRange", False, False),
    ("processedRange", False, True),
    ("processingRange", True, True),
])
@pytest.mark.parametrize("point", [False, True])
def test_only_activity_for_the_edited_document_counts(
    client, monkeypatch, kind, other_uri, restarts, point,
):
    doc = client._doc("Proof.v")
    uri = doc["uri"]
    content = doc["content"]
    views = [{"proof": "stale"}, {"proof": "fresh"}]

    def finish(*args):
        client._on_notification("prover/updateHighlights", {
            "uri": uri + ".other" if other_uri else uri,
            kind: [{"start": {"line": 0, "character": 0},
                    "end": {"line": 0, "character": len(content)}}],
        })
        return views.pop(0)

    monkeypatch.setattr(client, "_await_proof_view", finish)
    if point:
        assert client.goals_at("Proof.v", 0) == {
            "proof": "fresh" if restarts else "stale"
        }
    else:
        client.check_file("Proof.v")
    assert client._start_process.call_count == (2 if restarts else 1)
    assert client.get_file_content("Proof.v") == content
    assert client._doc("Proof.v")["dirty"] == (point and not restarts)
    if restarts:
        # The replay must refer to the reopened version and retain its target.
        method, params = client._notify.call_args.args
        assert params["textDocument"] == {"uri": uri, "version": 1}
        assert method == ("prover/interpretToPoint" if point else "prover/interpretToEnd")
        if point:
            assert params["position"] == {"line": 0, "character": len(content)}


def test_failed_check_keeps_the_edit_pending(client, monkeypatch):
    monkeypatch.setattr(client, "_await_proof_view", Mock(
        side_effect=RocqLSPError("timed out")
    ))
    with pytest.raises(RocqLSPError, match="timed out"):
        client.check_file("Proof.v")
    assert client._doc("Proof.v")["dirty"]
    assert client._checking_doc is None


def test_failed_point_check_does_not_advance_the_checked_prefix(client, monkeypatch):
    doc = client._doc("Proof.v")
    before = doc["point_checked_to"]

    def timeout(*args):
        client._on_notification("prover/updateHighlights", {
            "uri": doc["uri"],
            "preparedRange": [{"start": {"line": 0, "character": 0},
                               "end": {"line": 0, "character": len(doc["content"])}}],
        })
        raise RocqLSPError("timed out")

    monkeypatch.setattr(client, "_await_proof_view", timeout)
    with pytest.raises(RocqLSPError, match="timed out"):
        client.goals_at("Proof.v", 0)
    assert doc["point_checked_to"] == before
    assert doc["dirty"]
    assert client._checking_doc is None


def test_failed_restart_does_not_return_cached_clean_diagnostics(client, monkeypatch):
    monkeypatch.setattr(client, "_await_proof_view", Mock(return_value={"proof": None}))
    monkeypatch.setattr(client, "_start_process", Mock(
        side_effect=RocqLSPError("could not start")
    ))
    with pytest.raises(RocqLSPError, match="could not start"):
        client.check_file("Proof.v")
