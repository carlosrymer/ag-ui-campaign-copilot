"""Generate the seeded (fake but internally consistent) marketing dataset.

Deterministic: fixed seed, so `uv run python scripts/seed_dataset.py` reproduces
byte-identical files. The numbers are invented, but the relationships between them
(spend -> impressions -> clicks -> MQL -> SQL -> pipeline) are arithmetically
consistent, so the tools that aggregate over this data return real computed
answers rather than canned ones.
"""

import json
import pathlib
import random

random.seed(20260801)

OUT = pathlib.Path(__file__).resolve().parent.parent / "app" / "data"
OUT.mkdir(parents=True, exist_ok=True)

SEGMENTS = [
    # id, name, industry, employees band, size (accounts), avg ACV, intent score
    ("seg_midmarket_fintech", "Mid-market fintech ops leaders", "Financial Services", "201-1000", 4820, 48000, 0.71),
    ("seg_ent_healthcare_it", "Enterprise healthcare IT buyers", "Healthcare", "1000+", 1240, 145000, 0.63),
    ("seg_smb_ecom_ops", "SMB e-commerce operations managers", "Retail / E-commerce", "11-200", 18600, 9800, 0.55),
    ("seg_ent_manufacturing", "Enterprise manufacturing supply-chain leads", "Manufacturing", "1000+", 2075, 118000, 0.48),
    ("seg_midmarket_devtools", "Mid-market platform engineering leads", "Software", "201-1000", 6390, 62000, 0.78),
    ("seg_smb_prof_services", "SMB professional services owners", "Professional Services", "11-200", 24100, 7400, 0.41),
    ("seg_ent_public_sector", "Public sector procurement directors", "Government", "1000+", 890, 210000, 0.35),
    ("seg_midmarket_logistics", "Mid-market logistics ops directors", "Transportation", "201-1000", 3310, 54000, 0.58),
]

CHANNELS = [
    # id, name, pricing model, unit cost, min viable spend, lead time (days)
    ("linkedin_ads", "LinkedIn Ads", "cpm", 38.50, 5000, 3),
    ("google_search", "Google Search", "cpc", 12.80, 2500, 1),
    ("display_retargeting", "Display Retargeting", "cpm", 6.20, 1500, 2),
    ("email_nurture", "Email Nurture", "cpm", 1.10, 500, 1),
    ("webinar", "Webinar Program", "flat", 14000.00, 14000, 21),
    ("podcast_sponsor", "Podcast Sponsorship", "cpm", 25.00, 8000, 14),
]

# Per (segment, channel) response multipliers -- this is what makes some channels
# genuinely better for some segments, so the allocator has real signal to find.
AFFINITY = {
    "seg_midmarket_fintech": {"linkedin_ads": 1.35, "google_search": 1.10, "display_retargeting": 0.70,
                              "email_nurture": 1.15, "webinar": 1.25, "podcast_sponsor": 0.85},
    "seg_ent_healthcare_it": {"linkedin_ads": 1.20, "google_search": 0.80, "display_retargeting": 0.55,
                              "email_nurture": 0.95, "webinar": 1.55, "podcast_sponsor": 0.60},
    "seg_smb_ecom_ops": {"linkedin_ads": 0.65, "google_search": 1.45, "display_retargeting": 1.30,
                         "email_nurture": 1.25, "webinar": 0.60, "podcast_sponsor": 1.05},
    "seg_ent_manufacturing": {"linkedin_ads": 1.05, "google_search": 0.75, "display_retargeting": 0.60,
                              "email_nurture": 0.90, "webinar": 1.40, "podcast_sponsor": 0.70},
    "seg_midmarket_devtools": {"linkedin_ads": 1.15, "google_search": 1.25, "display_retargeting": 0.65,
                               "email_nurture": 1.05, "webinar": 1.10, "podcast_sponsor": 1.50},
    "seg_smb_prof_services": {"linkedin_ads": 0.70, "google_search": 1.30, "display_retargeting": 1.10,
                              "email_nurture": 1.35, "webinar": 0.75, "podcast_sponsor": 0.80},
    "seg_ent_public_sector": {"linkedin_ads": 0.90, "google_search": 0.70, "display_retargeting": 0.45,
                              "email_nurture": 1.00, "webinar": 1.45, "podcast_sponsor": 0.50},
    "seg_midmarket_logistics": {"linkedin_ads": 1.00, "google_search": 1.15, "display_retargeting": 0.85,
                                "email_nurture": 1.10, "webinar": 1.05, "podcast_sponsor": 0.75},
}

