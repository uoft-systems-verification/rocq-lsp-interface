"""Probe VsRocq directly, bypassing the client's stale-check recovery.

Run with the project's Python environment. An optional executable argument
allows comparing the installed server with an experimental local build.
"""

import argparse
import json
from tempfile import TemporaryDirectory

from rocq_lsp_mcp.rocq_client import RocqLSPClient

PROOF = """Definition n := 1.
Lemma one : n + 0 = 1.
Proof.
  simpl.
  reflexivity.
Qed.
"""


def interpret(client, line=None):
    doc = client._doc("Probe.v")
    params = {"textDocument": {"uri": doc["uri"], "version": doc["version"]}}
    method = "prover/interpretToEnd"
    if line is not None:
        method = "prover/interpretToPoint"
        params["position"] = client.clamp_position("Probe.v", line, None)
    client._notify(method, params)
    client._await_proof_view(30, "probe")
    return [d["message"] for d in client.get_diagnostics("Probe.v")]


def probe(executable, original, changed, visit_tactic=False):
    with TemporaryDirectory() as directory:
        client = RocqLSPClient(directory, executable=executable)
        try:
            client.open_file("Probe.v", original)
            assert interpret(client) == []
            pid = client.proc.pid
            client.update_file("Probe.v", changed)
            if visit_tactic:
                interpret(client, 4)
            errors = interpret(client)
            doc = client._doc("Probe.v")
            state = client._request("prover/documentState", {
                "textDocument": {"uri": doc["uri"], "version": doc["version"]},
            })["document"].split("Document using sentences_by_end")[0]
            client.update_file("Probe.v", original)
            restored = interpret(client)
            return {
                "errors": errors,
                "restored_errors": restored,
                "same_process": client.proc.pid == pid,
                "state_after_edit": state,
            }
        finally:
            client.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("executable", nargs="?", default="vsrocqtop")
    parser.add_argument("--expect-fixed", action="store_true")
    args = parser.parse_args()
    transparent = PROOF.replace("Qed.", "Defined.")
    cases = {
        "tactic_replacement": (PROOF, PROOF.replace("reflexivity.", "discriminate."), False),
        "unfinished_proof": (PROOF, PROOF.replace("reflexivity.", "idtac."), False),
        "point_then_end": (PROOF, PROOF.replace("reflexivity.", "idtac."), True),
        "mixed_edit": (PROOF, PROOF.replace("reflexivity.", "discriminate.")
                       + "Definition later := 2.\n", False),
        "sentence_deletion": (PROOF, PROOF.replace("  reflexivity.\n", ""), False),
        "transparent_proof": (
            transparent, transparent.replace("reflexivity.", "idtac."), False,
        ),
    }
    results = {name: probe(args.executable, *case) for name, case in cases.items()}
    print(json.dumps(results, indent=2))
    if args.expect_fixed:
        assert all(r["errors"] and not r["restored_errors"] and r["same_process"]
                   for r in results.values())


if __name__ == "__main__":
    main()
