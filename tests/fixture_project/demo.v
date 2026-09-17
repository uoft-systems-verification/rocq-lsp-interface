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
