import type { RecordedEvent } from "../agui/sources";

const fmt = (ms: number) => {
  const s = Math.floor(ms / 1000);
  return `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;
};

const SPEEDS = [0.5, 1, 2, 4];

export default function Scrubber({
  events, index, playing, speed, onToggle, onSeek, onSpeed,
}: {
  events: RecordedEvent[];
  index: number;
  playing: boolean;
  speed: number;
  onToggle: () => void;
  onSeek: (i: number) => void;
  onSpeed: (s: number) => void;
}) {
  const n = events.length;
  // `index` can briefly outrun `events` when the user switches to a shorter recording,
  // because React renders with the new events before the replayer effect resets position.
  const i = Math.max(0, Math.min(index, n));
  const pct = n ? (i / n) * 100 : 0;
  const nowMs = i > 0 ? (events[i - 1]?.at_ms ?? 0) : 0;
  const totalMs = n ? (events[n - 1]?.at_ms ?? 0) : 0;

  // Mark where the run hit the approval gate, so the pause is visible on the timeline.
  const gateMarks = events
    .map((e, i) => ({ e, i }))
    .filter(({ e }) => e.event.type === "RAW" && e.event.event?.kind === "human_decision")
    .map(({ i }) => (i / Math.max(n, 1)) * 100);

  return (
    <div className="scrub">
      <div className="scrub-in">
        <div className="scrub-row">
          <button className="play" onClick={onToggle} aria-label={playing ? "Pause" : "Play"}>
            {playing ? "❚❚" : "▶"}
          </button>

          <div className="track-wrap">
            <input
              type="range" className="seek" min={0} max={n} value={i}
              style={{ ["--pct" as any]: `${pct}%` }}
              onChange={(e) => onSeek(Number(e.target.value))}
              aria-label="Seek through the recorded run"
            />
            {gateMarks.map((m, i) => (
              <span className="gate-mark" style={{ left: `${m}%` }} key={i} title="human approval gate" />
            ))}
          </div>

          <span className="scrub-time">{fmt(nowMs)} / {fmt(totalMs)}</span>
          <span className="scrub-time">{i}/{n} ev</span>

          <div className="speeds">
            {SPEEDS.map((s) => (
              <button key={s} aria-pressed={speed === s} onClick={() => onSpeed(s)}>{s}×</button>
            ))}
          </div>
        </div>
        <div className="scrub-legend">
          replaying a recorded run at captured timing · ▮ marks the human-in-the-loop gate
        </div>
      </div>
    </div>
  );
}
