"""Time goal requests and in-memory edits against an existing project file.

The source file is never written. This uses the same before/after point queries
as the CLI/MCP goal tool, with a private prover closed when the benchmark ends.
"""

import argparse
import hashlib
import json
import time
from pathlib import Path

from rocq_lsp_mcp.rocq_client import RocqLSPClient


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project", type=Path)
    parser.add_argument("file")
    parser.add_argument("--after-line", type=int, default=186)
    parser.add_argument("--expect-incremental", action="store_true")
    args = parser.parse_args()
    path = args.project / args.file
    original_bytes = path.read_bytes()
    original = original_bytes.decode()
    lines = original.splitlines(keepends=True)
    index = args.after_line
    indent = lines[index][:len(lines[index]) - len(lines[index].lstrip())]
    inserted = "".join(lines[:index] + [indent + "idtac.\n"] + lines[index:])
    records = []
    client = RocqLSPClient(args.project)
    try:
        client.open_file(args.file, original)

        def goal(label, content=None, line=index):
            process = client.proc
            started = time.monotonic()
            if content is not None:
                client.update_file(args.file, content)
            text = client.get_file_content(args.file).split("\n")[line]
            before = client.goals_at(args.file, line, len(text) - len(text.lstrip()))
            after = client.goals_at(args.file, line)
            record = {
                "case": label, "line": line + 1,
                "seconds": round(time.monotonic() - started, 3),
                "restarted": client.proc is not process,
                "before_range": before.get("range") if before else None,
                "after_range": after.get("range") if after else None,
                "errors": [d["message"] for d in client.get_diagnostics(args.file)
                           if d.get("severity") == 1],
            }
            records.append(record)
            print(json.dumps(record), flush=True)

        goal("cold")
        goal("warm")
        goal("insert tactic", inserted)
        goal("repeat inserted tactic")
        goal("next tactic", line=index + 1)
        goal("delete inserted tactic", original)
        goal("repeat after deletion")
        # A query far before the edit must also retain the cached imports.
        goal("insert then inspect line 18", inserted, line=17)
    finally:
        client.close()
    assert path.read_bytes() == original_bytes, "source changed during benchmark"
    if args.expect_incremental:
        assert all(not r["restarted"] for r in records), records
        assert all(not r["errors"] for r in records), records
    print(json.dumps({"source_sha256": hashlib.sha256(original_bytes).hexdigest(),
                      "total_seconds": round(sum(r["seconds"] for r in records), 3)}))


if __name__ == "__main__":
    main()
