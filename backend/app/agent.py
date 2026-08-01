"""The campaign copilot agent loop, expressed as a stream of AG-UI events.

The loop is an ordinary tool-calling agent. What makes it an *AG-UI* agent is that
every observable thing it does is emitted as a typed protocol event:

  RUN_STARTED / RUN_FINISHED / RUN_ERROR      run lifecycle
  STEP_STARTED / STEP_FINISHED                agent turns
  TEXT_MESSAGE_START / _CONTENT / _END        streamed assistant prose
  TOOL_CALL_START / _ARGS / _END / _RESULT    tool lifecycle, rendered as cards
  STATE_SNAPSHOT                              one seed of the shared campaign state
  STATE_DELTA                                 every subsequent state change, as JSON Patch
  CUSTOM                                      the wire-byte meter readout

The human-in-the-loop gate uses AG-UI's first-class interrupt mechanism: when the model
calls a gated tool, the run ends with a RunFinishedEvent whose outcome is a
RunFinishedInterruptOutcome carrying an Interrupt. The tool does NOT execute. The client
resumes by sending a fresh RunAgentInput whose `resume` array resolves that interrupt id.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from ag_ui.core import (
    BaseEvent,
    CustomEvent,
    Interrupt,
    ResumeEntry,
    RunErrorEvent,
    RunFinishedEvent,
    RunFinishedInterruptOutcome,
    RunFinishedSuccessOutcome,
    RunStartedEvent,
    StepFinishedEvent,
    StepStartedEvent,
    TextMessageContentEvent,
    TextMessageEndEvent,
    TextMessageStartEvent,
    ToolCallArgsEvent,
    ToolCallEndEvent,
    ToolCallResultEvent,
    ToolCallStartEvent,
)

from . import tools as toolkit
from .llm import LLM
from .state import SharedState, WireMeter

MAX_TURNS = 10

SYSTEM_PROMPT = f"""You are the campaign copilot for {toolkit.BRAND['company']}, \
working on {toolkit.BRAND['product']}: {toolkit.BRAND['one_liner']}

You plan multi-channel B2B campaigns against a real internal dataset. Work in this order \
and do not skip steps:

1. `search_segments` to find the audience. Pick ONE segment and say why in one sentence.
2. `get_channel_benchmarks` for that segment to see what has actually worked historically.
3. `allocate_budget` to split the budget. Do not invent a split yourself -- the tool applies \
minimum-spend and concentration rules you cannot see.
4. Draft 3 copy variants for the top-funded channels. Put each variant in your prose as a \
line of the exact form:
   VARIANT <id> | <channel_id> | <the copy>
   Use ids v1, v2, v3. Write in the brand tone: {', '.join(toolkit.BRAND['tone'])}. \
Never use these phrases: {', '.join(toolkit.BRAND['banned_phrases'])}.
   Ground claims in these proof points: {' / '.join(toolkit.BRAND['proof_points'])}
5. `check_copy_compliance` on those variants. If anything fails, rewrite it (emitting fresh \
VARIANT lines) and check again.
6. `publish_campaign` to go live.

CRITICAL: `publish_campaign` is irreversible and always requires human approval. The runtime \
pauses the run and asks the human for you -- just call the tool when the plan is ready. \
The human may approve, approve with edits, or reject.

If the human rejects, do NOT call `publish_campaign` again in this run. Acknowledge the \
rejection, explain what you would change, and stop.

Be concise. Cite concrete numbers from the tools rather than adjectives."""


def _now_ms() -> int:
    return int(time.time() * 1000)


def _parse_variants(text: str) -> list[dict]:
    """Pull `VARIANT <id> | <channel> | <copy>` lines out of the model's prose."""
    out = []
    for line in text.splitlines():
        line = line.strip().lstrip("*-# ").strip()
        if not line.upper().startswith("VARIANT "):
            continue
        parts = [p.strip() for p in line[len("VARIANT "):].split("|")]
        if len(parts) >= 3 and parts[0]:
            out.append({"id": parts[0], "channel_id": parts[1], "body": "|".join(parts[2:]).strip()})
    return out


@dataclass
class Session:
    """Per-thread agent session, survives across the interrupt/resume boundary."""

    thread_id: str
    brief: str
    model: str
    history: list[dict] = field(default_factory=list)
    meter: WireMeter = field(default_factory=WireMeter)
    state: SharedState | None = None
    pending: dict[str, Any] | None = None  # {interrupt_id, tool_call_id, name, args}
    turn: int = 0


