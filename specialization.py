"""Partner specialization gate — judges whether a sourced business genuinely
SPECIALIZES in the target category (it's their core business), vs. one that
merely happens to sell/mention the product somewhere on their site.

Built 2026-10-01, user: "make sure now u pick andidates where they
speciialize in that category they have that category as a whole business,
now when it comes to eyelashes see if theres any business that speicializes
in that".

Business Scout's existing `product_verified` flag (see app_ship.py's
_business_scout_scan / brightdata.js) only confirms a `site:<domain>
"<product term>"` Google hit exists somewhere on the domain — a general
pharmacy with one page mentioning "eyelash glue" passes that check exactly
the same as a dedicated lash-extension studio. That is real evidence the
product exists on the site, but it is NOT evidence the category is the
business's core offering, which is what matters for a partner-onboarding
pitch (a specialist has inventory depth, is motivated to grow that specific
line, and is a sharper Partner Central fit than an incidental seller).

Same semantic-gate pattern as worth_pursuing.py: one batched Claude CLI call
reasoning over the TEXT SIGNALS already in hand (business name, which source
it came from, website/listing URL, address) — no new scraping, no new
Business Scout call. Fail-open on any error (keep every candidate
unjudged/unfiltered) for the same reason every other filter in this
pipeline fails open: a false reject here silently removes a real partner
from the list before a human ever sees it; a false "specialist" tag just
costs one slightly-less-sharp outreach message.
"""
from __future__ import annotations
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from claude_cli import run_claude_cli, extract_json  # noqa: E402

_PROMPT_TEMPLATE = """You are reviewing candidate businesses found for a Kapruka.com partner-onboarding
outreach list for the category "{concept}".

Goal: tell apart a business that SPECIALIZES in "{concept}" (it is their core business, or a
clearly dedicated product line — e.g. a dedicated lash-extension studio for "eyelashes", a
dedicated car-audio installer for "car interior accessories") from a business that merely
ALSO sells or mentions "{concept}" as one minor item among an unrelated main business (e.g. a
general pharmacy, a general gift shop, a general electronics store, a general supermarket).

A specialist is a MUCH better partner-outreach target: they have real inventory depth in this
exact category, a direct incentive to grow that line, and a sharper fit for a Partner Central
pitch than an incidental seller does.

Judge each candidate below using only its name, listing source, website/URL, and address — the
same weak signals a human reviewer would glance at before calling. If the name or URL clearly
names the category/a close synonym as the business itself (e.g. "XYZ Lash Studio", "ABC
Guitars", "Colombo Car Audio"), that is strong specialist evidence. A generic business name
with no category signal (e.g. "City Mart", "ABC Traders", a general pharmacy/supermarket/mall
listing) is NOT a specialist, even if Business Scout confirmed the product appears somewhere
on their site. When genuinely unclear from the available signals, mark is_specialist false but
say so plainly in the reason rather than guessing yes.

Output ONLY a JSON array, one object per candidate, in the same order, each exactly:
{{"name": "<candidate name>", "is_specialist": true|false, "reason": "<one short sentence why>"}}.
No other text, no markdown fences.

Candidates:
{candidates_block}
"""


def _build_candidates_block(concept: str, candidates: list[dict]) -> str:
    lines = []
    for c in candidates:
        bits = [f'name: "{c.get("name") or "(no name)"}"']
        if c.get("source"):
            bits.append(f'source: {c["source"]}')
        if c.get("url"):
            bits.append(f'url: {c["url"]}')
        if c.get("address"):
            bits.append(f'address: {c["address"]}')
        lines.append("- " + ", ".join(bits))
    return "\n".join(lines)


def judge_specialization(concept: str, candidates: list[dict], model: str = "claude-sonnet-5",
                          batch_size: int = 15, timeout: int = 180) -> list[dict]:
    """Returns `candidates` in the same order, each with 'is_specialist'
    (bool) and 'specialization_reason' (str) fields added. Never drops a
    candidate — this is a ranking signal, not a filter (see module
    docstring: a false reject here silently loses a real partner, so the
    caller decides how to use the tag, e.g. sort specialists first while
    keeping non-specialists eligible)."""
    if not candidates:
        return []

    out: list[dict] = []
    for i in range(0, len(candidates), batch_size):
        batch = candidates[i:i + batch_size]
        prompt = _PROMPT_TEMPLATE.format(concept=concept, candidates_block=_build_candidates_block(concept, batch))
        try:
            raw = run_claude_cli(prompt, model=model, timeout=timeout)
            verdicts = extract_json(raw)
            verdict_by_name = {v["name"]: v for v in verdicts if isinstance(v, dict) and "name" in v}
        except Exception:
            # fail-open: tag as unjudged, keep every candidate as-is
            for c in batch:
                out.append({**c, "is_specialist": False,
                            "specialization_reason": "(specialization check failed — unjudged)"})
            continue

        for c in batch:
            v = verdict_by_name.get(c.get("name") or "(no name)")
            if v is None:
                out.append({**c, "is_specialist": False,
                            "specialization_reason": "(specialization check returned no verdict — unjudged)"})
                continue
            out.append({**c, "is_specialist": bool(v.get("is_specialist")),
                        "specialization_reason": v.get("reason") or ""})

    return out
