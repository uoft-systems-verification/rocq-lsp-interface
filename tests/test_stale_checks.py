"""Regression tests against a real prover's incremental proof cache."""

import shutil

import pytest

from rocq_lsp_mcp.rocq_client import RocqLSPClient

PROOF = """Definition n := 1.
Lemma one : n + 0 = 1.
Proof.
  simpl.
  reflexivity.
Qed.
"""


@pytest.fixture
def prover(tmp_path):
    if shutil.which("vsrocqtop") is None:
        pytest.skip("vsrocqtop not found")
    client = RocqLSPClient(tmp_path)
    try:
        yield client
    finally:
        client.close()


@pytest.mark.parametrize("original,broken", [
    (PROOF, PROOF.replace("reflexivity.", "discriminate.")),
    (PROOF, PROOF.replace("  reflexivity.\n", "")),
    (PROOF, PROOF.replace("reflexivity.", "reflexivit.")),
    ("Lemma one : 1 = 1. Proof. reflexivity. Qed.\n",
     "Lemma one : 1 = 1. Proof. discriminate. Qed.\n"),
    (PROOF, PROOF.replace("reflexivity.", "discriminate.")
     + "Definition later := 2.\n"),
], ids=["tactic", "deletion", "mid-sentence", "one-line", "unrelated-activity"])
def test_changed_proof_is_rechecked(prover, original, broken):
    prover.open_file("Proof.v", original)
    assert prover.check_file("Proof.v") == []
    prover.update_file("Proof.v", broken)
    assert any(d.get("severity") == 1 for d in prover.check_file("Proof.v"))
    assert any(d.get("severity") == 1 for d in prover.check_file("Proof.v"))
    prover.update_file("Proof.v", original)
    assert prover.check_file("Proof.v") == []


def test_changed_goal_matches_a_fresh_check(prover, tmp_path):
    prover.open_file("Proof.v", PROOF)
    prover.check_file("Proof.v")
    changed = PROOF.replace("simpl.", "idtac.")
    prover.update_file("Proof.v", changed)
    actual = prover.goals_at("Proof.v", 3)
    fresh = RocqLSPClient(tmp_path)
    try:
        fresh.open_file("Proof.v", changed)
        assert actual == fresh.goals_at("Proof.v", 3)
    finally:
        fresh.close()


@pytest.mark.parametrize("changed", [
    PROOF.replace("n := 1", "n := 2"),
    PROOF.replace("n + 0 = 1", "n + 0 = 2"),
    PROOF.replace("  simpl.\n", "  simpl.\n  discriminate.\n"),
], ids=["definition", "statement", "insertion"])
def test_real_reexecution_keeps_the_process(prover, changed):
    prover.open_file("Proof.v", PROOF)
    assert prover.check_file("Proof.v") == []
    process = prover.proc
    assert prover.check_file("Proof.v") == []
    prover.update_file("Proof.v", changed)
    assert any(d.get("severity") == 1 for d in prover.check_file("Proof.v"))
    assert prover.proc is process


def test_deleting_all_code_clears_old_errors(prover):
    prover.open_file("Proof.v", PROOF.replace("reflexivity.", "discriminate."))
    assert prover.check_file("Proof.v")
    prover.update_file("Proof.v", "(* empty *)\n")
    assert prover.check_file("Proof.v") == []
    assert prover.goals_at("Proof.v", 0) is None


def test_partial_goal_does_not_validate_a_later_stale_proof(prover):
    prover.open_file("Proof.v", PROOF)
    prover.check_file("Proof.v")
    prover.update_file("Proof.v", PROOF.replace("reflexivity.", "discriminate."))
    prover.goals_at("Proof.v", 0)
    assert any(d.get("severity") == 1 for d in prover.check_file("Proof.v"))


def test_executing_changed_tactic_does_not_validate_cached_qed(prover):
    prover.open_file("Proof.v", PROOF)
    prover.check_file("Proof.v")
    prover.update_file("Proof.v", PROOF.replace("reflexivity.", "idtac."))
    # This tactic succeeds and produces real activity, but leaves an open goal.
    # VsRocq still has the old successful Qed cached after checking the tactic.
    assert prover.goals_at("Proof.v", 4)
    diagnostics = prover.check_file("Proof.v")
    assert any("incomplete proof" in d["message"] for d in diagnostics)


def test_multiple_edits_before_check_are_all_validated(prover):
    prover.open_file("Proof.v", PROOF)
    prover.check_file("Proof.v")
    broken = PROOF.replace("reflexivity.", "discriminate.")
    prover.update_file("Proof.v", broken)
    prover.update_file("Proof.v", broken + "Definition later := 2.\n")
    assert any(d.get("severity") == 1 for d in prover.check_file("Proof.v"))


def test_restart_releases_old_process_and_other_document_caches(prover, tmp_path):
    other = tmp_path / "Other.v"
    other.write_text("Definition other := 1.\n")
    prover.open_file("Other.v")
    prover.check_file("Other.v")
    prover.open_file("Proof.v", PROOF)
    prover.check_file("Proof.v")
    process, reader = prover.proc, prover._reader
    prover.update_file("Proof.v", PROOF.replace("reflexivity.", "discriminate."))
    assert prover.check_file("Proof.v")
    assert process.poll() is not None
    assert not reader.is_alive()
    assert prover.open_files() == ["Proof.v"]
    assert prover._uri(other) not in prover._diagnostics
    prover.open_file("Other.v")
    assert prover.check_file("Other.v") == []
