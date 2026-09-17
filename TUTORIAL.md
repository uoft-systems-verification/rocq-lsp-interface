# Tutorial: repairing a proof with `rocq-lsp`

A complete worked session. Every command and every output below was run against
Rocq 9.1; nothing is invented.

The task: finish a proof, without knowing in advance what the relevant lemmas are
called. That is the usual situation, because library names differ between Rocq
versions.

## Before you start

You need `vsrocqtop` and `rocq` on PATH, then:

```sh
uv venv && uv pip install -e .
```

Starting and ending a session are explicit. Working commands never do it for you,
so a stray command can never leave a prover running in the background.

## The file

```sh
mkdir /tmp/rocq-tutorial && cd /tmp/rocq-tutorial
printf -- '-R . Tutorial\n' > _CoqProject
```

`_CoqProject` is what makes this a project. The prover finds it by searching
upward from the file, so you never pass load paths on the command line.

Now open the session. It stays up, holding the prover, until you stop it:

```sh
$ rocq-lsp start /tmp/rocq-tutorial
Session started.
Session ready.
project: /private/tmp/rocq-tutorial
```

The path is optional; it just points the session at a project up front. If you
forget this step, every other command tells you so rather than starting one
behind your back:

```sh
$ rocq-lsp goal lists.v:7
No session running. Start one with `rocq-lsp start`.
```

`lists.v`, with the proof left open:

```coq
From Stdlib Require Import List.
Import ListNotations.

Lemma length_app_swap (A : Type) (l1 l2 : list A) :
  length (l1 ++ l2) = length l2 + length l1.
Proof.
Admitted.
```

## 1. Orient yourself

```sh
$ rocq-lsp outline lists.v
# lists.v

## Imports
L1: From Stdlib Require Import List.
L2: Import ListNotations.

## Declarations
[Thm L4-5] length_app_swap
```

Cheaper than reading the file, and it gives you the line numbers the other
commands want.

## 2. See the goal

`Admitted.` is on line 7, so ask what the state is there.

```sh
$ rocq-lsp goal lists.v:7
Line 7:
Admitted.

--- before ---
A : Type
l1, l2 : list A
⊢ length (l1 ++ l2) = length l2 + length l1

--- after ---
No goals. The proof is complete at this point.
```

With no column you get the state on both sides of the line, which is how you see
what the tactic on that line did. Here "after" is empty because `Admitted.`
discharges the goal.

This command executed only the sentences above line 7. It did not check the rest
of the file.

## 3. Ask the prover what applies

```sh
$ rocq-lsp suggest lists.v:7 --max 4
Lemmas applicable to the goal at line 7 (3792 found):
length_app
partition_length
length_prod
length_combine
... and 3788 more, ranked lower
```

This is Rocq's own goal-directed ranking, not a text search. `length_app` is the
first suggestion and it is the right first step.

## 4. Confirm before you write it

Names move between versions, so check rather than guess:

```sh
$ rocq-lsp search lists.v '(length (_ ++ _) = _)' --max 2
length_app
    forall [A : Type] (l l' : list A), length (l ++ l') = length l + length l'
last_length
    forall [A : Type] (l : list A) (a : A), length (l ++ [a]) = S (length l)
```

Mind the quoting: the pattern must reach Rocq intact, so wrap it in single quotes.
To search by name instead, quote the name too: `'"app_nil"'`.

## 5. Try candidates before editing

`try` replaces a line in the prover's copy only. Your file is not touched.

```sh
$ rocq-lsp try lists.v:7 "  rewrite length_app." "  apply Nat.add_comm."
===   rewrite length_app.
no diagnostics
A : Type
l1, l2 : list A
⊢ length l1 + length l2 = length l2 + length l1

===   apply Nat.add_comm.
error at line 7, columns 9-20:
7 |   apply Nat.add_comm.
  |         ^^^^^^^^^^^^
The reference Nat.add_comm was not found in the current environment.
A : Type
l1, l2 : list A
⊢ length (l1 ++ l2) = length l2 + length l1
```

Two answers at once. `rewrite length_app.` works and leaves an addition to
commute. `Nat.add_comm` is not in scope under this import, which you would
otherwise have found out by editing the file and re-checking.

## 6. Make the edit, then look again

Edit `lists.v` with your normal tools so the proof reads:

```coq
Proof.
  rewrite length_app.
Admitted.
```

Then ask again. You do not tell `rocq-lsp` that you edited anything; it compares
the file on disk against what the prover holds and syncs the difference.

```sh
$ rocq-lsp goal lists.v:8
--- before ---
A : Type
l1, l2 : list A
⊢ length l1 + length l2 = length l2 + length l1
```

## 7. Find the second step

```sh
$ rocq-lsp search lists.v '(_ + _ = _ + _)' --max 2
PeanoNat.Nat.add_comm
    forall n m : nat, n + m = m + n
PeanoNat.Nat.add_succ_comm
    forall n m : nat, S n + m = n + S m
```

There is the qualified name that is actually in scope. `rocq-lsp find add_comm`
searches installed sources by text if you would rather see where it is defined.

## 8. Finish and verify

```coq
Proof.
  rewrite length_app.
  apply PeanoNat.Nat.add_comm.
Qed.
```

```sh
$ rocq-lsp diagnostics lists.v
No diagnostics; the file checks cleanly.
```

`diagnostics` checks the whole file, unlike `goal`. Use it to confirm you are
done, and `goal` while you are working.

## 9. Tidy up

```sh
$ rocq-lsp status
/private/tmp/rocq-tutorial
  project file: /private/tmp/rocq-tutorial/_CoqProject
  memory: 96 MB
  open: lists.v

$ rocq-lsp close lists.v
Closed `lists.v`. It is checked from scratch when next used.

$ rocq-lsp stop
Session stopped.
```

`close` frees one file while you keep working; `stop` ends the session and frees
everything. Both are safe to repeat, and `stop` succeeds even if nothing was
running, so it is safe at the end of a script.

A small file costs about 96 MB. A large development costs 1 to 4 GB per open
file, so `close` matters there. An idle daemon exits on its own after an hour.

## What made that a session

`rocq-lsp start`. It launches a daemon that holds the state until you stop it:

- one prover per project, reused by every later command;
- your documents stay open in it, with the sentences already executed kept;
- edits on disk are detected and sent as a minimal change.

The practical consequence is that the first command on a file costs a full
compile and the rest are fast, as long as your edits stay below what has already
been checked. An edit near the top of a file makes the prover re-execute
everything under it.

## Command reference

| Want to | Command |
|---|---|
| See the proof state | `rocq-lsp goal FILE:LINE` |
| Find the next step | `rocq-lsp suggest FILE:LINE` |
| Find a lemma by shape | `rocq-lsp search FILE '(_ + _ = _ + _)'` |
| Find a lemma by name | `rocq-lsp search FILE '"add_comm"'` |
| Check a name exists | `rocq-lsp find add_comm` |
| Inspect a term | `rocq-lsp query FILE:LINE Print foo` |
| Compare tactics | `rocq-lsp try FILE:LINE "  tac1." "  tac2."` |
| Check the whole file | `rocq-lsp diagnostics FILE` |
| See the file's structure | `rocq-lsp outline FILE` |
| Free one file's memory | `rocq-lsp close FILE` |
| Begin and end a session | `rocq-lsp start` / `rocq-lsp stop` |

Add `--help` to any of them.
