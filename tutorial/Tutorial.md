# Tutorial: repairing a proof with `rocq-lsp`

Edit a working proof, locate the failure, and test a repair.

## Setup

With `vsrocqtop` and `rocq` on PATH, run from the repository root:

```sh
uv sync && uv pip install -e .
source .venv/bin/activate
cd tutorial
```

Use the supplied [`lists.v`](./lists.v) and `_CoqProject`.

Before using `rocq-lsp`, build your project's dependencies so the required
`.vos` files are available. Proof checks load these compiled dependencies as-is;
they do not build or update them.

## 1. Start and check

```sh
rocq-lsp start
rocq-lsp goal lists.v:13
```

Line 13 is the final `Qed.`, so this checks the whole file:

```text
Line 13:
Qed.

--- before ---
No goals. The proof is complete at this point.

--- after ---
No goals. The proof is complete at this point.
```

Keep the session running. Commands pick up saved edits automatically and reuse
unchanged checked prefixes when possible.

## 2. Break the proof

In `lists.v`, reverse the concatenation on line 5:

```diff
 Definition combine_lists {A : Type} (l1 l2 : list A) : list A :=
-  l1 ++ l2.
+  l2 ++ l1.
```

Check again:

```sh
rocq-lsp goal lists.v:13
```

The output includes the earlier failing tactic's location:

```text
--- errors ---
error at line 12, columns 3-13:
12 |   reflexivity.
   |   ^^^^^^^^^^^
In environment
A : Type
l1, l2 : list A
Unable to unify "length l1 + length l2" with "length l2 + length l1".
```

Check `--- errors ---` even if a later position shows “No goals”.

## 3. Inspect the failure

```sh
rocq-lsp goal lists.v:12
```

`goal` shows the state before and after the line. Here both are unchanged
(output excerpt):

```text
Line 12:
  reflexivity.

--- before ---
A : Type
l1, l2 : list A
⊢ length l2 + length l1 = length l1 + length l2

--- after ---
A : Type
l1, l2 : list A
⊢ length l2 + length l1 = length l1 + length l2
```

The remaining goal needs commutativity of addition.

## 4. Try repairs (optional)

`try` tests each replacement independently in the prover, without changing
the file on disk:

```sh
rocq-lsp try lists.v:12 "  symmetry." "  apply PeanoNat.Nat.add_comm."
```

```text
===   symmetry.
no diagnostics
A : Type
l1, l2 : list A
⊢ length l1 + length l2 = length l2 + length l1

===   apply PeanoNat.Nat.add_comm.
no diagnostics
No goals. The proof is complete at this point.
```

The first candidate leaves a goal; the second solves it. You can also skip
`try` and edit directly.

## 5. Save and verify

Replace line 12:

```diff
-  reflexivity.
+  apply PeanoNat.Nat.add_comm.
```

`try` checks only the replacement tactic. Check through `Qed.` after saving:

```sh
rocq-lsp goal lists.v:13
```

The output now shows no goals and no `--- errors ---` section.

## 6. Clean up

```sh
rocq-lsp stop
```

To release only this file while keeping the session running, use
`rocq-lsp close lists.v` instead.

Use `rocq-lsp --help` or `rocq-lsp COMMAND --help` for more options.
