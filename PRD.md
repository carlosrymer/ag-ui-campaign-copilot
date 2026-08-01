# PRD — AG-UI Campaign Copilot

## Problem statement

Agent backends and agent frontends are glued together with bespoke, per-project plumbing.
Every team that builds an agentic product re-invents the same channel: how does a partial
token reach the UI, how does the UI know a tool started versus finished, how does the
frontend learn that the agent's working state changed, and — hardest — how does a human
stop the agent before it does something irreversible?

That glue is almost always ad hoc. It is usually a websocket carrying untyped JSON blobs,
a `useState` that gets clobbered by whole-object re-sends, and a "confirm?" modal bolted on
after someone notices the agent can send a real email. The result is that the agent→UI
channel is the least portable, least testable part of the stack.

The **AG-UI protocol** claims to fix this by making that channel a specification: a fixed
set of typed events covering run lifecycle, streamed text, tool-call lifecycle, incremental
state sync, and human-in-the-loop interrupts.

This project tests two specific parts of that claim against a real workload:

1. **Incremental state sync via typed events + JSON Patch is meaningfully cheaper and
   cleaner than re-sending state.**
2. **Human-in-the-loop interrupts (approve / edit / reject mid-run) are first-class in the
   protocol, not bolted on.**

## Target user

A B2B SaaS growth or demand-generation marketer who wants an assistant to plan a
multi-channel campaign against their own historical performance data — and, critically,
their employer, who needs a hard guarantee that nothing goes live without a named human
approving it.

Secondarily: engineers evaluating whether AG-UI is worth adopting for their own agent UI.

## Goals

- Build a campaign copilot that researches an audience segment, pulls historical channel
  performance, allocates a budget, drafts copy, lints it for compliance, and publishes —
  with every step surfaced to the UI as typed AG-UI events.
- Make the publish step genuinely un-bypassable without a human decision.
- Exercise all three human paths — approve, edit, reject — and show the agent handling each
  correctly.
- **Measure** the state-sync claim: bytes on the wire for AG-UI's snapshot-plus-patches
  approach versus a naive full-snapshot-per-tick baseline, over the same runs.
- Report event counts by type, so the shape of a real run is visible rather than asserted.
- Ship a public, static, honest artifact: a replayer that plays real recorded runs through
  the same components the live app uses.

## Non-goals

- Not a production campaign manager. `publish_campaign` writes nothing to a real ad
  platform; it returns a simulated receipt against the local dataset.
- Not a benchmark of model quality. The models here drive the agent loop; the subject under
  test is the protocol, not the model.
- Not a multi-tenant service. Sessions are in-memory, keyed by thread id, and vanish on
  restart.
- Not an evaluation of AG-UI's other transports (binary/protobuf via `@ag-ui/proto`) or of
  the CopilotKit integration layer. This is the raw protocol over SSE.
- No authentication, no persistence, no rate limiting.

## Scope (MVP)

**Backend** — a Python FastAPI service exposing one AG-UI endpoint, `POST /agui`, which
takes a `RunAgentInput` and returns an SSE stream of AG-UI events encoded by the official
`ag_ui.encoder.EventEncoder`. An agent loop drives a model over five tools that compute
against a seeded local dataset (8 audience segments, 6 channels, 207 historical campaign
rows). Shared campaign state is diffed into RFC 6902 JSON Patches and emitted as
`STATE_DELTA`. Wire bytes are instrumented on both the real and the baseline path.

**The gate** — `publish_campaign` is registered as a gated tool. When the model calls it,
the tool does not execute; the run ends with `RunFinishedInterruptOutcome` carrying an
`Interrupt`. Only a subsequent `RunAgentInput` whose `resume` array resolves that interrupt
id lets the run continue, and the human's decision (approve / edit / reject) determines
what happens next.