# Cost-per-MQL is modelled as a fraction of the segment's ACV (expensive segments have
# expensive leads), divided by that segment's affinity for the channel. This keeps
# pipeline ROAS -- sql_rate * ACV / cost_per_mql -- inside a believable 2x-13x band
# while still leaving real, discoverable differences between channels.
CPMQL_ACV_FACTOR = {"linkedin_ads": 0.042, "google_search": 0.036, "display_retargeting": 0.058,
                    "email_nurture": 0.021, "webinar": 0.048, "podcast_sponsor": 0.055}
# Fraction of clicks that become an MQL, per channel.
CLICK_TO_MQL = {"linkedin_ads": 0.021, "google_search": 0.034, "display_retargeting": 0.008,
                "email_nurture": 0.048, "webinar": 0.190, "podcast_sponsor": 0.014}
CTR = {"linkedin_ads": 0.0062, "google_search": 0.0410, "display_retargeting": 0.0031,
       "email_nurture": 0.0185, "webinar": 0.0900, "podcast_sponsor": 0.0048}

segments = [
    {"id": i, "name": n, "industry": ind, "employee_band": eb,
     "reachable_accounts": sz, "avg_acv_usd": acv, "intent_score": iscore}
    for i, n, ind, eb, sz, acv, iscore in SEGMENTS
]

channels = [
    {"id": i, "name": n, "pricing_model": pm, "unit_cost_usd": uc,
     "min_viable_spend_usd": mvs, "lead_time_days": lt}
    for i, n, pm, uc, mvs, lt in CHANNELS
]

QUARTERS = ["2025-Q1", "2025-Q2", "2025-Q3", "2025-Q4", "2026-Q1", "2026-Q2"]

campaigns = []
cid = 0
for seg in segments:
    for ch in channels:
        aff = AFFINITY[seg["id"]][ch["id"]]
        for q in QUARTERS:
            # Not every segment/channel/quarter combination ran.
            if random.random() > (0.55 + 0.25 * min(aff, 1.5) / 1.5):
                continue
            cid += 1
            # Forward-consistent funnel, driven by cost per MQL. Spend is sized to hit a
            # target MQL volume (then floored at the channel's minimum viable spend), so
            # enterprise segments with expensive leads still get statistically meaningful
            # campaigns instead of 1-2 MQL rounding noise.
            cost_per_mql = (seg["avg_acv_usd"] * CPMQL_ACV_FACTOR[ch["id"]]
                            / aff * random.uniform(0.85, 1.15))
            target_mqls = random.uniform(14, 55)
            spend = round(max(target_mqls * cost_per_mql, ch["min_viable_spend_usd"] * 1.6), 2)
            mqls = max(1, round(spend / cost_per_mql))
            sql_rate = (0.18 + 0.24 * seg["intent_score"]) * random.uniform(0.88, 1.12)
            sqls = max(0, round(mqls * sql_rate))
            pipeline = round(sqls * seg["avg_acv_usd"] * random.uniform(0.88, 1.12), 2)

            clicks = max(mqls, round(mqls / CLICK_TO_MQL[ch["id"]]))
            impressions = max(clicks, round(clicks / CTR[ch["id"]]))

            campaigns.append({
                "campaign_id": f"cmp_{cid:04d}",
                "quarter": q,
                "segment_id": seg["id"],
                "channel_id": ch["id"],
                "spend_usd": spend,
                "impressions": impressions,
                "clicks": clicks,
                "mqls": mqls,
                "sqls": sqls,
                "pipeline_usd": pipeline,
            })

brand = {
    "company": "Northwind Analytics",
    "product": "Northwind Signal",
    "one_liner": "Revenue analytics that tells go-to-market teams which accounts to work next.",
    "tone": ["direct", "specific", "no hype", "numbers over adjectives"],
    "banned_phrases": ["revolutionary", "game-changing", "unlock the power", "seamlessly", "cutting-edge"],
    "compliance_notes": [
        "No unqualified ROI claims -- any number must cite a customer or a benchmark.",
        "Healthcare and public-sector copy must avoid implying HIPAA/FedRAMP certification.",
    ],
    "proof_points": [
        "Cut pipeline review prep from 6 hours to 40 minutes at Halden Logistics.",
        "38% of Northwind customers report a full quarter of forecast accuracy within 90 days.",
        "Deploys against Salesforce and HubSpot without a data-warehouse project.",
    ],
}

for name, payload in [("segments.json", segments), ("channels.json", channels),
                      ("campaigns.json", campaigns), ("brand.json", brand)]:
    (OUT / name).write_text(json.dumps(payload, indent=2) + "\n")
    print(f"wrote {name}: {len(payload) if isinstance(payload, list) else 'obj'}")

print(f"total historical campaign rows: {len(campaigns)}")
