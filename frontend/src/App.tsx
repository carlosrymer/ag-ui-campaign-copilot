import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { initialUi, reduce, type UiState } from "./agui/reducer";
import { Replayer, streamLive, type Recording } from "./agui/sources";
import Transcript from "./components/Transcript";
import StatePanel from "./components/StatePanel";
import WireMeter from "./components/WireMeter";
import Scrubber from "./components/Scrubber";
import "./styles.css";

const BASE = import.meta.env.BASE_URL;
/** Set VITE_AGENT_URL at build time to point the UI at a running backend. */
const LIVE_URL = (import.meta.env.VITE_AGENT_URL as string | undefined) ?? "";

interface IndexDoc {
  generated_at: string;
  recordings: Array<{
    scenario: string; label: string; model: string; file: string;
    event_count: number; duration_ms: number;
    wire_meter: { snapshot_to_delta_ratio: number; reduction_pct: number };
  }>;
}

type Theme = "auto" | "light" | "dark";

function useTheme(): [Theme, () => void] {
  const [theme, setTheme] = useState<Theme>(
    () => (localStorage.getItem("theme") as Theme) || "auto",
  );
  useEffect(() => {
    const root = document.documentElement;
    if (theme === "auto") root.removeAttribute("data-theme");
    else root.setAttribute("data-theme", theme);
    localStorage.setItem("theme", theme);
  }, [theme]);
  const cycle = () => setTheme((t) => (t === "auto" ? "light" : t === "light" ? "dark" : "auto"));
  return [theme, cycle];
}

