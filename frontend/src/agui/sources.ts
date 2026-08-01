/**
 * Event sources.
 *
 * `streamLive` talks to the Python AG-UI server over SSE.
 * `Replayer` plays a recorded event array back at its captured timing.
 *
 * Both hand raw AG-UI events to the same reducer, so the rendering path is identical.
 */
import type { UiState } from "./reducer";
import { reduce, initialUi } from "./reducer";

export interface RecordedEvent {
  at_ms: number;
  event: any;
}

export interface Recording {
  scenario: string;
  label: string;
  brief: string;
  model: string;
  captured_at: string;
  thread_id: string;
  duration_ms: number;
  event_count: number;
  event_counts_by_type: Record<string, number>;
  final_state: any;
  wire_meter: any;
  events: RecordedEvent[];
}

/* ------------------------------------------------------------------ live */

export interface RunAgentInputLike {
  threadId: string;
  runId: string;
  state: unknown;
  messages: Array<{ id: string; role: string; content: string }>;
  tools: unknown[];
  context: unknown[];
  forwardedProps: Record<string, unknown>;
  resume?: Array<{ interruptId: string; status: "resolved" | "cancelled"; payload?: unknown }>;
}

/** POST a RunAgentInput and yield each AG-UI event as it arrives. */
export async function streamLive(
  baseUrl: string,
  body: RunAgentInputLike,
  onEvent: (e: any) => void,
  signal?: AbortSignal,
): Promise<void> {
  const resp = await fetch(`${baseUrl}/agui`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
    body: JSON.stringify(body),
    signal,
  });
  if (!resp.ok || !resp.body) {
    throw new Error(`agent server returned ${resp.status}: ${await resp.text()}`);
  }

  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";

  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    // SSE frames are separated by a blank line.
    let idx: number;
    while ((idx = buf.indexOf("\n\n")) !== -1) {
      const frame = buf.slice(0, idx);
      buf = buf.slice(idx + 2);
      for (const line of frame.split("\n")) {
        if (!line.startsWith("data: ")) continue;
        try { onEvent(JSON.parse(line.slice(6))); } catch { /* ignore malformed frame */ }
      }
    }
  }
}

/* ---------------------------------------------------------------- replay */

/**
 * Drives a recording forward in wall-clock time.
 *
 * Seeking recomputes state by re-reducing the prefix from scratch. That is O(n) per
 * seek, but n is a few hundred events and the reducer is pure, so it is both instant
 * and exactly consistent with what live playback would have produced.
 */
export class Replayer {
  private timer: number | null = null;
  private startWall = 0;
  private startOffset = 0;

  index = 0;
  playing = false;
  speed = 1;

  private events: RecordedEvent[];
  private onState: (ui: UiState, index: number, playing: boolean) => void;
  /** Extra dwell (ms) inserted where the human was deciding, so the gate is readable. */
  private gateHoldMs: number;

  constructor(
    events: RecordedEvent[],
    onState: (ui: UiState, index: number, playing: boolean) => void,
    gateHoldMs = 1400,
  ) {
    this.events = events;
    this.onState = onState;
    this.gateHoldMs = gateHoldMs;
    this.buildSchedule();
  }

  get length() { return this.events.length; }
  get durationMs() { return this.events.length ? this.events[this.events.length - 1].at_ms : 0; }
  get currentMs() { return this.index > 0 ? this.events[this.index - 1].at_ms : 0; }

  /**
   * State after the first `i` events.
   *
   * Naively this is a fold from zero every frame, which is O(n) per repaint and
   * O(n^2) across a run -- fine for 100 events, not for the ~1,700 a token-streaming
   * model produces. Two caches fix it without giving up purity: the last computed
   * position (so forward playback is O(1) amortised) and periodic keyframes (so a
   * backward scrub restarts from a nearby checkpoint rather than from zero).
   */
  private lastIndex = 0;
  private lastUi: UiState = initialUi();
  private keyframes = new Map<number, UiState>([[0, initialUi()]]);
  private static KEYFRAME_EVERY = 100;

  private uiAt(i: number): UiState {
    let start = 0;
    let ui = initialUi();

    if (this.lastIndex <= i) {
      start = this.lastIndex;
      ui = this.lastUi;
    } else {
      // Seeking backwards: resume from the nearest keyframe at or before i.
      let best = 0;
      for (const k of this.keyframes.keys()) if (k <= i && k > best) best = k;
      start = best;
      ui = this.keyframes.get(best)!;
    }

    for (let k = start; k < i; k++) {
      ui = reduce(ui, this.events[k].event);
      const at = k + 1;
      if (at % Replayer.KEYFRAME_EVERY === 0 && !this.keyframes.has(at)) {
        this.keyframes.set(at, ui);
      }
    }

    this.lastIndex = i;
    this.lastUi = ui;
    return ui;
  }

  private emit() { this.onState(this.uiAt(this.index), this.index, this.playing); }

  /** Precomputed schedule: capture time plus accumulated human-decision dwell. */
  private schedule: number[] = [];

  private buildSchedule() {
    let extra = 0;
    this.schedule = this.events.map(({ at_ms, event }) => {
      const at = at_ms + extra;
      if (event.type === "RAW" && event.event?.kind === "human_decision") extra += this.gateHoldMs;
      return at;
    });
  }

  /** Timeline position of event k, with the human-decision pause stretched out. */
  private scheduledAt(k: number): number {
    if (!this.schedule.length) return 0;
    return this.schedule[Math.max(0, Math.min(k, this.schedule.length - 1))];
  }

  play() {
    if (this.playing) return;
    if (this.index >= this.events.length) this.index = 0;
    this.playing = true;
    this.startWall = performance.now();
    this.startOffset = this.index > 0 ? this.scheduledAt(this.index - 1) : 0;
    this.tick();
    this.emit();
  }

  pause() {
    this.playing = false;
    if (this.timer !== null) { window.clearTimeout(this.timer); this.timer = null; }
    this.emit();
  }

  toggle() { this.playing ? this.pause() : this.play(); }

  seek(index: number) {
    const wasPlaying = this.playing;
    if (this.timer !== null) { window.clearTimeout(this.timer); this.timer = null; }
    this.index = Math.max(0, Math.min(index, this.events.length));
    if (wasPlaying) {
      this.startWall = performance.now();
      this.startOffset = this.index > 0 ? this.scheduledAt(this.index - 1) : 0;
      this.tick();
    }
    this.emit();
  }

  setSpeed(s: number) {
    const wasPlaying = this.playing;
    if (wasPlaying) this.pause();
    this.speed = s;
    if (wasPlaying) this.play();
  }

  private tick = () => {
    if (!this.playing) return;
    if (this.index >= this.events.length) { this.playing = false; this.emit(); return; }

    const target = this.scheduledAt(this.index);
    const elapsed = (performance.now() - this.startWall) * this.speed + this.startOffset;
    const wait = target - elapsed;

    if (wait <= 0) {
      // Consume every event that is now due, then repaint once.
      while (this.index < this.events.length) {
        const t = this.scheduledAt(this.index);
        const el = (performance.now() - this.startWall) * this.speed + this.startOffset;
        if (t > el) break;
        this.index++;
      }
      this.emit();
      if (this.index >= this.events.length) { this.playing = false; this.emit(); return; }
      this.timer = window.setTimeout(this.tick, 16);
      return;
    }
    this.timer = window.setTimeout(this.tick, Math.min(wait / this.speed, 250));
  };

  destroy() { if (this.timer !== null) window.clearTimeout(this.timer); }
}
