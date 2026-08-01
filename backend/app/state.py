"""Shared campaign state, JSON-Patch delta generation, and wire-byte metering.

This module is where the first claim under test gets measured. Every mutation to the
shared state goes through `SharedState.mutate`, which:

  1. diffs before/after into an RFC 6902 JSON Patch,
  2. emits an AG-UI STATE_DELTA carrying only that patch,
  3. records both what the delta cost on the wire AND what a naive
     "re-send the whole state every tick" baseline would have cost.

The baseline is deliberately the fair one: same number of ticks, same SSE framing,
same JSON encoder -- the only difference is delta-vs-snapshot payload.
"""

from __future__ import annotations

import copy
from typing import Any, Callable

import jsonpatch
from ag_ui.core import StateDeltaEvent, StateSnapshotEvent
from ag_ui.encoder import EventEncoder

_encoder = EventEncoder()


def sse_bytes(event) -> int:
    """Exact bytes this event occupies on the wire, SSE framing included."""
    return len(_encoder.encode(event).encode("utf-8"))


def initial_state(brief: str) -> dict[str, Any]:
    return {
        "brief": brief,
        "phase": "starting",
        "segment": None,
        "candidate_segments": [],
        "benchmarks": [],
        "budget": None,
        "variants": [],
        "compliance": None,
        "approval": {"status": "not_requested", "interrupt_id": None,
                     "decision": None, "note": None, "edits": None},
        "published": None,
        "log": [],
    }


class WireMeter:
    """Tallies delta-vs-snapshot bytes across a run."""

    def __init__(self) -> None:
        self.ticks: list[dict[str, Any]] = []
        self.event_counts: dict[str, int] = {}

    def count_event(self, event_type: str) -> None:
        self.event_counts[event_type] = self.event_counts.get(event_type, 0) + 1

    def record_tick(self, label: str, patch_ops: list[dict], delta_bytes: int,
                    snapshot_bytes: int) -> None:
        self.ticks.append({
            "tick": len(self.ticks) + 1,
            "label": label,
            "patch_ops": len(patch_ops),
            "delta_bytes": delta_bytes,
            "snapshot_baseline_bytes": snapshot_bytes,
        })

    def summary(self) -> dict[str, Any]:
        d = sum(t["delta_bytes"] for t in self.ticks)
        s = sum(t["snapshot_baseline_bytes"] for t in self.ticks)
        return {
            "state_sync_ticks": len(self.ticks),
            "agui_delta_bytes_total": d,
            "naive_snapshot_bytes_total": s,
            "bytes_saved": s - d,
            "reduction_pct": round((1 - d / s) * 100, 2) if s else None,
            "snapshot_to_delta_ratio": round(s / d, 2) if d else None,
            "per_tick": self.ticks,
            "event_counts_by_type": dict(sorted(self.event_counts.items())),
            "total_events": sum(self.event_counts.values()),
        }


class SharedState:
    """The agent's view of campaign state, synced to the UI as JSON-Patch deltas."""

    def __init__(self, brief: str, meter: WireMeter) -> None:
        self.data = initial_state(brief)
        self.meter = meter

    def snapshot_event(self) -> StateSnapshotEvent:
        """A full STATE_SNAPSHOT -- sent once, to seed the client.

        Counted as tick 0 against BOTH columns: AG-UI genuinely pays for this snapshot,
        and the naive baseline would have sent the same thing at the same moment. Only
        the ticks after it differ, which is exactly the comparison worth making.
        """
        event = StateSnapshotEvent(snapshot=copy.deepcopy(self.data))
        n = sse_bytes(event)
        self.meter.record_tick("seed snapshot", [], n, n)
        return event

    def mutate(self, label: str, fn: Callable[[dict], None]) -> StateDeltaEvent | None:
        """Apply `fn` to the state and return the STATE_DELTA describing the change.

        Returns None when the mutation was a no-op (nothing to send -- itself a win
        the naive baseline does not get, since it would re-send everything anyway).
        """
        before = copy.deepcopy(self.data)
        fn(self.data)
        patch = jsonpatch.JsonPatch.from_diff(before, self.data)
        ops = list(patch)
        if not ops:
            return None

        delta_event = StateDeltaEvent(delta=ops)
        # What the naive approach would have pushed at this same tick.
        baseline_event = StateSnapshotEvent(snapshot=copy.deepcopy(self.data))

        self.meter.record_tick(label, ops, sse_bytes(delta_event), sse_bytes(baseline_event))
        return delta_event
