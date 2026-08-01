import { useState } from "react";
import type { HumanDecision, InterruptView, MessageView, ToolCallView, UiState } from "../agui/reducer";

const usd = (n: number) =>
  n?.toLocaleString("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 0 }) ?? "-";

/** Inline markdown: **bold** and `code`. Deliberately tiny -- the models only use these. */
function inline(text: string, keyBase: string) {
  const out: any[] = [];
  const re = /\*\*([^*]+)\*\*|`([^`]+)`/g;
  let last = 0;
  let m: RegExpExecArray | null;
  while ((m = re.exec(text))) {
    if (m.index > last) out.push(text.slice(last, m.index));
    if (m[1]) out.push(<strong key={`${keyBase}b${m.index}`}>{m[1]}</strong>);
    else out.push(<code key={`${keyBase}c${m.index}`} className="icode">{m[2]}</code>);
    last = m.index + m[0].length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

const isTableRow = (l: string) => l.trim().startsWith("|") && l.trim().endsWith("|");
const isDivider = (l: string) => /^\|[\s:|-]+\|$/.test(l.trim());
const cells = (l: string) => l.trim().slice(1, -1).split("|").map((c) => c.trim());

/**
 * Render an assistant message.
 *
 * The models emit light markdown unprompted -- bold, and pipe tables of channel
 * benchmarks. Showing that as raw syntax looked broken, so this handles exactly the
 * subset they actually produce, plus the `VARIANT id | channel | copy` convention
 * the system prompt asks for.
 */
function Message({ m }: { m: MessageView }) {
  const lines = m.content.split("\n");
  const blocks: any[] = [];
  let i = 0;

  while (i < lines.length) {
    const line = lines[i];
    const t = line.trim().replace(/^[*\-#\s]+/, "");

    if (/^VARIANT\s/i.test(t)) {
      const parts = t.slice(8).split("|").map((p) => p.trim());
      if (parts.length >= 3) {
        blocks.push(
          <span className="vline" key={`v${i}`}>
            <b>{parts[0]} · {parts[1]}</b>
            <br />
            {parts.slice(2).join("|")}
          </span>,
        );
        i++;
        continue;
      }
    }

    if (isTableRow(line) && i + 1 < lines.length && isDivider(lines[i + 1])) {
      const head = cells(line);
      const body: string[][] = [];
      let j = i + 2;
      while (j < lines.length && isTableRow(lines[j]) && !isDivider(lines[j])) {
        body.push(cells(lines[j]));
        j++;
      }
      blocks.push(
        <div className="mdtable-wrap" key={`t${i}`}>
          <table className="mdtable">
            <thead>
              <tr>{head.map((h, k) => <th key={k}>{inline(h, `h${i}${k}`)}</th>)}</tr>
            </thead>
            <tbody>
              {body.map((row, r) => (
                <tr key={r}>{row.map((c, k) => <td key={k}>{inline(c, `c${i}${r}${k}`)}</td>)}</tr>
              ))}
            </tbody>
          </table>
        </div>,
      );
      i = j;
      continue;
    }

    blocks.push(
      <span key={`l${i}`}>
        {inline(line, `i${i}`)}
        {i < lines.length - 1 ? "\n" : ""}
      </span>,
    );
    i++;
  }

  return (
    <div className="msg">
      {blocks}
      {m.streaming && <span className="cursor" />}
    </div>
  );
}

const SUMMARIES: Record<string, (a: any, r: any) => string> = {
  search_segments: (a, r) => r ? `"${a?.query ?? ""}" → ${r.total_matched} matched` : `"${a?.query ?? ""}"`,
  get_channel_benchmarks: (a, r) =>
    r ? `${r.rows_aggregated} campaigns → best: ${(r.best_by_pipeline_roas ?? []).join(", ")}` : a?.segment_id ?? "",
  allocate_budget: (a, r) =>
    r ? `${usd(r.total_budget_usd)} → ${r.allocations?.length} channels @ ${r.projected_totals?.blended_pipeline_roas}x`
      : usd(a?.total_budget_usd),
  check_copy_compliance: (_a, r) =>
    r ? `${r.checked} variants → ${r.total_issues} issues` : "linting copy…",
  publish_campaign: (a) => `${a?.campaign_name ?? "campaign"} — needs approval`,
};

function ToolCard({ t }: { t: ToolCallView }) {
  const [open, setOpen] = useState(false);
  const summary = SUMMARIES[t.name]?.(t.args, t.result) ?? "";
  return (
    <div className={`tool${t.status === "gated" ? " gated" : ""}`}>
      <button className="tool-hd" onClick={() => setOpen((o) => !o)} aria-expanded={open}>
        <span className={`chev${open ? " open" : ""}`}>▶</span>
        <span className="tool-name">{t.name}</span>
        <span className="tool-summary">{summary}</span>
        <span className="spacer" />
        {t.status === "running" && <span className="spinner" aria-label="running" />}
        {t.status === "done" && <span className="tick" aria-label="complete">✓</span>}
        {t.status === "gated" && <span className="lock" aria-label="awaiting approval">⏸ held</span>}
      </button>
      {open && (
        <div className="tool-bd">
          <div className="kv">arguments</div>
          <pre className="json">{JSON.stringify(t.args ?? t.argsText, null, 2)}</pre>
          {t.result != null && (
            <>
              <div className="kv">result</div>
              <pre className="json">{JSON.stringify(t.result, null, 2)}</pre>
            </>
          )}
          {t.status === "gated" && (
            <div className="kv" style={{ color: "var(--gate)" }}>
              not executed — run interrupted pending human approval
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function Gate({
  interrupt, decision, onDecide, interactive,
}: {
  interrupt: InterruptView;
  decision: HumanDecision | null;
  onDecide?: (d: "approve" | "edit" | "reject") => void;
  interactive: boolean;
}) {
  const proposed = interrupt.metadata?.proposed_args ?? {};
  const total = (proposed.allocations ?? []).reduce(
    (s: number, a: any) => s + (a.budget_usd ?? 0), 0);
  return (
    <div className={`gate${decision ? " settled" : ""}`}>
      <div className="gate-hd">
        <span style={{ fontSize: 16 }}>⏸</span>
        <h4>Human approval required</h4>
      </div>
      <p className="gate-q">{interrupt.message}</p>
      <div className="gate-meta">
        <span>tool: {interrupt.metadata?.tool}</span>
        <span>budget: {usd(total)}</span>
        <span>channels: {(proposed.allocations ?? []).length}</span>
        <span>interrupt: {interrupt.id}</span>
      </div>
      {decision ? (
        <div className="gate-waiting">
          resolved → <strong>{decision.decision}</strong>
        </div>
      ) : interactive ? (
        <div className="gate-actions">
          <button className="btn approve" onClick={() => onDecide?.("approve")}>Approve &amp; publish</button>
          <button className="btn edit" onClick={() => onDecide?.("edit")}>Approve with edits</button>
          <button className="btn reject" onClick={() => onDecide?.("reject")}>Reject</button>
        </div>
      ) : (
        <div className="gate-waiting">
          <span className="spinner" /> run halted — waiting on the human…
        </div>
      )}
    </div>
  );
}

function Verdict({ d }: { d: HumanDecision }) {
  const title = d.decision === "approve" ? "Approved" : d.decision === "edit" ? "Approved with edits" : "Rejected";
  const edits = d.edits ?? {};
  return (
    <div className={`verdict ${d.decision}`}>
      <h4>{title}</h4>
      {d.note && <p>“{d.note}”</p>}
      {d.decision === "edit" && (
        <ul>
          {edits.campaign_name && <li>renamed → <em>{edits.campaign_name}</em></li>}
          {edits.allocations && <li>budget split adjusted by hand</li>}
          {edits.variants && <li>{edits.variants.length} copy variant rewritten</li>}
        </ul>
      )}
      <div className="who">— {d.approver ?? "human reviewer"}</div>
    </div>
  );
}

export default function Transcript({
  ui, interactive, onDecide,
}: {
  ui: UiState;
  interactive: boolean;
  onDecide?: (d: "approve" | "edit" | "reject") => void;
}) {
  if (!ui.state && ui.timeline.length === 0) {
    return <div className="empty">No events yet. Press play to replay a recorded run.</div>;
  }
  return (
    <div className="transcript">
      {ui.state?.brief && (
        <div className="brief-card">
          <div className="who">Brief</div>
          {ui.state.brief}
        </div>
      )}
      {ui.timeline.map((item, i) => {
        if (item.kind === "message") {
          const m = ui.messages[item.id];
          return m ? <Message m={m} key={`m${item.id}${i}`} /> : null;
        }
        if (item.kind === "tool") {
          const t = ui.tools[item.id];
          return t ? <ToolCard t={t} key={`t${item.id}`} /> : null;
        }
        if (item.kind === "gate") {
          // Read from the interrupt history, not the active slot: once the run resumes
          // the gate is no longer blocking, but it must stay in the transcript.
          const it = ui.interrupts[item.id];
          if (!it) return null;
          const isActive = ui.interrupt?.id === item.id;
          return (
            <Gate key={`g${item.id}`} interrupt={it} decision={ui.decision}
                  onDecide={onDecide} interactive={interactive && isActive} />
          );
        }
        if (item.kind === "decision" && ui.decision) {
          return <Verdict d={ui.decision} key={`d${item.id}`} />;
        }
        return null;
      })}
      {ui.error && (
        <div className="verdict reject">
          <h4>Run error</h4>
          <p>{ui.error}</p>
        </div>
      )}
    </div>
  );
}
