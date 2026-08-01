/**
 * The AG-UI event reducer.
 *
 * This is the single place where protocol events become UI state, and it is the
 * reason the live app and the static replayer can share rendering: both feed the
 * exact same events through this exact same function. The replayer is not a
 * re-implementation -- it is the same reducer with a different event source.
 *
 * The reducer is pure and total: `events.reduce(reduce, initialUi())` for any prefix
 * of the stream yields the UI at that point. That is what makes the scrubber work --
 * seeking is just replaying a prefix, not unwinding side effects.
 */
import { applyPatch, type Operation } from "fast-json-patch";

export type Phase =
  | "starting" | "researching" | "segment_selected" | "benchmarking"
  | "drafting_copy" | "compliance_checked" | "revising_copy"
  | "awaiting_approval" | "publishing" | "published" | "rejected" | "done";

export interface Variant {
  id: string;
  channel_id: string;
  body: string;
  edited_by_human?: boolean;
}

export interface Allocation {
  channel_id: string;
  channel_name: string;
  budget_usd: number;
  share: number;
  basis_pipeline_roas: number;
  basis_cost_per_mql_usd: number;
  projected_mqls: number;
  projected_sqls: number;
  projected_pipeline_usd: number;
  lead_time_days: number;
}

export interface CampaignState {
  brief: string;
  phase: Phase;
  segment: { id: string; name: string } | null;
  candidate_segments: Array<Record<string, any>>;
  benchmarks: Array<Record<string, any>>;
  budget: {
    total_usd: number;
    allocations: Allocation[];
    excluded: Array<Record<string, any>>;
    projected: Record<string, number>;
  } | null;
  variants: Variant[];
  compliance: { all_passed: boolean; total_issues: number; results: any[] } | null;
  approval: {
    status: string;
    interrupt_id: string | null;
    decision: string | null;
    note: string | null;
    edits: Record<string, any> | null;
    proposed?: Record<string, any>;
  };
  published: Record<string, any> | null;
  log: string[];
}

export interface ToolCallView {
  id: string;
  name: string;
  argsText: string;
  args: Record<string, any> | null;
  result: any;
  status: "running" | "done" | "gated";
  startedAt: number | null;
  endedAt: number | null;
}

export interface MessageView {
  id: string;
  role: string;
  content: string;
  streaming: boolean;
}

export type TimelineItem =
  | { kind: "message"; id: string }
  | { kind: "tool"; id: string }
  | { kind: "gate"; id: string }
  | { kind: "decision"; id: string };

export interface InterruptView {
  id: string;
  reason: string;
  message: string | null;
  toolCallId: string | null;
  metadata: Record<string, any> | null;
}

export interface HumanDecision {
  interruptId: string;
  decision: "approve" | "edit" | "reject";
  note?: string;
  approver?: string;
  edits?: Record<string, any> | null;
}

export interface WireMeter {
  state_sync_ticks: number;
  agui_delta_bytes_total: number;
  naive_snapshot_bytes_total: number;
  bytes_saved: number;
  reduction_pct: number;
  snapshot_to_delta_ratio: number;
  per_tick: Array<{
    tick: number; label: string; patch_ops: number;
    delta_bytes: number; snapshot_baseline_bytes: number;
  }>;
  event_counts_by_type: Record<string, number>;
  total_events: number;
}

export interface UiState {
  runIds: string[];
  status: "idle" | "running" | "awaiting_approval" | "finished" | "error";
  state: CampaignState | null;
  messages: Record<string, MessageView>;
  tools: Record<string, ToolCallView>;
  timeline: TimelineItem[];
  /** The interrupt currently blocking the run, or null. */
  interrupt: InterruptView | null;
  /** Every interrupt seen this thread, kept so resolved gates stay in the transcript. */
  interrupts: Record<string, InterruptView>;
  decision: HumanDecision | null;
  meter: WireMeter | null;
  eventCounts: Record<string, number>;
  currentStep: string | null;
  error: string | null;
  /** Bytes of AG-UI state traffic seen so far, and the naive baseline, for the live gauge. */
  deltaBytes: number;
  snapshotBaselineBytes: number;
}

export function initialUi(): UiState {
  return {
    runIds: [], status: "idle", state: null, messages: {}, tools: {},
    timeline: [], interrupt: null, interrupts: {}, decision: null, meter: null,
    eventCounts: {}, currentStep: null, error: null,
    deltaBytes: 0, snapshotBaselineBytes: 0,
  };
}

const enc = new TextEncoder();
const sseBytes = (obj: unknown) => enc.encode(`data: ${JSON.stringify(obj)}\n\n`).length;

function push(timeline: TimelineItem[], item: TimelineItem): TimelineItem[] {
  const last = timeline[timeline.length - 1];
  if (last && last.kind === item.kind && last.id === item.id) return timeline;
  return [...timeline, item];
}

