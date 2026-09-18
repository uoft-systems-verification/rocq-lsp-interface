# Tutorial: repairing a proof with `rocq-lsp`

A complete worked session. Every command and every output below was run against
Rocq 9.1; nothing is invented.

The task: finish a proof, testing each step in the prover before it goes into
the file.

## Before you start

You need `vsrocqtop` and `rocq` on PATH, then:

```sh
uv sync && uv pip install -e .
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

No provers running. They start on the first file command.
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

## 3. Try candidates before editing

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

[error] The reference Nat.add_comm was not found in the current environment.
```

Two answers at once. `rewrite length_app.` works and leaves an addition to
commute. `Nat.add_comm` is not in scope under this import, which you would
otherwise have found out by editing the file and re-checking.

## 4. Make the edit, then look again

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
Line 8:
Admitted.

--- before ---
A : Type
l1, l2 : list A
⊢ length l1 + length l2 = length l2 + length l1

--- after ---
No goals. The proof is complete at this point.
```

## 5. Find the second step

The short name was not in scope, so try the qualified one:

```sh
$ rocq-lsp try lists.v:8 "  apply PeanoNat.Nat.add_comm."
===   apply PeanoNat.Nat.add_comm.
no diagnostics
No goals. The proof is complete at this point.
```

## 6. Finish and verify

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

## 7. Tidy up

```sh
$ rocq-lsp status
/private/tmp/rocq-tutorial
  project file: /private/tmp/rocq-tutorial/_CoqProject
  memory: 217 MB
  open: lists.v

$ rocq-lsp close lists.v
Closed `lists.v`. It is checked from scratch when next used.

$ rocq-lsp stop
Session stopped.
```

`close` frees one file while you keep working; `stop` ends the session and frees
everything. Both are safe to repeat, and `stop` succeeds even if nothing was
running, so it is safe at the end of a script.

A small file costs about 200 MB. A large development costs 1 to 4 GB per open
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
| Compare tactics | `rocq-lsp try FILE:LINE "  tac1." "  tac2."` |
| Check the whole file | `rocq-lsp diagnostics FILE` |
| See the file's structure | `rocq-lsp outline FILE` |
| Free one file's memory | `rocq-lsp close FILE` |
| Begin and end a session | `rocq-lsp start` / `rocq-lsp stop` |

Add `--help` to any of them.