export default function App() {
  const [theme, cycleTheme] = useTheme();
  const [index, setIndex] = useState<IndexDoc | null>(null);
  const [current, setCurrent] = useState<Recording | null>(null);
  const [ui, setUi] = useState<UiState>(initialUi());
  const [pos, setPos] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [speed, setSpeed] = useState(1);
  const [live, setLive] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [ready, setReady] = useState(false);
  const replayer = useRef<Replayer | null>(null);

  /* ----------------------------------------------------------- load index */
  useEffect(() => {
    fetch(`${BASE}recordings/index.json`)
      .then((r) => r.json())
      .then((d: IndexDoc) => {
        setIndex(d);
        return fetch(`${BASE}recordings/${d.recordings[0].file}`).then((r) => r.json());
      })
      .then(setCurrent)
      .catch(() => setErr("Could not load the recorded runs."));
  }, []);

  /* -------------------------------------------------------- build replayer */
  useEffect(() => {
    replayer.current?.destroy();
    replayer.current = null;
    setReady(false);
    if (!current || live) return;

    const r = new Replayer(current.events, (state, i, p) => {
      setUi(state); setPos(i); setPlaying(p);
    });
    r.setSpeed(speed);
    replayer.current = r;
    setUi(initialUi()); setPos(0); setPlaying(false);
    setReady(true);
    const t = window.setTimeout(() => r.play(), 500);
    return () => { window.clearTimeout(t); r.destroy(); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [current, live]);

  const pick = useCallback(async (file: string) => {
    try {
      const rec = await fetch(`${BASE}recordings/${file}`).then((r) => r.json());
      // Reset position before swapping recordings so nothing renders past the new end.
      setPos(0);
      setUi(initialUi());
      setCurrent(rec);
    } catch {
      setErr("Could not load that recording.");
    }
  }, []);

  /* ------------------------------------------------------------- live mode */
  const liveThread = useRef<string>("");
  const liveInterrupt = useRef<string | null>(null);
  const rid = () => `run_${Math.random().toString(36).slice(2, 10)}`;

  const runLive = useCallback(async (brief: string) => {
    if (!LIVE_URL) return;
    setLive(true); setErr(null);
    replayer.current?.destroy();
    liveThread.current = `thr_${Math.random().toString(36).slice(2, 10)}`;
    let s = initialUi();
    setUi(s);
    try {
      await streamLive(LIVE_URL, {
        threadId: liveThread.current, runId: rid(), state: {},
        messages: [{ id: "m1", role: "user", content: brief }],
        tools: [], context: [], forwardedProps: {},
      }, (ev) => {
        s = reduce(s, ev);
        setUi(s);
        if (ev.type === "RUN_FINISHED" && ev.outcome?.type === "interrupt") {
          liveInterrupt.current = ev.outcome.interrupts[0].id;
        }
      });
    } catch (e: any) {
      setErr(String(e?.message ?? e));
    }
  }, []);

  const decideLive = useCallback(async (decision: "approve" | "edit" | "reject") => {
    if (!LIVE_URL || !liveInterrupt.current) return;
    const interruptId = liveInterrupt.current;
    liveInterrupt.current = null;

    const proposed = ui.interrupt?.metadata?.proposed_args ?? {};
    const edits = decision === "edit"
      ? { campaign_name: `${proposed.campaign_name ?? "Campaign"} (rev. by GTM lead)` }
      : undefined;
    const payload = { decision, approver: "you", edits };

    let s = reduce(ui, {
      type: "RAW", source: "ui",
      event: { kind: "human_decision", interruptId, payload },
    });
    setUi(s);

    try {
      await streamLive(LIVE_URL, {
        threadId: liveThread.current, runId: rid(), state: {},
        messages: [], tools: [], context: [], forwardedProps: {},
        resume: [{ interruptId, status: "resolved", payload }],
      }, (ev) => { s = reduce(s, ev); setUi(s); });
    } catch (e: any) {
      setErr(String(e?.message ?? e));
    }
  }, [ui]);

  /* ------------------------------------------------------------ shortcuts */
  useEffect(() => {
    const h = (e: KeyboardEvent) => {
      const r = replayer.current;
      if (live || !r) return;
      const tag = (e.target as HTMLElement)?.tagName;
      if (tag === "INPUT" || tag === "TEXTAREA") return;
      if (e.code === "Space") { e.preventDefault(); r.toggle(); }
      if (e.code === "ArrowLeft") { e.preventDefault(); r.seek(r.index - 1); }
      if (e.code === "ArrowRight") { e.preventDefault(); r.seek(r.index + 1); }
    };
    window.addEventListener("keydown", h);
    return () => window.removeEventListener("keydown", h);
  }, [live]);

  const ratio = useMemo(
    () => (ui.deltaBytes ? ui.snapshotBaselineBytes / ui.deltaBytes : 0),
    [ui.deltaBytes, ui.snapshotBaselineBytes],
  );

  return (
    <div className="app">
      <header className="hdr">
        <div className="hdr-in">
          <div className="brand">
            <h1>Campaign Copilot</h1>
            <span className="sub">an AG-UI protocol demo</span>
          </div>
          <span className={`mode-pill${live ? " live" : ""}`}>
            <span className="dot" />
            {live ? "LIVE AGENT" : "REPLAY OF A RECORDED RUN"}
          </span>
          {LIVE_URL && !live && (
            <button className="icon-btn" onClick={() => runLive(current?.brief ?? "")}>
              ▶ run live
            </button>
          )}
          <button className="icon-btn" onClick={cycleTheme} title={`Theme: ${theme}`}>
            {theme === "auto" ? "◐ auto" : theme === "light" ? "☀ light" : "☾ dark"}
          </button>
        </div>
      </header>

      {!live && (
        <div className="notice">
          <div className="notice-in">
            <span>ℹ</span>
            <div>
              <strong>This page is a replayer, not a live agent.</strong> GitHub Pages is static, so
              no model runs here. Every event below was captured from a real run against{" "}
              <code>{current?.model ?? "Gemini"}</code> and replays at its original timing through
              the same React components the live app uses. To drive a live agent with your own key,
              run <code>docker compose up</code> — see the README.
            </div>
          </div>
        </div>
      )}

      {index && !live && (
        <div className="scen-bar">
          {index.recordings.map((r) => (
            <button
              key={r.scenario} className="scen"
              aria-pressed={current?.scenario === r.scenario}
              onClick={() => pick(r.file)}
            >
              <span className="t">{r.label}</span>
              <span className="m">
                {r.event_count} events · {r.wire_meter?.snapshot_to_delta_ratio}× lighter
              </span>
            </button>
          ))}
        </div>
      )}

      <main className="main">
        <div className="col">
          <div className="card">
            <div className="card-hd">
              <h3>Agent stream</h3>
              <span className="spacer" />
              <span style={{ fontFamily: "var(--mono)", fontSize: 11.5, color: "var(--text-dim)" }}>
                {ui.status}{ui.currentStep ? ` · ${ui.currentStep}` : ""}
              </span>
            </div>
            <div className="card-bd tight">
              <Transcript ui={ui} interactive={live} onDecide={decideLive} />
            </div>
          </div>
          {err && (
            <div className="card">
              <div className="card-bd" style={{ color: "var(--bad)", fontSize: 13.5 }}>{err}</div>
            </div>
          )}
        </div>

        <div className="col">
          <WireMeter ui={ui} />
          <StatePanel state={ui.state} />
        </div>
      </main>

      <footer className="foot">
        <div className="foot-in">
          <span>
            Recorded {current?.captured_at ?? "—"} · model <code>{current?.model ?? "—"}</code> ·{" "}
            {current?.event_count ?? 0} AG-UI events · state sync{" "}
            {ratio ? `${ratio.toFixed(2)}×` : "—"} lighter than re-sending snapshots.
          </span>
        </div>
      </footer>

      {current && !live && ready && (
        <Scrubber
          events={current.events} index={pos} playing={playing} speed={speed}
          onToggle={() => replayer.current?.toggle()}
          onSeek={(i) => replayer.current?.seek(i)}
          onSpeed={(s) => { setSpeed(s); replayer.current?.setSpeed(s); }}
        />
      )}
    </div>
  );
}
