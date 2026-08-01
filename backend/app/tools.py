"""Campaign copilot tools.

Every function here is a real computation over the seeded dataset in `app/data`.
None of them return canned text: `get_channel_benchmarks` genuinely aggregates the
189 historical campaign rows, and `allocate_budget` genuinely runs a
ROAS-weighted allocator with min-spend and concentration constraints.
"""

from __future__ import annotations

import json
import pathlib
from typing import Any

DATA = pathlib.Path(__file__).resolve().parent / "data"

SEGMENTS: list[dict] = json.loads((DATA / "segments.json").read_text())
CHANNELS: list[dict] = json.loads((DATA / "channels.json").read_text())
CAMPAIGNS: list[dict] = json.loads((DATA / "campaigns.json").read_text())
BRAND: dict = json.loads((DATA / "brand.json").read_text())

SEG_BY_ID = {s["id"]: s for s in SEGMENTS}
CH_BY_ID = {c["id"]: c for c in CHANNELS}


class ToolError(Exception):
    """Raised for bad tool arguments; surfaced back to the model as a tool result."""


# --------------------------------------------------------------------------
# 1. search_segments
# --------------------------------------------------------------------------

def search_segments(query: str = "", min_accounts: int = 0,
                    industry: str | None = None, limit: int = 4) -> dict[str, Any]:
    """Rank audience segments by keyword overlap, intent score and reachable size."""
    terms = [t for t in "".join(c.lower() if c.isalnum() else " " for c in query).split() if len(t) > 2]

    scored = []
    for seg in SEGMENTS:
        if seg["reachable_accounts"] < min_accounts:
            continue
        if industry and industry.lower() not in seg["industry"].lower():
            continue
        haystack = f"{seg['name']} {seg['industry']} {seg['employee_band']}".lower()
        keyword_hits = sum(1 for t in terms if t in haystack)
        keyword_score = keyword_hits / len(terms) if terms else 0.0
        # Blend text relevance with intrinsic quality so an empty query still ranks sanely.
        score = 0.60 * keyword_score + 0.25 * seg["intent_score"] + 0.15 * min(
            seg["reachable_accounts"] / 25000, 1.0)
        n_rows = sum(1 for c in CAMPAIGNS if c["segment_id"] == seg["id"])
        scored.append({
            **seg,
            "match_score": round(score, 4),
            "keyword_hits": keyword_hits,
            "historical_campaigns_on_record": n_rows,
        })

    scored.sort(key=lambda s: s["match_score"], reverse=True)
    return {
        "query": query,
        "filters": {"min_accounts": min_accounts, "industry": industry},
        "total_matched": len(scored),
        "segments": scored[: max(1, min(limit, 8))],
    }


# --------------------------------------------------------------------------
# 2. get_channel_benchmarks
# --------------------------------------------------------------------------

