import type { UiState } from "../agui/reducer";

const kb = (n: number) => `${(n / 1024).toFixed(1)} KB`;

/**
 * The measurement panel.
 *
 * `deltaBytes` is what AG-UI actually put on the wire for state sync (one
 * STATE_SNAPSHOT to seed, then STATE_DELTA patches). `snapshotBaselineBytes` is what
 * a naive implementation would have sent if it re-serialised the whole state object
 * at every one of those same ticks. Both are measured with identical SSE framing and
 * the same JSON encoder, so the only variable is delta-vs-snapshot.
 */
export default function WireMeter({ ui }: { ui: UiState }) {
  const delta = ui.deltaBytes;
  const naive = ui.snapshotBaselineBytes;
  const ratio = delta > 0 ? naive / delta : 0;
  const saved = Math.max(0, naive - delta);
  const pct = naive > 0 ? (1 - delta / naive) * 100 : 0;
  const max = Math.max(naive, delta, 1);

  const counts = Object.entries(ui.eventCounts).sort((a, b) => b[1] - a[1]);
  const maxCount = counts.length ? counts[0][1] : 1;

  return (
    <div className="card">
      <div className="card-hd">
        <h3>State sync on the wire</h3>
        <span className="spacer" />
        <span style={{ fontFamily: "var(--mono)", fontSize: 11.5, color: "var(--text-dim)" }}>
          {ui.eventCounts.STATE_DELTA ?? 0} patches
        </span>
      </div>
      <div className="card-bd">
        <div className="meter-hero">
          <span className="big">{ratio ? `${ratio.toFixed(2)}×` : "—"}</span>
          <span className="lbl">less traffic than re-sending state</span>
        </div>
        <div className="meter-sub">
          {saved > 0
            ? `${kb(saved)} saved (${pct.toFixed(1)}%) across ${ui.eventCounts.STATE_DELTA ?? 0} state changes.`
            : "Measuring…"}
        </div>

        <div className="mbar naive">
          <div className="r">
            <span className="k">Naive: full snapshot every tick</span>
            <span className="v">{kb(naive)}</span>
          </div>
          <div className="track"><i style={{ width: `${(naive / max) * 100}%` }} /></div>
        </div>
        <div className="mbar agui">
          <div className="r">
            <span className="k">AG-UI: snapshot once, then JSON-Patch deltas</span>
            <span className="v">{kb(delta)}</span>
          </div>
          <div className="track"><i style={{ width: `${(delta / max) * 100}%` }} /></div>
        </div>

        {counts.length > 0 && (
          <>
            <div className="kv" style={{ marginTop: 16, marginBottom: 6 }}>events by type</div>
            <table className="evt-table">
              <tbody>
                {counts.map(([t, n]) => (
                  <tr key={t}>
                    <td className="n">{n}</td>
                    <td className="t">{t}</td>
                    <td className="b">
                      {/* Square-root scale: per-token TEXT_MESSAGE_CONTENT outnumbers
                          everything else by ~30x, which flattens a linear bar to nothing. */}
                      <div className="minibar"
                           style={{ width: `${Math.max(2, Math.sqrt(n / maxCount) * 100)}%` }} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </>
        )}
      </div>
    </div>
  );
}