class CampaignAgent:
    def __init__(self, session: Session) -> None:
        self.s = session
        self.llm = LLM(session.model)
        if self.s.state is None:
            self.s.state = SharedState(session.brief, session.meter)
            self.s.history = [self.llm.user_turn(session.brief)]

    # ---------------------------------------------------------------- helpers

    def _emit(self, event: BaseEvent) -> BaseEvent:
        event.timestamp = _now_ms()
        self.s.meter.count_event(event.type.value if hasattr(event.type, "value") else str(event.type))
        return event

    def _delta(self, label: str, fn) -> list[BaseEvent]:
        ev = self.s.state.mutate(label, fn)
        return [self._emit(ev)] if ev else []

    # ---------------------------------------------------------------- run

    async def run(self, run_id: str, resume: list[ResumeEntry] | None = None
                  ) -> AsyncIterator[BaseEvent]:
        yield self._emit(RunStartedEvent(thread_id=self.s.thread_id, run_id=run_id))

        try:
            if resume:
                async for ev in self._apply_resume(resume):
                    yield ev
                    if isinstance(ev, RunFinishedEvent):
                        return
            else:
                # Seed the client with one full snapshot; everything after is a patch.
                yield self._emit(self.s.state.snapshot_event())
                for ev in self._delta("brief accepted", lambda st: st.update({"phase": "researching"})):
                    yield ev

            async for ev in self._loop(run_id):
                yield ev

        except Exception as exc:  # noqa: BLE001 - surface any failure as a protocol event
            yield self._emit(RunErrorEvent(message=f"{type(exc).__name__}: {exc}"))

    async def _apply_resume(self, resume: list[ResumeEntry]) -> AsyncIterator[BaseEvent]:
        """Resolve the pending interrupt with the human's decision."""
        pending = self.s.pending
        if not pending:
            yield self._emit(RunErrorEvent(message="resume received but no interrupt is pending"))
            return

        entry = next((r for r in resume if r.interrupt_id == pending["interrupt_id"]), None)
        if entry is None:
            yield self._emit(RunErrorEvent(
                message=f"resume did not address pending interrupt {pending['interrupt_id']}"))
            return

        payload = entry.payload if isinstance(entry.payload, dict) else {}
        decision = "reject" if entry.status == "cancelled" else payload.get("decision", "approve")
        note = payload.get("note")
        edits = payload.get("edits") or {}
        tool_call_id = pending["tool_call_id"]
        args = dict(pending["args"])

        self.s.pending = None

        # ---- reject -------------------------------------------------------
        if decision == "reject":
            for ev in self._delta("human rejected publish", lambda st: st.update({
                "phase": "rejected",
                "approval": {**st["approval"], "status": "rejected",
                             "decision": "reject", "note": note, "edits": None},
                "log": st["log"] + [f"Human REJECTED publish. {note or ''}".strip()],
            })):
                yield ev
            result = {"status": "blocked_by_human",
                      "message": "The human rejected this campaign. It was NOT published.",
                      "human_note": note or "(no reason given)"}
            yield self._emit(ToolCallResultEvent(
                message_id=f"msg_{uuid.uuid4().hex[:8]}", tool_call_id=tool_call_id,
                content=json.dumps(result), role="tool"))
            self.s.history.append(self.llm.tool_result_turn(tool_call_id, "publish_campaign", result))
            return

        # ---- edit: human approved a modified version ----------------------
        applied_edits = None
        if decision == "edit" and edits:
            applied_edits = edits
            if "campaign_name" in edits:
                args["campaign_name"] = edits["campaign_name"]
            if "allocations" in edits:
                args["allocations"] = edits["allocations"]
            if "variants" in edits:
                # Human rewrote copy. Reflect it in shared state and in what goes live.
                by_id = {v["id"]: v for v in edits["variants"]}

                def apply_variants(st, by_id=by_id):
                    st["variants"] = [{**v, **by_id.get(v["id"], {}), "edited_by_human": v["id"] in by_id}
                                      for v in st["variants"]]
                for ev in self._delta("human edited copy", apply_variants):
                    yield ev
                args["variant_ids"] = sorted({*args.get("variant_ids", []), *by_id})

        status = "approved_with_edits" if applied_edits else "approved"
        for ev in self._delta(f"human {status}", lambda st, s=status, n=note, e=applied_edits: st.update({
            "phase": "publishing",
            "approval": {**st["approval"], "status": s, "decision": "edit" if e else "approve",
                         "note": n, "edits": e},
            "log": st["log"] + [f"Human {s.upper()} publish. {n or ''}".strip()],
        })):
            yield ev

        # Now -- and only now -- the gated tool actually executes.
        result = self._execute(pending["name"], {**args, "approved_by": payload.get("approver", "human reviewer")})
        yield self._emit(ToolCallResultEvent(
            message_id=f"msg_{uuid.uuid4().hex[:8]}", tool_call_id=tool_call_id,
            content=json.dumps(result), role="tool"))
        self.s.history.append(self.llm.tool_result_turn(tool_call_id, pending["name"], result))

        for ev in self._delta("campaign published", lambda st, r=result: st.update({
            "phase": "published", "published": r,
            "log": st["log"] + [f"Published '{r.get('campaign_name')}' "
                                f"(${r.get('committed_budget_usd', 0):,.0f})"],
        })):
            yield ev

    # ---------------------------------------------------------------- core loop

    async def _loop(self, run_id: str) -> AsyncIterator[BaseEvent]:
        while self.s.turn < MAX_TURNS:
            self.s.turn += 1
            step = f"turn-{self.s.turn}"
            yield self._emit(StepStartedEvent(step_name=step))

            msg_id = f"msg_{uuid.uuid4().hex[:8]}"
            text_open = False
            text_buf: list[str] = []
            calls: list[dict] = []

            async for chunk in self.llm.stream(SYSTEM_PROMPT, self.s.history, toolkit.TOOL_SCHEMAS):
                if chunk["type"] == "text":
                    if not text_open:
                        yield self._emit(TextMessageStartEvent(message_id=msg_id, role="assistant"))
                        text_open = True
                    text_buf.append(chunk["delta"])
                    yield self._emit(TextMessageContentEvent(message_id=msg_id, delta=chunk["delta"]))
                elif chunk["type"] == "tool_call":
                    calls.append(chunk)
                elif chunk["type"] == "turn":
                    self.s.history.append(chunk["message"])

            if text_open:
                yield self._emit(TextMessageEndEvent(message_id=msg_id))

            full_text = "".join(text_buf)
            variants = _parse_variants(full_text)
            if variants:
                def add_variants(st, vs=variants):
                    existing = {v["id"]: v for v in st["variants"]}
                    for v in vs:
                        prev = existing.get(v["id"], {})
                        # A human edit outranks anything the model re-drafts afterwards.
                        if prev.get("edited_by_human"):
                            continue
                        existing[v["id"]] = {**prev, **v, "edited_by_human": False}
                    st["variants"] = [existing[k] for k in sorted(existing)]
                for ev in self._delta("copy variants drafted", add_variants):
                    yield ev

            if not calls:
                yield self._emit(StepFinishedEvent(step_name=step))
                for ev in self._delta("run complete", lambda st: st.update(
                        {"phase": "published" if st.get("published") else
                         ("rejected" if st["approval"]["status"] == "rejected" else "done")})):
                    yield ev
                yield self._emit(CustomEvent(name="wire_meter", value=self.s.meter.summary()))
                yield self._emit(RunFinishedEvent(thread_id=self.s.thread_id, run_id=run_id,
                                                  outcome=RunFinishedSuccessOutcome()))
                return

            for call in calls:
                interrupted = False
                async for ev in self._handle_call(call, run_id):
                    yield ev
                    if isinstance(ev, RunFinishedEvent):
                        interrupted = True
                if interrupted:
                    return

            yield self._emit(StepFinishedEvent(step_name=step))

        yield self._emit(RunErrorEvent(message=f"agent exceeded {MAX_TURNS} turns"))

    async def _handle_call(self, call: dict, run_id: str) -> AsyncIterator[BaseEvent]:
        name, args, call_id = call["name"], call["args"], call["id"]

        yield self._emit(ToolCallStartEvent(tool_call_id=call_id, tool_call_name=name))
        # Gemini and Kimi both hand back complete argument objects rather than streaming
        # partial JSON, so ToolCallArgs is a single chunk here rather than many.
        yield self._emit(ToolCallArgsEvent(tool_call_id=call_id, delta=json.dumps(args)))
        yield self._emit(ToolCallEndEvent(tool_call_id=call_id))

        # ---- the gate ------------------------------------------------------
        if name in toolkit.GATED_TOOLS:
            interrupt_id = f"int_{uuid.uuid4().hex[:10]}"
            self.s.pending = {"interrupt_id": interrupt_id, "tool_call_id": call_id,
                              "name": name, "args": args}

            for ev in self._delta("approval requested", lambda st, i=interrupt_id, a=args: st.update({
                "phase": "awaiting_approval",
                "approval": {"status": "pending", "interrupt_id": i, "decision": None,
                             "note": None, "edits": None, "proposed": a},
                "log": st["log"] + [f"Agent requested approval to publish '{a.get('campaign_name')}'"],
            })):
                yield ev

            yield self._emit(CustomEvent(name="wire_meter", value=self.s.meter.summary()))
            yield self._emit(RunFinishedEvent(
                thread_id=self.s.thread_id, run_id=run_id,
                outcome=RunFinishedInterruptOutcome(
                    type="interrupt",
                    interrupts=[Interrupt(
                        id=interrupt_id,
                        reason="human_approval_required",
                        message=f"Approve publishing '{args.get('campaign_name', 'this campaign')}'?",
                        tool_call_id=call_id,
                        response_schema={
                            "type": "object",
                            "properties": {
                                "decision": {"type": "string", "enum": ["approve", "edit", "reject"]},
                                "note": {"type": "string"},
                                "edits": {"type": "object"},
                            },
                            "required": ["decision"],
                        },
                        metadata={"tool": name, "proposed_args": args},
                    )],
                )))
            return

        # ---- ordinary tool -------------------------------------------------
        result = self._execute(name, args)
        yield self._emit(ToolCallResultEvent(
            message_id=f"msg_{uuid.uuid4().hex[:8]}", tool_call_id=call_id,
            content=json.dumps(result), role="tool"))
        self.s.history.append(self.llm.tool_result_turn(call_id, name, result))

        for ev in self._project(name, args, result):
            yield ev

    def _execute(self, name: str, args: dict) -> Any:
        fn = toolkit.REGISTRY.get(name)
        if fn is None:
            return {"error": f"unknown tool '{name}'"}
        try:
            return fn(**args)
        except toolkit.ToolError as e:
            return {"error": str(e)}
        except TypeError as e:
            return {"error": f"bad arguments for {name}: {e}"}

    def _project(self, name: str, args: dict, result: Any) -> list[BaseEvent]:
        """Fold a tool result into shared state -- each fold becomes one STATE_DELTA."""
        if not isinstance(result, dict) or "error" in result:
            return []

        if name == "search_segments":
            return self._delta("segments researched", lambda st: st.update({
                "phase": "segment_selected",
                "candidate_segments": [
                    {k: s[k] for k in ("id", "name", "industry", "reachable_accounts",
                                       "avg_acv_usd", "intent_score", "match_score")}
                    for s in result["segments"]],
                "log": st["log"] + [f"Searched segments for '{args.get('query', '')}' "
                                    f"-> {result['total_matched']} matched"],
            }))

        if name == "get_channel_benchmarks":
            return self._delta("benchmarks loaded", lambda st: st.update({
                "phase": "benchmarking",
                "segment": {"id": result["segment_id"], "name": result["segment_name"]},
                "benchmarks": [c for c in result["channels"] if c.get("campaigns")],
                "log": st["log"] + [f"Aggregated {result['rows_aggregated']} historical campaigns; "
                                    f"best ROAS: {', '.join(result['best_by_pipeline_roas'])}"],
            }))

        if name == "allocate_budget":
            return self._delta("budget allocated", lambda st: st.update({
                "phase": "drafting_copy",
                "budget": {"total_usd": result["total_budget_usd"],
                           "allocations": result["allocations"],
                           "excluded": result["excluded_channels"],
                           "projected": result["projected_totals"]},
                "log": st["log"] + [f"Allocated ${result['total_budget_usd']:,.0f} across "
                                    f"{len(result['allocations'])} channels "
                                    f"(blended ROAS {result['projected_totals']['blended_pipeline_roas']}x)"],
            }))

        if name == "check_copy_compliance":
            # The variants passed to this tool are the authoritative set -- the model
            # always sends them here, whereas it only sometimes echoes them in prose.
            submitted = [v for v in (args.get("variants") or []) if isinstance(v, dict) and v.get("id")]

            def fold(st, vs=submitted, res=result):
                if vs:
                    existing = {v["id"]: v for v in st["variants"]}
                    for v in vs:
                        prev = existing.get(v["id"], {})
                        # Never let a fresh draft silently clobber a human's edit.
                        if prev.get("edited_by_human"):
                            continue
                        existing[v["id"]] = {**prev, **v, "edited_by_human": False}
                    st["variants"] = [existing[k] for k in sorted(existing)]
                st.update({
                    "phase": "compliance_checked" if res["all_passed"] else "revising_copy",
                    "compliance": {"all_passed": res["all_passed"],
                                   "total_issues": res["total_issues"],
                                   "results": res["results"]},
                    "log": st["log"] + [f"Compliance check: {res['checked']} variants, "
                                        f"{res['total_issues']} issues"],
                })

            return self._delta("copy variants + compliance", fold)

        return []