**Frontend** — a React app whose entire rendering is driven by one pure reducer over AG-UI
events. Two event sources feed that reducer: a live SSE client, and a replayer that plays
recorded event streams at captured timing with a scrubber. Panels: streaming transcript,
live tool-call cards, the approval gate, a shared-state panel, and a wire-byte meter.

**Deliverable** — three real captured runs (one per human decision path) committed as JSON,
and a static GitHub Pages site that replays them.

## User stories

- As a demand-gen marketer, I want to describe a campaign in one sentence and watch the
  agent research it, so that I can see its reasoning instead of just its conclusion.
- As a marketer, I want to see which historical campaigns justified the budget split, so
  that I can argue with the recommendation rather than accept it blindly.
- As a marketing lead, I want the run to stop dead before anything publishes, so that no
  agent can spend budget without me.
- As a marketing lead, I want to approve *with edits* — change a headline, move budget
  between channels — so that I am not forced to choose between accepting a flawed plan and
  starting over.
- As a compliance owner, I want a rejection to be final within that run, so that the agent
  cannot simply retry the thing I just refused.
- As an engineer evaluating AG-UI, I want to see the actual byte counts and event
  histogram from a real run, so that I can judge the protocol's overhead claim myself.

## Success criteria

Judged strictly on whether AG-UI delivered on the two claims under test.

**Claim 1 — incremental state sync is meaningfully cheaper.** Met. Across three recorded
runs, state sync cost 3.5–6.2× fewer bytes than re-sending the full state object at the
same ticks, measured with identical SSE framing and the same JSON encoder. The advantage
grows as the run proceeds, because the state object grows monotonically while patches stay
proportional to what changed. See README for the per-run table.

The caveat that matters: this only wins because the state object is large relative to each
change. Early in a run, when state is nearly empty, individual patches are *not* cheaper
than the snapshot they replace — the win is cumulative, not per-event.

**Claim 2 — HITL interrupts are first-class.** Met, and more thoroughly than expected. The
protocol models a pause as a *run outcome* (`RunFinishedInterruptOutcome` carrying an
`Interrupt` with an id, a reason, and a JSON Schema describing the response it wants), and
resumption as a typed `ResumeEntry` on the next run's input. I did not have to invent a
side-channel, and the gate is enforced by the agent loop's control flow rather than by UI
convention. All three paths work: approve publishes, edit publishes a human-modified
version, reject blocks the publish permanently within that run.

Where it fell short is documented honestly in the README and ARCHITECTURE — the protocol
specifies the pause, but leaves session durability, the semantics of an edit, and gate
policy entirely to the implementer.

**Build criteria.** All met: three real captured runs committed; every recorded event
validates against the official `@ag-ui/core` zod schemas; the static site replays real
recordings through the live app's own components; no fabricated event streams.

## Risks / open questions

- **In-memory sessions.** An interrupt that outlives a server restart is unresumable. Fine
  for a demo; a real deployment needs the session in a store, and the protocol says nothing
  about how.
- **Gate policy lives in my code, not the protocol.** `GATED_TOOLS` is my set. AG-UI gives
  the mechanism to pause, not a policy language for what must be paused. Two teams will
  implement "which tools need approval" differently.
- **"Edit" has no protocol semantics.** `ResumeEntry.payload` is untyped. That the payload
  means "apply these budget and copy changes, then publish" is a private contract between
  my backend and my frontend. Interoperability stops at the gate.
- **Model variance.** Which tools get called, and how many times, differs run to run. Both
  models occasionally passed a bad filter argument and self-corrected on a retry. The
  recordings show this rather than hiding it.
- **Provider availability.** The Gemini key ran out of prepayment credits partway through
  this build, so the committed recordings are from Kimi K3. Both clients are implemented
  and both were verified working.
- **Replay is not proof of liveness.** A recording can go stale against a changed backend.
  The conformance check in CI catches schema drift but not behavioural drift.

## Timeline

Single sitting. Protocol research → seeded dataset and tools → agent loop and interrupt
machinery → capture harness → React reducer, replayer and UI → verification in a real
browser → docs and deploy.