def get_channel_benchmarks(segment_id: str, channel_ids: list[str] | None = None) -> dict[str, Any]:
    """Aggregate historical performance for a segment, per channel.

    Real aggregation over the campaign table: sums spend/impressions/clicks/MQL/
    SQL/pipeline per channel, then derives CTR, CPC, cost-per-MQL and ROAS.
    """
    if segment_id not in SEG_BY_ID:
        raise ToolError(f"unknown segment_id '{segment_id}'. Valid ids: {sorted(SEG_BY_ID)}")

    wanted = set(channel_ids) if channel_ids else set(CH_BY_ID)
    unknown = wanted - set(CH_BY_ID)
    if unknown:
        raise ToolError(f"unknown channel_ids {sorted(unknown)}. Valid ids: {sorted(CH_BY_ID)}")

    rows = [c for c in CAMPAIGNS if c["segment_id"] == segment_id and c["channel_id"] in wanted]

    by_channel: dict[str, dict] = {}
    for r in rows:
        acc = by_channel.setdefault(r["channel_id"], {
            "campaigns": 0, "spend_usd": 0.0, "impressions": 0,
            "clicks": 0, "mqls": 0, "sqls": 0, "pipeline_usd": 0.0,
        })
        acc["campaigns"] += 1
        acc["spend_usd"] += r["spend_usd"]
        acc["impressions"] += r["impressions"]
        acc["clicks"] += r["clicks"]
        acc["mqls"] += r["mqls"]
        acc["sqls"] += r["sqls"]
        acc["pipeline_usd"] += r["pipeline_usd"]

    out = []
    for ch_id in sorted(wanted):
        acc = by_channel.get(ch_id)
        if not acc:
            out.append({
                "channel_id": ch_id, "channel_name": CH_BY_ID[ch_id]["name"],
                "campaigns": 0, "note": "no historical runs for this segment/channel",
            })
            continue
        spend = acc["spend_usd"]
        out.append({
            "channel_id": ch_id,
            "channel_name": CH_BY_ID[ch_id]["name"],
            "campaigns": acc["campaigns"],
            "spend_usd": round(spend, 2),
            "impressions": acc["impressions"],
            "clicks": acc["clicks"],
            "mqls": acc["mqls"],
            "sqls": acc["sqls"],
            "pipeline_usd": round(acc["pipeline_usd"], 2),
            "ctr": round(acc["clicks"] / acc["impressions"], 5) if acc["impressions"] else None,
            "cpc_usd": round(spend / acc["clicks"], 2) if acc["clicks"] else None,
            "cost_per_mql_usd": round(spend / acc["mqls"], 2) if acc["mqls"] else None,
            "cost_per_sql_usd": round(spend / acc["sqls"], 2) if acc["sqls"] else None,
            "pipeline_roas": round(acc["pipeline_usd"] / spend, 2) if spend else None,
        })

    ranked = [c for c in out if c.get("pipeline_roas") is not None]
    ranked.sort(key=lambda c: c["pipeline_roas"], reverse=True)

    return {
        "segment_id": segment_id,
        "segment_name": SEG_BY_ID[segment_id]["name"],
        "rows_aggregated": len(rows),
        "channels": out,
        "best_by_pipeline_roas": [c["channel_id"] for c in ranked[:3]],
    }


# --------------------------------------------------------------------------
# 3. allocate_budget
# --------------------------------------------------------------------------

