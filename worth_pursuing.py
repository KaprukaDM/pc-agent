"""Semantic "is this category worth pursuing" gate for Weekly Outreach.

Built 2026-09-30/10-01 after the user flagged a real strategic concern:
"we dont want our website to be another daraz we are like an invite only
very specific" — clarified through discussion (not literal "premium"/
"luxury" wording, which CLAUDE.md's brand voice rules explicitly ban) into
a concrete, evidence-tested judgment:

Investigated the user's 4 "good" examples (Car Interior Accessories,
Guitars, 3D Printers, Drums Percussion) against 4 deliberately "bad"/
commodity comparisons (Baby Diapers, Coffee Grinders, Sports Nutrition,
Ladders & Workbenches) using real Daraz data (revenue_proxy, avg_price,
genuine_count, total_products) — the raw NUMBERS are structurally
identical across both groups, so no numeric threshold can build this
filter. The user's own answer for what actually separates them: (1)
giftability — could this be bought AS a gift (matches business-context.md's
Gifting-first strategy), (2) self-expression/passion/hobby category, not
household utility — identity and creative pursuit vs. pure function, (3)
"some premium uniqueness" — distinctive/non-interchangeable, not
mass-commodity (a quality signal, not literal Kapruka-facing copy — see
KOGUL.md's own ban on the word "premium" in anything said publicly).

This is a semantic judgment, not a numeric filter — hence one Claude CLI
call per generate run, not a keyword/threshold rule. Runs BEFORE the
expensive live-Kapruka-check/Daraz-match metrics step (user decision:
reject fast, don't pay for live HTTP checks on something that was going to
be rejected anyway) and ALONGSIDE the existing human approve/reject gate,
never replacing it — a category that passes this gate still needs a
manual Approve click before partners are sourced.
"""
from __future__ import annotations
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from claude_cli import run_claude_cli, extract_json  # noqa: E402

_PROMPT_TEMPLATE = """You are screening candidate product categories for Kapruka.com, a Sri Lankan
e-commerce site, before any partner-outreach work is spent on them.

Kapruka's business context: mostly GIFTING (emotional purchases, often diaspora buying for
someone back home) and ECOM SELF-PURCHASE (practical, considered purchases) — NOT a
general marketplace competing to list every commodity category, and explicitly not trying
to become "another Daraz."

A category is WORTH PURSUING if it's a reasonable fit for AT LEAST ONE of:
  - Giftable — a real person would plausibly buy this FOR someone else as a gift.
  - Passion / self-expression / hobby category — about a person's identity, creativity, or a
    considered hobby pursuit (e.g. musical instruments, car customization, maker/hobby tech),
    not pure household utility or function.
  - Distinctive / specialty — a real, non-interchangeable product class a buyer researches and
    cares about, not generic mass-commodity goods anyone could buy anywhere.

A category is NOT worth pursuing if it's purely functional household/commodity utility with
none of the above — e.g. diapers, coffee grinders, step ladders, protein powder — even if it
has real sales volume on Daraz. High revenue or demand does NOT by itself make something worth
pursuing; Kapruka deliberately does not chase every commodity category just because demand
exists.

For each candidate below, decide pass or reject. Output ONLY a JSON array, one object per
candidate, in the same order, each exactly: {{"concept": "<the concept text>", "worth_pursuing":
true|false, "reason": "<one short sentence why>"}}. No other text, no markdown fences.

Candidates:
{candidates_block}
"""


def _build_candidates_block(candidates: list[dict]) -> str:
    lines = []
    for c in candidates:
        lines.append(f'- "{c["concept"]}" (department: {c.get("department") or "unknown"}, '
                      f'source: {c.get("source") or "unknown"})')
    return "\n".join(lines)


def filter_worth_pursuing(candidates: list[dict], model: str = "claude-sonnet-5",
                            batch_size: int = 15, timeout: int = 180) -> list[dict]:
    """Returns only the candidates judged worth_pursuing=True, each with a
    'worth_pursuing_reason' field added (kept for transparency — shown on
    the category card). On any failure for a batch (CLI error, bad JSON),
    that batch's candidates are KEPT, not silently dropped — a false
    reject here permanently hides a real opportunity from the human
    reviewer who would otherwise catch it; a false keep just costs one
    extra manual Reject click, the same asymmetry already applied to every
    other filter in this pipeline (see sourcing.py's _looks_onboardable
    docstring for the same reasoning)."""
    if not candidates:
        return []

    out: list[dict] = []
    for i in range(0, len(candidates), batch_size):
        batch = candidates[i:i + batch_size]
        prompt = _PROMPT_TEMPLATE.format(candidates_block=_build_candidates_block(batch))
        try:
            raw = run_claude_cli(prompt, model=model, timeout=timeout)
            verdicts = extract_json(raw)
            verdict_by_concept = {v["concept"]: v for v in verdicts if isinstance(v, dict) and "concept" in v}
        except Exception:
            # fail-open: keep the whole batch, unjudged, rather than lose real candidates
            for c in batch:
                out.append({**c, "worth_pursuing_reason": "(worth-pursuing check failed — kept unfiltered)"})
            continue

        for c in batch:
            v = verdict_by_concept.get(c["concept"])
            if v is None:
                # model dropped this one from its output — same fail-open reasoning
                out.append({**c, "worth_pursuing_reason": "(worth-pursuing check returned no verdict — kept unfiltered)"})
                continue
            if v.get("worth_pursuing"):
                out.append({**c, "worth_pursuing_reason": v.get("reason") or ""})
            # else: dropped — genuinely rejected, not an error case

    return out
