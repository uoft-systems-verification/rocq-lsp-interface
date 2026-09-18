"""Tests for the `rocq-lsp` command line, run as a real subprocess.

Each run uses its own daemon socket, so a developer's own daemon is left
alone.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from rocq_lsp_mcp.cli import parse_location

# --- location parsing (no prover needed) ------------------------------------


def test_parse_location_accepts_line_and_column():
    parsed = parse_location("/tmp/a.v:12")
    assert parsed["line"] == 12 and parsed["column"] is None
    parsed = parse_location("/tmp/a.v:12:5", want_column=True)
    assert (parsed["line"], parsed["column"]) == (12, 5)


def test_parse_location_rejects_bad_input():
    with pytest.raises(Exception):
        parse_location("/tmp/a.v")
    with pytest.raises(Exception):
        parse_location("/tmp/a.v:12", want_column=True)


# --- the command line against a real prover ---------------------------------


@pytest.fixture(scope="module")
def cli(test_project_path: Path, repo_root: Path):
    """A `rocq-lsp` runner with its own daemon, stopped at the end.

    The socket lives directly under /tmp because a unix socket path has
    only about a hundred bytes to work with.
    """
    socket = Path(f"/tmp/rocq-lsp-test-{os.getpid()}.sock")
    env = {
        **os.environ,
        "ROCQ_LSP_SOCKET": str(socket),
    }

    def run(*args: str, expect_ok: bool = True) -> str:
        done = subprocess.run(
            [sys.executable, "-m", "rocq_lsp_mcp.cli", *args],
            capture_output=True, text=True, env=env, cwd=str(repo_root), timeout=300,
        )
        if expect_ok:
            assert done.returncode == 0, f"{args} failed:\n{done.stdout}{done.stderr}"
        return done.stdout + done.stderr

    run("start", str(test_project_path))
    yield run
    run("stop")


@pytest.fixture(scope="module")
def demo(test_project_path: Path) -> str:
    return str(test_project_path / "demo.v")


def test_goal_shows_before_and_after(cli, demo):
    """Line 13 is `intros l.`, so `l` enters the context across it."""
    out = cli("goal", f"{demo}:13")
    assert "--- before ---" in out and "--- after ---" in out
    before, after = out.split("--- after ---")
    assert "forall" in before
    assert "l : list nat" in after and "l ++ [] = l" in after


def test_goal_reports_a_finished_proof(cli, demo):
    out = cli("goal", f"{demo}:14")
    assert "No goals" in out.split("--- after ---")[1]


@pytest.mark.parametrize("position", ["5", "6", "5:15"])
def test_goal_locates_the_failing_tactic(cli, test_project_path, position):
    out = cli("goal", f"{test_project_path / 'broken.v'}:{position}")
    errors = out.split("--- errors ---")[1]
    assert "error at line 5, columns" in errors
    assert "5 |   reflexivity." in errors
    assert "^^" in errors
    assert "Unable to unify" in errors
    assert "Unsolved goals:" not in out


def test_goal_keeps_the_original_unfocused_summary(cli, tmp_path):
    path = tmp_path / "Focused.v"
    path.write_text("Lemma pending : True /\\ 1 = 1.\nProof.\n  split.\n  - idtac.\nQed.\n")
    out = cli("goal", f"{path}:4")
    after = out.split("--- after ---")[1]
    assert "⊢ True" in after
    assert "(1 unfocused goal(s))" in after
    assert "⊢ 1 = 1" not in after
    assert "Unsolved goals:" not in out


def test_suggest_ranks_the_closing_lemma_first(cli, demo):
    out = cli("suggest", f"{demo}:13", "--max", "5")
    assert out.splitlines()[1] == "app_nil_r"


def test_query_print(cli, demo):
    assert "42" in cli("query", f"{demo}:5", "Print", "answer")


def test_search_by_name_and_by_pattern(cli, demo):
    assert "app_nil_r" in cli("search", demo, '"app_nil"', "--max", "5")
    assert "app_nil_r" in cli("search", demo, "(_ ++ nil = _)")


def test_search_guides_when_the_name_is_unquoted(cli, demo):
    """A bare name is not a constant, and the message must say how to fix it."""
    out = cli("search", demo, "app_nil", expect_ok=False)
    assert "quote it" in out


def test_outline_lists_declarations(cli, demo):
    out = cli("outline", demo)
    for name in ("answer", "answer_is_42", "app_nil_demo"):
        assert name in out


def test_diagnostics_clean_and_broken(cli, demo, test_project_path):
    assert "checks cleanly" in cli("diagnostics", demo)
    broken = cli("diagnostics", str(test_project_path / "broken.v"))
    assert "Unable to unify" in broken
    assert "Unsolved goals:\n⊢ two = 3" in broken


def test_diagnostics_rechecks_a_changed_proof(cli, tmp_path):
    path = tmp_path / "Changed.v"
    good = "Lemma one : 1 = 1. Proof. reflexivity. Qed.\n"
    path.write_text(good)
    assert "checks cleanly" in cli("diagnostics", str(path))
    path.write_text(good.replace("reflexivity.", "discriminate."))
    assert "No applicable tactic" in cli("diagnostics", str(path))
    path.write_text(good)
    assert "checks cleanly" in cli("diagnostics", str(path))


def test_try_compares_tactics_without_touching_the_file(cli, demo):
    before = Path(demo).read_text()
    out = cli("try", f"{demo}:14", "  apply app_nil_r.", "  reflexivity.")
    assert "No goals" in out and "Unable to unify" in out
    assert Path(demo).read_text() == before


def test_hover_reports_a_type(cli, demo):
    assert "nat" in cli("hover", f"{demo}:4:12")


def test_find_locates_a_declaration(cli, demo):
    cli("outline", demo)  # sets the project
    assert "answer_is_42" in cli("find", "answer_is")


def test_run_code_checks_a_snippet(cli, demo):
    cli("outline", demo)
    assert "cleanly" in cli("run-code", "--code", "Definition n := 1.\n")
    bad = cli("run-code", "--code", "Lemma b : 1 = 2. Proof. reflexivity. Qed.\n")
    assert "Unable to unify" in bad
    assert "Unsolved goals:" not in bad


def test_status_and_close(cli, demo):
    cli("outline", demo)
    assert "demo.v" in cli("status")
    assert "Closed" in cli("close", demo)
    assert "demo.v" not in cli("status").split("open:")[-1]


def test_missing_file_is_reported(cli, test_project_path):
    out = cli("goal", f"{test_project_path / 'nope.v'}:1", expect_ok=False)
    assert "does not exist" in out


def test_working_commands_do_not_start_a_session(cli, demo, repo_root):
    """Only `start` may bring a session up."""
    lonely = Path(f"/tmp/rocq-lsp-none-{os.getpid()}.sock")
    env = {**os.environ, "ROCQ_LSP_SOCKET": str(lonely)}
    done = subprocess.run(
        [sys.executable, "-m", "rocq_lsp_mcp.cli", "goal", f"{demo}:13"],
        capture_output=True, text=True, env=env, cwd=str(repo_root), timeout=60,
    )
    assert done.returncode == 1
    assert "rocq-lsp start" in done.stdout
    assert not lonely.exists(), "a working command must not leave a daemon behind"


def test_start_is_idempotent_and_stop_always_succeeds(cli):
    assert "already running" in cli("start")
    assert "stopped" in cli("stop")
    assert "No session running" in cli("stop")
    cli("start")  # leave one for the remaining tests
