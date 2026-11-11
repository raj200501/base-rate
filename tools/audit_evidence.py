"""Evidence that the label auditors used no tools (prereg/0002 label audit, docs/DATA.md).

    python tools/audit_evidence.py --journal <workflow transcript dir>/journal.jsonl

The blind label audit runs as a Claude Code workflow whose auditor agents get
the audit packet in their prompt and return their answers as structured
output. The workflow's journal records, for each agent, the tools its
transcript used, and its transcript file sits beside the journal. This tool
writes runs/audit/evidence.json: one entry per auditor agent (label ``audit:<n>``)
with those tools and the SHA-256 of its transcript, and the auditors' answers
as runs/audit/answers.jsonl, in packet order, exactly as the agents returned
them. tools/label_audit.py score refuses the audit if any auditor used a tool
other than returning its answers.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / "runs" / "audit"


def _value(raw: Any) -> Any:
    """A journal field that may hold a Python literal written as a string."""
    if isinstance(raw, str):
        try:
            return ast.literal_eval(raw)
        except (ValueError, SyntaxError):
            return raw
    return raw


def transcript_calls(transcript: Path) -> tuple[list[str], list[str]]:
    """(tool names, files read) from every tool call an agent's transcript records."""
    tools: list[str] = []
    reads: list[str] = []
    for line in transcript.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        entry = json.loads(line)
        content = (entry.get("message") or {}).get("content")
        for block in content if isinstance(content, list) else []:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                tools.append(str(block.get("name")))
                if block.get("name") == "Read":
                    reads.append(str((block.get("input") or {}).get("file_path", "")))
    return tools, reads


def read_journal(journal: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(evidence entries, answers) for every finished auditor agent in a workflow journal."""
    started: dict[str, dict[str, Any]] = {}
    evidence: list[dict[str, Any]] = []
    answers: list[dict[str, Any]] = []
    for line in journal.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        entry = json.loads(line)
        if entry.get("type") == "started":
            started[entry["key"]] = entry
        elif entry.get("type") == "result" and entry.get("key") in started:
            begin = started[entry["key"]]
            label = str(begin.get("label", ""))
            if not label.startswith("audit:"):
                continue
            agent_id = str(entry.get("agentId") or begin.get("agentId"))
            transcript = journal.parent / f"agent-{agent_id}.jsonl"
            if not transcript.is_file():
                raise SystemExit(f"no transcript for {label} ({transcript.name})")
            listed = _value(entry.get("toolsUsed", []))
            if not isinstance(listed, list):
                raise SystemExit(f"{label}: unreadable tool list {listed!r}")
            calls, reads = transcript_calls(transcript)
            evidence.append(
                {
                    "label": label,
                    "agent_id": agent_id,
                    # the journal's list and the transcript's own calls, together
                    "tools_used": sorted({str(t) for t in listed} | set(calls)),
                    "reads": reads,
                    "transcript_sha256": hashlib.sha256(transcript.read_bytes()).hexdigest(),
                }
            )
            result = _value(entry.get("result"))
            answers.extend((result or {}).get("answers", []) if isinstance(result, dict) else [])
    return evidence, answers


def main(argv: list[str] | None = None) -> int:
    """Command-line entry point."""
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--journal", type=Path, required=True)
    ap.add_argument("--out", type=Path, default=WORK)
    args = ap.parse_args(argv)
    evidence, answers = read_journal(args.journal)
    if not evidence:
        raise SystemExit("the journal holds no finished auditor agents")
    packet = [json.loads(ln) for ln in (args.out / "packet.jsonl").read_text().splitlines() if ln]
    order = {item["audit_id"]: i for i, item in enumerate(packet)}
    unknown = sorted({a.get("audit_id") for a in answers} - set(order), key=str)
    if unknown:
        raise SystemExit(f"answers for audit ids not in the packet: {unknown[:3]}")
    ids = [a["audit_id"] for a in answers]
    twice = sorted({i for i in ids if ids.count(i) > 1})
    if twice:
        raise SystemExit(f"audit ids answered more than once: {twice[:3]}")
    answers.sort(key=lambda a: order[a["audit_id"]])
    packet_sha = hashlib.sha256((args.out / "packet.jsonl").read_bytes()).hexdigest()
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "evidence.json").write_text(
        json.dumps(
            {
                "workflow_run": args.journal.parent.name,
                "packet_sha256": packet_sha,
                "agents": evidence,
            },
            indent=1,
        )
        + "\n"
    )
    (args.out / "answers.jsonl").write_text(
        "".join(json.dumps(a, sort_keys=True) + "\n" for a in answers)
    )
    print(f"{len(evidence)} auditor agents, {len(answers)} answers; wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
