"""A tiny Rocq project used by the tests."""

from __future__ import annotations

import shutil
from pathlib import Path

DEMO = """\
Require Import List.
Import ListNotations.

Definition answer := 42.

Lemma answer_is_42 : answer = 42.
Proof.
  reflexivity.
Qed.

Lemma app_nil_demo : forall (l : list nat), l ++ [] = l.
Proof.
  intros l.
  apply app_nil_r.
Qed.

(* trailing comment *)
"""

BROKEN = """\
Definition two := 2.

Lemma wrong : two = 3.
Proof.
  reflexivity.
Qed.
"""


def ensure_test_project(root: Path) -> Path:
    """Create the fixture project, or skip if Rocq is not installed."""
    if shutil.which("vsrocqtop") is None:
        raise RuntimeError("vsrocqtop not found; install vsrocq-language-server")

    project = root / "tests" / "fixture_project"
    project.mkdir(parents=True, exist_ok=True)
    (project / "_CoqProject").write_text("-R . Demo\n", encoding="utf-8")
    (project / "demo.v").write_text(DEMO, encoding="utf-8")
    (project / "broken.v").write_text(BROKEN, encoding="utf-8")
    return project