def allocate_budget(segment_id: str, total_budget_usd: float,
                    channel_ids: list[str] | None = None,
                    max_share: float = 0.45) -> dict[str, Any]:
    """ROAS-weighted budget allocator with min-spend and concentration constraints.

    1. Weight each channel by its historical pipeline ROAS for this segment.
    2. Drop channels whose weighted share falls under their min viable spend.
    3. Cap any single channel at `max_share`, redistributing the excess.
    4. Project MQLs / SQLs / pipeline forward using that channel's historical
       cost-per-MQL and SQL conversion rate.
    """
    if total_budget_usd <= 0:
        raise ToolError("total_budget_usd must be positive")
    if not 0.1 <= max_share <= 1.0:
        raise ToolError("max_share must be between 0.1 and 1.0")

    bench = get_channel_benchmarks(segment_id, channel_ids)
    candidates = [c for c in bench["channels"] if c.get("pipeline_roas")]
    if not candidates:
        raise ToolError(f"no channel with historical data for segment '{segment_id}'")

    excluded: list[dict] = []

    # Soften the weights. Raw ROAS-proportional weighting hands almost everything to the
    # single best channel; a square-root damp keeps the ranking but leaves a real
    # multi-channel mix, which is what a media planner would actually ship.
    ALPHA = 0.5
    weight = {c["channel_id"]: c["pipeline_roas"] ** ALPHA for c in candidates}

    # Step 1: choose how many channels the budget can actually sustain. Every funded
    # channel must clear its minimum viable spend, so drop the weakest until the
    # combined floor fits inside the budget.
    pool = sorted(candidates, key=lambda c: weight[c["channel_id"]], reverse=True)
    while pool and sum(CH_BY_ID[c["channel_id"]]["min_viable_spend_usd"] for c in pool) > total_budget_usd:
        worst = pool[-1]
        excluded.append({
            "channel_id": worst["channel_id"],
            "reason": "budget cannot cover this channel's minimum viable spend alongside better performers",
            "min_viable_spend_usd": CH_BY_ID[worst["channel_id"]]["min_viable_spend_usd"],
            "pipeline_roas": worst["pipeline_roas"],
        })
        pool = pool[:-1]

    if not pool:
        raise ToolError(
            f"total_budget_usd {total_budget_usd} is too small: no channel clears its "
            f"min viable spend for segment '{segment_id}'")

    # Step 2: seat every funded channel at its floor, then spread the remainder by weight.
    floors = {c["channel_id"]: float(CH_BY_ID[c["channel_id"]]["min_viable_spend_usd"]) for c in pool}
    alloc = dict(floors)
    remainder = total_budget_usd - sum(floors.values())
    total_w = sum(weight[c["channel_id"]] for c in pool)
    for c in pool:
        alloc[c["channel_id"]] += remainder * weight[c["channel_id"]] / total_w

    # Step 3: apply the concentration cap, redistributing overflow to channels that are
    # neither capped nor pinned at their floor.
    cap = total_budget_usd * max_share
    for _ in range(len(pool) * 2):
        over = {k: v for k, v in alloc.items() if v > cap + 0.01 and cap >= floors[k]}
        if not over:
            break
        overflow = sum(v - cap for v in over.values())
        for k in over:
            alloc[k] = cap
        free = {k: v for k, v in alloc.items() if k not in over and v < cap - 0.01}
        if not free:
            break
        free_w = sum(weight[k] for k in free)
        for k in free:
            alloc[k] += overflow * weight[k] / free_w

    bench_by_id = {c["channel_id"]: c for c in candidates}
    lines, proj_mql, proj_sql, proj_pipe = [], 0.0, 0.0, 0.0
    for ch_id, amount in sorted(alloc.items(), key=lambda kv: kv[1], reverse=True):
        b = bench_by_id[ch_id]
        cpm_mql = b["cost_per_mql_usd"]
        mqls = amount / cpm_mql if cpm_mql else 0.0
        sql_rate = (b["sqls"] / b["mqls"]) if b["mqls"] else 0.0
        sqls = mqls * sql_rate
        pipeline = amount * b["pipeline_roas"]
        proj_mql += mqls
        proj_sql += sqls
        proj_pipe += pipeline
        lines.append({
            "channel_id": ch_id,
            "channel_name": b["channel_name"],
            "budget_usd": round(amount, 2),
            "share": round(amount / total_budget_usd, 4),
            "basis_pipeline_roas": b["pipeline_roas"],
            "basis_cost_per_mql_usd": cpm_mql,
            "projected_mqls": round(mqls, 1),
            "projected_sqls": round(sqls, 1),
            "projected_pipeline_usd": round(pipeline, 2),
            "lead_time_days": CH_BY_ID[ch_id]["lead_time_days"],
        })

    return {
        "segment_id": segment_id,
        "segment_name": SEG_BY_ID[segment_id]["name"],
        "total_budget_usd": round(total_budget_usd, 2),
        "max_share_constraint": max_share,
        "allocations": lines,
        "excluded_channels": excluded,
        "projected_totals": {
            "mqls": round(proj_mql, 1),
            "sqls": round(proj_sql, 1),
            "pipeline_usd": round(proj_pipe, 2),
            "blended_pipeline_roas": round(proj_pipe / total_budget_usd, 2),
            "blended_cost_per_mql_usd": round(total_budget_usd / proj_mql, 2) if proj_mql else None,
        },
    }


# --------------------------------------------------------------------------
# 4. check_copy_compliance
# --------------------------------------------------------------------------

def check_copy_compliance(variants: list[dict]) -> dict[str, Any]:
    """Lint drafted copy against the brand's banned-phrase and length rules.

    A real string check, not a model call -- so the agent gets deterministic,
    checkable feedback it must act on.
    """
    if not variants:
        raise ToolError("variants must be a non-empty list")

    limits = {"linkedin_ads": 220, "google_search": 90, "display_retargeting": 60,
              "email_nurture": 320, "webinar": 300, "podcast_sponsor": 400}

    results, violations = [], 0
    for v in variants:
        vid = v.get("id") or v.get("variant_id") or "unnamed"
        body = (v.get("body") or v.get("copy") or "").strip()
        channel = v.get("channel_id") or v.get("channel") or ""
        issues = []

        low = body.lower()
        for phrase in BRAND["banned_phrases"]:
            if phrase.lower() in low:
                issues.append({"rule": "banned_phrase", "detail": phrase})

        limit = limits.get(channel)
        if limit and len(body) > limit:
            issues.append({"rule": "length", "detail": f"{len(body)} chars exceeds {limit} for {channel}"})
        if not body:
            issues.append({"rule": "empty", "detail": "variant body is empty"})

        violations += len(issues)
        results.append({"variant_id": vid, "channel_id": channel,
                        "char_count": len(body), "issues": issues,
                        "status": "fail" if issues else "pass"})

    return {"checked": len(variants), "total_issues": violations,
            "all_passed": violations == 0, "results": results,
            "rules_applied": {"banned_phrases": BRAND["banned_phrases"], "char_limits": limits}}


