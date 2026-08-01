import type { CampaignState } from "../agui/reducer";

const usd0 = (n?: number | null) =>
  n == null ? "—" : n.toLocaleString("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 0 });

const PHASES: Array<[string, string]> = [
  ["researching", "research"],
  ["benchmarking", "benchmarks"],
  ["drafting_copy", "budget"],
  ["compliance_checked", "copy"],
  ["awaiting_approval", "approval"],
  ["published", "live"],
];

export default function StatePanel({ state }: { state: CampaignState | null }) {
  if (!state) {
    return (
      <div className="card">
        <div className="card-hd"><h3>Shared state</h3></div>
        <div className="empty">Waiting for STATE_SNAPSHOT…</div>
      </div>
    );
  }

  const order = PHASES.map(([p]) => p);
  const activeIdx = order.indexOf(state.phase);
  const rejected = state.phase === "rejected";

  return (
    <div className="card">
      <div className="card-hd">
        <h3>Shared state</h3>
        <span className="spacer" />
        <span style={{ fontFamily: "var(--mono)", fontSize: 11.5, color: "var(--text-dim)" }}>
          via STATE_DELTA
        </span>
      </div>

      <div className="section">
        <div className="phase-row">
          {PHASES.map(([p, label], i) => {
            const cls = rejected && i >= 4 ? "" : i < activeIdx ? "done" : i === activeIdx ? "now" : "";
            return <span className={`phase ${cls}`} key={p}>{label}</span>;
          })}
          {rejected && <span className="phase" style={{ color: "var(--bad)", background: "var(--bad-soft)" }}>rejected</span>}
        </div>
      </div>

      {state.published && (
        <div className="section">
          <div className="pub-banner">
            <span>✓</span>
            <div>
              <strong>{state.published.campaign_name}</strong> is live —{" "}
              {usd0(state.published.committed_budget_usd)} across{" "}
              {state.published.flight_channels?.length} channels, approved by{" "}
              {state.published.approved_by}.
            </div>
          </div>
        </div>
      )}
      {rejected && (
        <div className="section">
          <div className="pub-banner blocked">
            <span>⊘</span>
            <div>Publish blocked by the human. Nothing went live.</div>
          </div>
        </div>
      )}

      {state.segment && (
        <div className="section">
          <h4>Segment</h4>
          <div className="seg-name">{state.segment.name}</div>
          {(() => {
            const c = state.candidate_segments.find((s) => s.id === state.segment?.id);
            if (!c) return null;
            return (
              <div className="stat-grid">
                <div className="stat"><div className="l">Accounts</div><div className="v">{c.reachable_accounts?.toLocaleString()}</div></div>
                <div className="stat"><div className="l">Avg ACV</div><div className="v">{usd0(c.avg_acv_usd)}</div></div>
                <div className="stat"><div className="l">Intent</div><div className="v">{c.intent_score}</div></div>
              </div>
            );
          })()}
        </div>
      )}

      {state.budget && (
        <div className="section">
          <h4>Budget split — {usd0(state.budget.total_usd)}</h4>
          {state.budget.allocations.map((a) => (
            <div className="alloc" key={a.channel_id}>
              <div className="alloc-top">
                <span className="n">{a.channel_name}</span>
                <span className="spacer" style={{ marginLeft: "auto" }} />
                <span className="amt">{usd0(a.budget_usd)}</span>
                <span className="pct">{(a.share * 100).toFixed(1)}%</span>
              </div>
              <div className="bar"><i style={{ width: `${a.share * 100}%` }} /></div>
              <div className="why">
                {a.basis_pipeline_roas}x historical ROAS · {usd0(a.basis_cost_per_mql_usd)}/MQL ·{" "}
                {Math.round(a.projected_mqls)} MQLs projected
              </div>
            </div>
          ))}
          {state.budget.excluded?.length > 0 && (
            <div className="why" style={{ marginTop: 8, fontFamily: "var(--mono)", fontSize: 11, color: "var(--text-faint)" }}>
              excluded: {state.budget.excluded.map((e: any) => e.channel_id).join(", ")} (below min viable spend)
            </div>
          )}
          <div className="stat-grid">
            <div className="stat"><div className="l">Proj. MQLs</div><div className="v">{state.budget.projected.mqls}</div></div>
            <div className="stat"><div className="l">Proj. SQLs</div><div className="v">{state.budget.projected.sqls}</div></div>
            <div className="stat"><div className="l">Pipeline</div><div className="v">{usd0(state.budget.projected.pipeline_usd)}</div></div>
            <div className="stat"><div className="l">Blended ROAS</div><div className="v">{state.budget.projected.blended_pipeline_roas}x</div></div>
          </div>
        </div>
      )}

      {state.variants.length > 0 && (
        <div className="section">
          <h4>
            Copy variants
            {state.compliance && (
              <span style={{
                marginLeft: 8, textTransform: "none", letterSpacing: 0,
                color: state.compliance.all_passed ? "var(--ok)" : "var(--bad)",
              }}>
                {state.compliance.all_passed
                  ? "· compliance passed"
                  : `· ${state.compliance.total_issues} issues`}
              </span>
            )}
          </h4>
          {state.variants.map((v) => (
            <div className={`variant${v.edited_by_human ? " edited" : ""}`} key={v.id}>
              <div className="vh">
                <span className="vid">{v.id}</span>
                <span className="vch">{v.channel_id}</span>
                {v.edited_by_human && <span className="vbadge">human edit</span>}
              </div>
              <div className="vb">{v.body}</div>
            </div>
          ))}
        </div>
      )}

      {state.log.length > 0 && (
        <div className="section">
          <h4>Activity</h4>
          <ul className="log">
            {state.log.map((l, i) => <li key={i}>{l}</li>)}
          </ul>
        </div>
      )}
    </div>
  );
}
