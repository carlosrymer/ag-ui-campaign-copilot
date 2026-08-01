"""Drive real campaign-copilot runs over HTTP and record the AG-UI event streams.

This exercises the genuine protocol path -- POST a RunAgentInput, read the SSE stream,
hit the interrupt, POST a resume -- and writes every event, with its arrival offset in
milliseconds, to `recordings/<scenario>.json`. Those recordings are what the static
GitHub Pages replayer plays back.

Usage:  uv run python scripts/capture_runs.py [--base http://127.0.0.1:8000] [--only approve]
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time
import uuid

import httpx

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
OUT_DIR = ROOT / "recordings"
OUT_DIR.mkdir(exist_ok=True)

BRIEF = (
    "I need a Q3 demand-gen campaign for mid-market fintech operations leaders. "
    "Total budget is $120,000. Find the right segment, use what has actually worked for "
    "them historically, split the budget across channels, draft three copy variants, and "
    "get it published."
)

SCENARIOS = {
    "approve": {
        "label": "Human approves",
        "brief": BRIEF,
        "resume": {
            "status": "resolved",
            "payload": {"decision": "approve", "approver": "Carlos Rymer",
                        "note": "Split looks right. Ship it."},
        },
    },
    "edit": {
        "label": "Human edits the draft, then approves",
        "brief": BRIEF,
        # `edits` is filled in at capture time from the agent's actual proposal,
        # so the edit is a genuine modification of what this run really produced.
        "resume": {"status": "resolved", "payload": {"decision": "edit", "approver": "Carlos Rymer"}},
    },
    "reject": {
        "label": "Human rejects",
        "brief": BRIEF,
        "resume": {
            "status": "resolved",
            "payload": {"decision": "reject", "approver": "Carlos Rymer",
                        "note": "Legal has not cleared the fintech compliance claim yet. "
                                "Hold this until the review closes."},
        },
    },
}


def run_agent_input(thread_id: str, run_id: str, brief: str | None = None,
                    resume: list[dict] | None = None, model: str | None = None) -> dict:
    body = {
        "threadId": thread_id,
        "runId": run_id,
        "state": {},
        "messages": ([{"id": f"m_{uuid.uuid4().hex[:8]}", "role": "user", "content": brief}]
                     if brief else []),
        "tools": [],
        "context": [],
        "forwardedProps": {"model": model} if model else {},
    }
    if resume:
        body["resume"] = resume
    return body


def stream_run(client: httpx.Client, base: str, body: dict, events: list[dict],
               t0: float) -> dict | None:
    """POST a RunAgentInput, append each SSE event to `events`, return any interrupt."""
    interrupt = None
    with client.stream("POST", f"{base}/agui", json=body,
                       headers={"Accept": "text/event-stream"}, timeout=300) as resp:
        if resp.status_code != 200:
            raise SystemExit(f"server returned {resp.status_code}: {resp.read().decode()[:500]}")
        for line in resp.iter_lines():
            if not line.startswith("data: "):
                continue
            event = json.loads(line[6:])
            events.append({"at_ms": int((time.time() - t0) * 1000), "event": event})
            etype = event.get("type")
            if etype == "RUN_ERROR":
                print(f"    !! RUN_ERROR: {event.get('message')}", file=sys.stderr)
            if etype == "RUN_FINISHED":
                outcome = event.get("outcome") or {}
                if outcome.get("type") == "interrupt":
                    interrupt = outcome["interrupts"][0]
            if etype == "TOOL_CALL_START":
                print(f"    -> tool {event['toolCallName']}")
    return interrupt


def build_edits(current_variants: list[dict], proposed: dict) -> dict:
    """Construct a realistic human edit from what the agent actually proposed."""
    edits: dict = {"campaign_name": f"{proposed.get('campaign_name', 'Q3 Campaign')} (rev. by GTM lead)"}

    # Shift 30% of the largest channel's budget into the second largest -- the kind of
    # correction a human planner actually makes.
    allocs = [dict(a) for a in proposed.get("allocations", [])]
    if len(allocs) >= 2:
        allocs.sort(key=lambda a: a.get("budget_usd", 0), reverse=True)
        move = round(allocs[0]["budget_usd"] * 0.30, 2)
        allocs[0]["budget_usd"] = round(allocs[0]["budget_usd"] - move, 2)
        allocs[1]["budget_usd"] = round(allocs[1]["budget_usd"] + move, 2)
        edits["allocations"] = allocs

    # Rewrite the first variant the agent actually drafted.
    if current_variants:
        first = sorted(current_variants, key=lambda v: v.get("id", ""))[0]
        edits["variants"] = [{
            "id": first["id"],
            "channel_id": first.get("channel_id", ""),
            "body": "Your CRM lists 4,820 fintech accounts. Northwind Signal tells you which "
                    "40 to call today. Halden Logistics cut pipeline review prep from 6 hours "
                    "to 40 minutes.",
        }]
    return edits


def resolve_model(base: str, override: str | None) -> str:
    """Ask the server which model it is actually running.

    The recordings are evidence, so the model they name has to be the model that ran --
    never a client-side default that might disagree with the server's configuration.
    """
    if override:
        return override
    with httpx.Client() as c:
        return c.get(f"{base}/health", timeout=30).json()["model"]


def capture(base: str, name: str, spec: dict, model: str | None) -> dict:
    print(f"\n=== scenario: {name} ({spec['label']}) ===")
    thread_id = f"thr_{name}_{uuid.uuid4().hex[:8]}"
    events: list[dict] = []
    t0 = time.time()

    with httpx.Client() as client:
        print("  run 1: initial")
        interrupt = stream_run(
            client, base, run_agent_input(thread_id, f"run_{uuid.uuid4().hex[:8]}",
                                          brief=spec["brief"], model=model), events, t0)
        if not interrupt:
            raise SystemExit(f"scenario '{name}' never hit the approval interrupt -- "
                             "the agent finished without calling publish_campaign")

        print(f"  interrupt: {interrupt['id']} ({interrupt['reason']})")
        proposed = (interrupt.get("metadata") or {}).get("proposed_args", {})

        payload = dict(spec["resume"]["payload"])
        if payload.get("decision") == "edit":
            # Read the live thread state so the edit targets the variants this run
            # genuinely produced, rather than guessing from the patch stream.
            live = client.get(f"{base}/threads/{thread_id}/state", timeout=30).json()
            payload["edits"] = build_edits((live.get("state") or {}).get("variants", []), proposed)
            payload["note"] = ("Moved 30% off the top channel into the runner-up and "
                               "rewrote variant 1 to lead with the number.")

        # Mark where the human paused, so the replayer can hold the gate realistically.
        events.append({"at_ms": int((time.time() - t0) * 1000),
                       "event": {"type": "RAW", "source": "recorder",
                                 "event": {"kind": "human_decision", "interruptId": interrupt["id"],
                                           "payload": payload}}})
        time.sleep(1.0)

        print(f"  run 2: resume with decision={payload['decision']}")
        resume = [{"interruptId": interrupt["id"], "status": spec["resume"]["status"],
                   "payload": payload}]
        stream_run(client, base,
                   run_agent_input(thread_id, f"run_{uuid.uuid4().hex[:8]}", resume=resume),
                   events, t0)

        final = client.get(f"{base}/threads/{thread_id}/state", timeout=30).json()

    counts: dict[str, int] = {}
    for rec in events:
        t = rec["event"].get("type", "?")
        counts[t] = counts.get(t, 0) + 1

    recording = {
        "scenario": name,
        "label": spec["label"],
        "brief": spec["brief"],
        "model": model,
        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "thread_id": thread_id,
        "duration_ms": events[-1]["at_ms"] if events else 0,
        "event_count": len(events),
        "event_counts_by_type": dict(sorted(counts.items())),
        "final_state": final.get("state"),
        "wire_meter": final.get("meter"),
        "events": events,
    }

    path = OUT_DIR / f"{name}.json"
    path.write_text(json.dumps(recording, indent=2) + "\n")
    m = recording["wire_meter"] or {}
    print(f"  wrote {path.relative_to(ROOT)}  ({len(events)} events, {recording['duration_ms']}ms)")
    print(f"  wire: delta {m.get('agui_delta_bytes_total')}B vs snapshot "
          f"{m.get('naive_snapshot_bytes_total')}B -> {m.get('snapshot_to_delta_ratio')}x "
          f"({m.get('reduction_pct')}% less)")
    print(f"  final phase: {(final.get('state') or {}).get('phase')}")
    return recording


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    ap.add_argument("--only", default=None, help="capture a single scenario")
    ap.add_argument("--model", default=None)
    args = ap.parse_args()

    names = [args.only] if args.only else list(SCENARIOS)
    model = resolve_model(args.base, args.model)
    print(f"capturing against model: {model}")
    index = []
    for n in names:
        rec = capture(args.base, n, SCENARIOS[n], model)
        index.append({
            "scenario": rec["scenario"], "label": rec["label"], "model": rec["model"],
            "captured_at": rec["captured_at"], "event_count": rec["event_count"],
            "duration_ms": rec["duration_ms"], "wire_meter": rec["wire_meter"],
            "event_counts_by_type": rec["event_counts_by_type"],
            "file": f"{rec['scenario']}.json",
        })

    if not args.only:
        (OUT_DIR / "index.json").write_text(json.dumps(
            {"generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
             "recordings": index}, indent=2) + "\n")
        print(f"\nwrote {(OUT_DIR / 'index.json').relative_to(ROOT)}")


if __name__ == "__main__":
    main()
