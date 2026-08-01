# AG-UI Campaign Copilot

**Try it live: [https://carlosrymer.github.io/ag-ui-campaign-copilot/](https://carlosrymer.github.io/ag-ui-campaign-copilot/)**

A B2B marketing copilot that plans a multi-channel campaign against real historical
performance data — and physically cannot publish it until a human approves, edits, or
rejects the plan. Built to test whether the AG-UI protocol makes the agent→UI channel a
real specification instead of bespoke glue.

> **The live page is a replayer, not a live agent.** GitHub Pages is static, so there is no
> model running there. It plays **six genuinely recorded runs** — two approvals, two
> approve-with-edits, two rejections, captured across two different model providers — at
> their original timing, through the exact same React components the live app uses.
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

**Verdict: true, by 3.37–5.24× across six real runs (4.43× in aggregate).**

The agent keeps a shared campaign state object (chosen segment, channel benchmarks, budget
split, copy variants, compliance results, approval status, activity log). AG-UI's model is
to send that object *once* as a `STATE_SNAPSHOT` and then send only RFC 6902 JSON Patches as
`STATE_DELTA` events.

I instrumented both paths at once. Every time state changes, the server measures the exact
bytes of the `STATE_DELTA` it is about to send, and the exact bytes a naive implementation
would have sent if it re-serialised the whole state object at that same moment. Same SSE
framing, same JSON encoder, same tick count — the only variable is delta versus snapshot.

| Recorded run | Model | Events | Wall time | State ticks | AG-UI (snapshot + patches) | Naive (snapshot every tick) | Ratio | Saved |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| **Human approves** | `kimi-k3` | 813 | 66s | 10 | 9,683 B | 44,357 B | **4.58×** | 78.17% |
| **Human edits the draft, then approves** | `kimi-k3` | 1,056 | 80s | 15 | 16,131 B | 73,691 B | **4.57×** | 78.11% |
| **Human rejects** | `kimi-k3` | 1,336 | 102s | 12 | 11,170 B | 58,539 B | **5.24×** | 80.92% |
| **Vague brief, agent recovers** | `gemini-3.6-flash` | 87 | 25s | 11 | 11,660 B | 52,397 B | **4.49×** | 77.75% |
| **Long enterprise run** | `gemini-3.6-flash` | 95 | 22s | 9 | 9,676 B | 39,147 B | **4.05×** | 75.28% |
| **SMB plan rejected on strategy** | `gemini-3.6-flash` | 93 | 23s | 8 | 8,997 B | 30,277 B | **3.37×** | 70.28% |
| **All six** | | **3,480** | | **65** | **67,317 B** | **298,408 B** | **4.43×** | **77.4%** |

Measured server-side by `backend/app/state.py` and recomputed independently in the browser by
`frontend/src/agui/reducer.ts` from the recorded events alone. The two land within ~3% of each
other (the live gauge reads 4.46× where the server recorded 4.58×) because the browser
re-serialises parsed JSON rather than replaying the server's exact bytes — close enough that
neither number depends on trusting the other. Reproduce with
`uv run python scripts/capture_runs.py`. Three runs were driven by `kimi-k3` and three by
`gemini-3.6-flash`.

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
  runs in CI on every push (`npm run validate`) — 3,480 of 3,480 recorded events pass.
- **The protocol is genuinely provider-agnostic.** The same agent loop ran on Google Gemini
  and on Moonshot's Kimi K3. Switching was one environment variable and zero protocol code,
  because both clients normalise to the same internal chunk shape before anything becomes an
  AG-UI event.
- **Streaming granularity varies wildly by provider, and the protocol does not normalise it.**
  This is the most practically useful thing I learned. For essentially the same workload,
  Kimi K3 produced **813–1,336 events per run** while Gemini 3.6 Flash produced **87–95** —
  roughly a **12× difference**, almost entirely in `TEXT_MESSAGE_CONTENT`. Kimi streams
  token-by-token; Gemini ships large chunks. Both are valid AG-UI. The consequence is that
  event *count* is meaningless as a cross-provider metric, and any UI that does per-event
  work (a React re-render, a network write, an analytics ping) will behave completely
  differently depending on which model is behind it. My replayer needed keyframe caching to
  scrub the Kimi runs smoothly and would never have needed it for the Gemini ones.
  If you build on AG-UI, coalesce text deltas on a frame budget rather than trusting the
  provider's chunking.
- **Byte savings track state size, not run length.** The weakest ratio (3.37×) is the SMB
  rejection, which never reaches a `published` state and so has the smallest state object.
  The strongest (5.24×) is a rejection with a fully built-out plan behind it. This confirms
  the mechanism: the win comes from how big the state object is when you would otherwise
  re-send it, not from how long the run takes.
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
   brand phrases and per-channel character limits. In most recorded runs the agent failed
   this check at least once and rewrote copy until it passed; in one Kimi run it took four
   attempts to get every variant under LinkedIn's 220-character limit.
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

## The recorded runs

Six real captures, all committed as `recordings/*.json` and replayable on the live page:

| Scenario | What it demonstrates |
|---|---|
| **Human approves** | The straight-through path: research → benchmarks → allocation → copy → compliance → gated publish → approval. |
| **Human edits the draft, then approves** | The human moves 30% of budget off the top channel, renames the campaign, and rewrites one variant. The **edited** plan is what publishes. |
| **Human rejects** | Legal blocks the publish. The tool never runs, and the agent acknowledges rather than retrying. |
| **Vague brief, agent recovers** | A deliberately sloppy brief ("our healthcare people... the hospital IT folks I think? budget is whatever's left, maybe like $38k?"). The agent resolves it to the right segment, fails compliance, rewrites, and the human then edits before approving. |
| **Long enterprise run** | A $400K multi-requirement brief with an explicit 30% concentration cap, producing the largest state object of the set. |
| **SMB plan rejected on strategy** | A rejection for business reasons rather than legal, on a different segment entirely. |

## What it cost

Building this — including several throwaway runs while debugging, and one full set of
captures I discarded and re-recorded after finding a model-labelling bug:

| Provider | Spend | What it bought |
|---|---|---|
| Moonshot (Kimi K3) | **$1.93** | ~7 full capture runs; 3 kept. K3's reasoning bursts are expensive — it was roughly 20× the cost per run of Gemini Flash. |
| Google (Gemini 3.6 Flash) | **< $0.10** | 3 kept capture runs plus debugging. Ran out of prepayment credits mid-build for unrelated reasons and was topped up. |

Total under **$2.10** for the whole project. Worth stating plainly because the expensive
part was not the build — it was re-recording, which is exactly what committed run artifacts
are supposed to make unnecessary next time.

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
cd backend  && uv run python scripts/capture_runs.py                 # re-record every scenario
cd backend  && uv run python scripts/capture_runs.py --only approve  # just one
cd backend  && uv run python scripts/capture_runs.py --model gemini-3.6-flash
cd frontend && npm run validate                        # AG-UI schema conformance check
cd frontend && npm run build                           # build the static replayer
```

With no `VITE_AGENT_URL` set, the frontend runs in replay mode — which is exactly what the
published site is.

## Stack

- **Protocol** — AG-UI: `ag-ui-protocol` 0.1.19 (Python), `@ag-ui/core` 0.0.57 (TypeScript),
  over Server-Sent Events
- **Agent server** — Python 3.11, FastAPI, `uv`, `jsonpatch`, `httpx`
- **Models** — Moonshot Kimi K3 and Google Gemini 3.6 Flash, both implemented and both used
  for committed recordings; no OpenAI or Anthropic key was available while building this, so
  no client was written for either
- **Frontend** — React 19, TypeScript, Vite, `fast-json-patch`; one pure reducer shared by
  live and replay modes
- **Verification** — schema conformance on all 3,480 recorded events, plus verification of
  every scenario in headless Chromium (desktop and mobile viewports, light and dark), run
  against the deployed bytes rather than a local build

## Deployed via

GitHub Pages, serving the Vite build from the `gh-pages` branch. The build runs the AG-UI
conformance check over the committed recordings first, so a recording that drifts out of
spec never ships.

An equivalent GitHub Actions workflow is included at
[`deploy/github-pages-workflow.yml`](deploy/github-pages-workflow.yml) rather than in
`.github/workflows/`, because the credential I had while building this lacked the `workflow`
OAuth scope and could not push into that directory. [`deploy/README.md`](deploy/README.md)
has both paths written out.

---
Part of an ongoing series of small, real-world builds trialing frontier AI models, frameworks,
and tools as they ship.