# --------------------------------------------------------------------------
# 5. publish_campaign -- the gated action
# --------------------------------------------------------------------------

def publish_campaign(campaign_name: str, segment_id: str, allocations: list[dict],
                     variant_ids: list[str], approved_by: str = "unknown") -> dict[str, Any]:
    """Simulated publish. Only ever reached after a human resolves the interrupt."""
    if segment_id not in SEG_BY_ID:
        raise ToolError(f"unknown segment_id '{segment_id}'")
    total = sum(float(a.get("budget_usd", 0)) for a in allocations)
    return {
        "status": "published",
        "campaign_name": campaign_name,
        "segment_id": segment_id,
        "segment_name": SEG_BY_ID[segment_id]["name"],
        "flight_channels": [a.get("channel_id") for a in allocations],
        "committed_budget_usd": round(total, 2),
        "live_variants": variant_ids,
        "approved_by": approved_by,
        "note": "Simulated publish against the local dataset. No external ad platform was contacted.",
    }


# --------------------------------------------------------------------------
# Registry + JSON-schema declarations handed to the model
# --------------------------------------------------------------------------

REGISTRY = {
    "search_segments": search_segments,
    "get_channel_benchmarks": get_channel_benchmarks,
    "allocate_budget": allocate_budget,
    "check_copy_compliance": check_copy_compliance,
    "publish_campaign": publish_campaign,
}

#: Tools that must not execute until a human resolves an AG-UI interrupt.
GATED_TOOLS = {"publish_campaign"}

TOOL_SCHEMAS: list[dict] = [
    {
        "name": "search_segments",
        "description": "Search the audience segment library. Returns ranked segments with reachable "
                       "account counts, average ACV and intent score.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Free-text description of the audience."},
                "min_accounts": {"type": "integer", "description": "Minimum reachable accounts."},
                "industry": {"type": "string", "description": "Optional industry filter."},
                "limit": {"type": "integer", "description": "Max segments to return (1-8)."},
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_channel_benchmarks",
        "description": "Aggregate historical campaign performance for one segment across channels. "
                       "Returns CTR, CPC, cost per MQL/SQL and pipeline ROAS per channel.",
        "parameters": {
            "type": "object",
            "properties": {
                "segment_id": {"type": "string", "description": "Segment id from search_segments."},
                "channel_ids": {"type": "array", "items": {"type": "string"},
                                "description": "Optional subset of channel ids."},
            },
            "required": ["segment_id"],
        },
    },
    {
        "name": "allocate_budget",
        "description": "Split a total budget across channels using historical pipeline ROAS, honouring "
                       "each channel's minimum viable spend and a max-share concentration cap. "
                       "Returns per-channel budget plus projected MQLs, SQLs and pipeline.",
        "parameters": {
            "type": "object",
            "properties": {
                "segment_id": {"type": "string"},
                "total_budget_usd": {"type": "number"},
                "channel_ids": {"type": "array", "items": {"type": "string"}},
                "max_share": {"type": "number", "description": "Max share of budget for one channel (0.1-1.0). Default 0.45."},
            },
            "required": ["segment_id", "total_budget_usd"],
        },
    },
    {
        "name": "check_copy_compliance",
        "description": "Lint drafted copy variants against brand banned phrases and per-channel character "
                       "limits. Call this after drafting and fix anything that fails.",
        "parameters": {
            "type": "object",
            "properties": {
                "variants": {
                    "type": "array",
                    "description": "The drafted variants to check.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string"},
                            "channel_id": {"type": "string"},
                            "body": {"type": "string"},
                        },
                        "required": ["id", "channel_id", "body"],
                    },
                },
            },
            "required": ["variants"],
        },
    },
    {
        "name": "publish_campaign",
        "description": "Push the campaign live. THIS IS IRREVERSIBLE and always requires explicit human "
                       "approval, which the runtime will request on your behalf. Call it once the plan, "
                       "budget split and copy variants are all settled.",
        "parameters": {
            "type": "object",
            "properties": {
                "campaign_name": {"type": "string"},
                "segment_id": {"type": "string"},
                "allocations": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "channel_id": {"type": "string"},
                            "budget_usd": {"type": "number"},
                        },
                        "required": ["channel_id", "budget_usd"],
                    },
                },
                "variant_ids": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["campaign_name", "segment_id", "allocations", "variant_ids"],
        },
    },
]
