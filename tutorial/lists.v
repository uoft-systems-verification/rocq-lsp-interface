From Stdlib Require Import List.
Import ListNotations.

Definition combine_lists {A : Type} (l1 l2 : list A) : list A :=
  l1 ++ l2.

Lemma length_combine_lists (A : Type) (l1 l2 : list A) :
  length (combine_lists l1 l2) = length l1 + length l2.
Proof.
  unfold combine_lists.
  rewrite length_app.
  reflexivity.
Qed.
