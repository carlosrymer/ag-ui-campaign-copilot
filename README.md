# AG-UI Campaign Copilot

**Try it live: [https://carlosrymer.github.io/ag-ui-campaign-copilot/](https://carlosrymer.github.io/ag-ui-campaign-copilot/)**

A B2B marketing copilot that plans a multi-channel campaign against real historical
performance data — and physically cannot publish it until a human approves, edits, or
rejects the plan. Built to test whether the AG-UI protocol makes the agent→UI channel a
real specification instead of bespoke glue.

> **The live page is a replayer, not a live agent.** GitHub Pages is static, so there is no
> model running there. It plays three genuinely recorded runs — one per human decision path
> — at their captured timing, through the exact same React components the live app uses.
> To drive a live agent with your own key, see [Running locally](#running-locally).

## What this showcases

**Technology:** [AG-UI](https://ag-ui.com) — an event-driven protocol that standardises the
channel between an AI agent and its user interface.

AG-UI's premise is that the agent→frontend channel deserves a spec. Instead of every team
inventing its own websocket message shapes, it defines a fixed vocabulary of typed events:
run lifecycle (`RUN_STARTED`, `RUN_FINISHED`, `RUN_ERROR`), streamed text
(`TEXT_MESSAGE_START/CONTENT/END`), tool-call lifecycle
(`TOOL_CALL_START/ARGS/END/RESULT`), incremental state sync (`STATE_SNAPSHOT`,
`STATE_DELTA`), and more. I used the official SDKs directly —
[`ag-ui-protocol` 0.1.19](https://pypi.org/project/ag-ui-protocol/) on the Python side,
[`@ag-ui/core` 0.0.57](https://www.npmjs.com/package/@ag-ui/core) on the TypeScript side —
with no integration framework in between, because the protocol is what I wanted to test.

I put two specific claims under test.

### Claim 1 — incremental state sync beats re-sending state

**Verdict: true, by 4.57–5.24× on these runs.**

The agent keeps a shared campaign state object (chosen segment, channel benchmarks, budget
split, copy variants, compliance results, approval status, activity log). AG-UI's model is
to send that object *once* as a `STATE_SNAPSHOT` and then send only RFC 6902 JSON Patches as
`STATE_DELTA` events.

I instrumented both paths at once. Every time state changes, the server measures the exact
bytes of the `STATE_DELTA` it is about to send, and the exact bytes a naive implementation
would have sent if it re-serialised the whole state object at that same moment. Same SSE
framing, same JSON encoder, same tick count — the only variable is delta versus snapshot.

| Recorded run | Events | Wall time | State ticks | AG-UI (snapshot + patches) | Naive (snapshot every tick) | Ratio | Saved |
|---|---:|---:|---:|---:|---:|---:|---:|
| **Human approves** | 813 | 66s | 10 | 9,683 B | 44,357 B | **4.58×** | 78.17% |
| **Human edits the draft, then approves** | 1,056 | 80s | 15 | 16,131 B | 73,691 B | **4.57×** | 78.11% |
| **Human rejects** | 1,336 | 102s | 12 | 11,170 B | 58,539 B | **5.24×** | 80.92% |
| **All three** | **3,205** | | **37** | **36,984 B** | **176,587 B** | **4.77×** | **79.1%** |

Measured server-side by `backend/app/state.py` and recomputed independently in the browser by
`frontend/src/agui/reducer.ts` from the recorded events alone. The two land within ~3% of each
other (the live gauge reads 4.46× where the server recorded 4.58×) because the browser
re-serialises parsed JSON rather than replaying the server's exact bytes — close enough that
neither number depends on trusting the other. Reproduce with
`uv run python scripts/capture_runs.py`. All three runs were driven by `kimi-k3`.

The gain compounds: state grows monotonically through a run while patches stay proportional
to what actually changed. By the end of a run a single patch like
`[{"op":"replace","path":"/phase","value":"publishing"}]` is ~60 bytes against a ~5 KB
snapshot.

**Where the claim is weaker than it sounds.** This is a win about *aggregate* traffic, not
about every event. Early in a run, when the state object is nearly empty, a patch is not
meaningfully cheaper than the snapshot it replaces — the first few ticks are close to a
wash. It only pays off because the object gets big. If your agent's shared state is small
and flat, JSON Patch buys you correctness and intent, not bandwidth. And patches carry a
real cost the snapshot approach does not have: the client must apply them in order and stay
in sync, which is why the spec tells clients to request a fresh snapshot if a patch fails to
apply.

### Claim 2 — human-in-the-loop interrupts are first-class

**Verdict: true, and better than I expected.**

I assumed I would have to build the approval gate myself and bolt it onto the protocol. I
did not. The Python SDK ships `Interrupt`, `RunFinishedInterruptOutcome`, `ResumeEntry`,
`ResumeStatus`, and a `HumanInTheLoopCapabilities` descriptor. The pause is modelled as a
*run outcome*:

```python
RunFinishedEvent(
    thread_id=..., run_id=...,
    outcome=RunFinishedInterruptOutcome(
        type="interrupt",
        interrupts=[Interrupt(
            id="int_9a9a2412c5",
            reason="human_approval_required",
            message="Approve publishing 'Q3 Demand Gen — Mid-Market Fintech Ops'?",
            tool_call_id="call_abc",
            response_schema={...},           # what the agent wants back
            metadata={"proposed_args": {...}},
        )],
    ))
```

The client resumes by sending a normal `RunAgentInput` whose `resume` array carries a
`ResumeEntry(interrupt_id=..., status="resolved", payload={...})`.

That the interrupt carries an **id**, a **reason**, and a **`responseSchema`** is the part
that makes it a protocol rather than a convention: a generic client can render an approval
UI it has never seen before, because the agent describes the decision it needs.

In this app `publish_campaign` is registered as a gated tool. When the model calls it, the
UI renders the proposed campaign — but **the tool does not execute**. The run ends in an
interrupt, and the only way forward is a resume that names the interrupt id. All three paths
are exercised in the recordings:

| Path | What actually happens |
|---|---|
| **Approve** | The stashed call executes with its original arguments and the campaign publishes. |
| **Edit** | The human's edits are merged into the stashed arguments *first* — budget moved between channels, campaign renamed, one copy variant rewritten — and the **edited** version is what publishes. Human-edited variants are flagged in state and later model drafts are forbidden to overwrite them. |
| **Reject** | The tool never executes. A synthetic result telling the model it was blocked goes back into history, state moves to `rejected`, and the agent acknowledges and proposes next steps instead of retrying. |

**Where it fell short.** The protocol specifies the pause; everything around it is still my
problem:

- **`ResumeEntry.payload` is untyped.** That my payload means "apply these budget and copy
  edits, then publish" is a private contract between my backend and my frontend.
  Interoperability stops exactly at the gate — which is a shame, because approve/edit/reject
  is the most universal shape in all of agent UX.
- **Which tools need a gate is not the protocol's concern.** `GATED_TOOLS` is my set in my
  code. AG-UI gives a mechanism, not a policy.
- **The interrupt terminates the HTTP response.** On the wire, one "paused run" is two runs
  sharing a thread id. That is the right call for durability, but "the run is paused" is a
  fiction the client maintains — and it means the server must hold session state somewhere.
  Mine is in memory, so a restart orphans a pending interrupt. The protocol says nothing
  about that.

### What else I found

- **Cross-language conformance is real.** Every event my Python backend produced validates
  against the TypeScript zod schemas in `@ag-ui/core`, with no adaptation layer. That check
  runs in CI on every push (`npm run validate`) — 3,205 of 3,205 recorded events pass.
- **The protocol is genuinely provider-agnostic.** The same agent loop ran on Google Gemini
  and on Moonshot's Kimi K3. Switching was one environment variable and zero protocol code,
  because both clients normalise to the same internal chunk shape before anything becomes an
  AG-UI event.
- **Per-token events are not free.** A token-streaming model emits well over a thousand
  `TEXT_MESSAGE_CONTENT` frames for a single run. That is the protocol working as designed,
  but it dominates event counts and recording size, and it is worth batching before you put
  it on a real network.
- **`TOOL_CALL_ARGS` never streamed.** Both providers return complete argument objects, so
  arguments arrived as a single chunk despite the protocol supporting many. That capability
  is untested here.

## The use case

A demand-generation marketer at a B2B SaaS company asks for a Q3 campaign with a $120,000
budget. The agent has to:

1. **`search_segments`** — rank audience segments by keyword relevance, intent score and
   reachable size.
2. **`get_channel_benchmarks`** — aggregate the historical campaign table for that segment
   into CTR, CPC, cost-per-MQL, cost-per-SQL and pipeline ROAS per channel.
3. **`allocate_budget`** — split the budget with a real constrained allocator: ROAS-weighted
   but damped, minimum-viable-spend floors per channel, and a concentration cap with
   redistribution. Projects MQLs, SQLs and pipeline forward from the benchmarks.
4. **Draft copy**, then **`check_copy_compliance`** — a deterministic linter for banned
   brand phrases and per-channel character limits. In the recorded runs the agent failed
   this check and rewrote copy until it passed.
5. **`publish_campaign`** — gated. Requires a human.

The tools compute against a seeded dataset — 8 audience segments, 6 channels, and 207
historical campaign rows — generated deterministically by
`backend/scripts/seed_dataset.py`. The numbers are invented, but the funnel is
arithmetically consistent (spend → impressions → clicks → MQL → SQL → pipeline), so
aggregates are genuinely computed rather than canned. `publish_campaign` is simulated: it
returns a receipt against the local dataset and contacts no ad platform.

This is a fair test rather than a flattering one because the gated action is the *point* of
the workflow, not an afterthought — nobody lets an agent spend six figures unsupervised —
and because the state object is exactly the kind of thing that makes naive re-sending
expensive: nested, growing, and updated in small pieces.

## Docs

- [Architecture](ARCHITECTURE.md) — system design, components, data flow, deployment
- [PRD](PRD.md) — problem statement, scope, success criteria

## Running locally

The live agent needs a model key. Gemini and Moonshot are both supported.

```bash
git clone https://github.com/carlosrymer/ag-ui-campaign-copilot.git
cd ag-ui-campaign-copilot

cp backend/.env.example backend/.env   # add GEMINI_API_KEY or MOONSHOT_API_KEY
export MOONSHOT_API_KEY=...            # or GEMINI_API_KEY=...
export COPILOT_MODEL=kimi-k3           # or gemini-3.6-flash, etc.

docker compose up
# UI     -> http://localhost:5173   (approval buttons drive a real model)
# Agent  -> http://localhost:8000/health
```

Without Docker:

```bash
# terminal 1 — the AG-UI agent server
cd backend
uv sync
uv run python scripts/seed_dataset.py        # regenerate the dataset (deterministic)
MOONSHOT_API_KEY=... COPILOT_MODEL=kimi-k3 uv run uvicorn app.main:app --port 8000

# terminal 2 — the UI, pointed at that server
cd frontend
npm install
VITE_AGENT_URL=http://localhost:8000 npm run dev
```

Other useful commands:

```bash
cd backend  && uv run python scripts/capture_runs.py   # re-record all three runs
cd frontend && npm run validate                        # AG-UI schema conformance check
cd frontend && npm run build                           # build the static replayer
```

With no `VITE_AGENT_URL` set, the frontend runs in replay mode — which is exactly what the
published site is.

## Stack

- **Protocol** — AG-UI: `ag-ui-protocol` 0.1.19 (Python), `@ag-ui/core` 0.0.57 (TypeScript),
  over Server-Sent Events
- **Agent server** — Python 3.11, FastAPI, `uv`, `jsonpatch`, `httpx`
- **Models** — Moonshot Kimi K3 (recorded runs) and Google Gemini 3.x, both implemented and
  verified; no OpenAI or Anthropic key was available while building this, so no client was
  written for either
- **Frontend** — React 19, TypeScript, Vite, `fast-json-patch`; one pure reducer shared by
  live and replay modes
- **Verification** — schema conformance in CI, plus manual verification of all three
  scenarios in headless Chromium (desktop and mobile viewports, light and dark)

## Deployed via

GitHub Pages, built and published by GitHub Actions on every push to `main`
(`actions/configure-pages` → `upload-pages-artifact` → `deploy-pages`). The workflow runs
the AG-UI conformance check before it builds, so a recording that drifts out of spec fails
the deploy.

---
Part of an ongoing series of small, real-world builds trialing frontier AI models, frameworks,
and tools as they ship.