export function reduce(ui: UiState, event: any): UiState {
  const type = event?.type;
  if (!type) return ui;

  const counts = { ...ui.eventCounts, [type]: (ui.eventCounts[type] ?? 0) + 1 };
  const next: UiState = { ...ui, eventCounts: counts };

  switch (type) {
    case "RUN_STARTED":
      return {
        ...next,
        runIds: [...next.runIds, event.runId],
        status: "running",
        interrupt: null,
        error: null,
      };

    case "RUN_FINISHED": {
      const outcome = event.outcome;
      if (outcome?.type === "interrupt" && outcome.interrupts?.length) {
        const it = outcome.interrupts[0];
        const view: InterruptView = {
          id: it.id, reason: it.reason, message: it.message ?? null,
          toolCallId: it.toolCallId ?? null, metadata: it.metadata ?? null,
        };
        return {
          ...next,
          status: "awaiting_approval",
          interrupt: view,
          interrupts: { ...next.interrupts, [it.id]: view },
          timeline: push(next.timeline, { kind: "gate", id: it.id }),
          tools: it.toolCallId && next.tools[it.toolCallId]
            ? { ...next.tools, [it.toolCallId]: { ...next.tools[it.toolCallId], status: "gated" } }
            : next.tools,
        };
      }
      return { ...next, status: "finished", interrupt: null };
    }

    case "RUN_ERROR":
      return { ...next, status: "error", error: event.message ?? "unknown error" };

    case "STEP_STARTED":
      return { ...next, currentStep: event.stepName };
    case "STEP_FINISHED":
      return { ...next, currentStep: null };

    case "STATE_SNAPSHOT": {
      const snap = event.snapshot as CampaignState;
      return {
        ...next,
        state: snap,
        snapshotBaselineBytes: next.snapshotBaselineBytes + sseBytes(event),
        deltaBytes: next.deltaBytes + sseBytes(event),
      };
    }

    case "STATE_DELTA": {
      if (!next.state) return next;
      const ops = event.delta as Operation[];
      let updated: CampaignState;
      try {
        updated = applyPatch(next.state, ops, /*validate*/ false, /*mutate*/ false).newDocument;
      } catch {
        // Per the AG-UI guidance, a failed patch means the client should ask for a
        // fresh snapshot. In replay there is nobody to ask, so hold the last good state.
        return next;
      }
      return {
        ...next,
        state: updated,
        deltaBytes: next.deltaBytes + sseBytes(event),
        snapshotBaselineBytes:
          next.snapshotBaselineBytes + sseBytes({ type: "STATE_SNAPSHOT", snapshot: updated }),
      };
    }

    case "TEXT_MESSAGE_START":
      return {
        ...next,
        messages: {
          ...next.messages,
          [event.messageId]: { id: event.messageId, role: event.role ?? "assistant", content: "", streaming: true },
        },
        timeline: push(next.timeline, { kind: "message", id: event.messageId }),
      };

    case "TEXT_MESSAGE_CONTENT": {
      const prev = next.messages[event.messageId] ?? {
        id: event.messageId, role: "assistant", content: "", streaming: true,
      };
      return {
        ...next,
        messages: { ...next.messages, [event.messageId]: { ...prev, content: prev.content + event.delta } },
        timeline: push(next.timeline, { kind: "message", id: event.messageId }),
      };
    }

    case "TEXT_MESSAGE_END": {
      const prev = next.messages[event.messageId];
      if (!prev) return next;
      return { ...next, messages: { ...next.messages, [event.messageId]: { ...prev, streaming: false } } };
    }

    case "TOOL_CALL_START":
      return {
        ...next,
        tools: {
          ...next.tools,
          [event.toolCallId]: {
            id: event.toolCallId, name: event.toolCallName, argsText: "", args: null,
            result: null, status: "running", startedAt: event.timestamp ?? null, endedAt: null,
          },
        },
        timeline: push(next.timeline, { kind: "tool", id: event.toolCallId }),
      };

    case "TOOL_CALL_ARGS": {
      const prev = next.tools[event.toolCallId];
      if (!prev) return next;
      const argsText = prev.argsText + (event.delta ?? "");
      let args: Record<string, any> | null = null;
      try { args = JSON.parse(argsText); } catch { /* still streaming */ }
      return { ...next, tools: { ...next.tools, [event.toolCallId]: { ...prev, argsText, args } } };
    }

    case "TOOL_CALL_END":
      return next;

    case "TOOL_CALL_RESULT": {
      const prev = next.tools[event.toolCallId];
      if (!prev) return next;
      let result: any = event.content;
      try { result = JSON.parse(event.content); } catch { /* plain string */ }
      return {
        ...next,
        tools: {
          ...next.tools,
          [event.toolCallId]: { ...prev, result, status: "done", endedAt: event.timestamp ?? null },
        },
      };
    }

    case "CUSTOM":
      if (event.name === "wire_meter") return { ...next, meter: event.value as WireMeter };
      return next;

    case "RAW": {
      // The capture harness records the human's decision as a RAW event so the
      // replayer can show the gate resolving exactly as it did live.
      const inner = event.event;
      if (inner?.kind === "human_decision") {
        const p = inner.payload ?? {};
        return {
          ...next,
          decision: {
            interruptId: inner.interruptId, decision: p.decision,
            note: p.note, approver: p.approver, edits: p.edits ?? null,
          },
          timeline: push(next.timeline, { kind: "decision", id: inner.interruptId }),
        };
      }
      return next;
    }

    default:
      return next;
  }
}

export function reduceAll(events: any[]): UiState {
  return events.reduce(reduce, initialUi());
}
