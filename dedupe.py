"""Same-business-line dedupe gate for Weekly Outreach candidates.

Built 2026-10-01, user: "make sure theres no overlap between suggested
category coz one partner can carry more than one category products on the
same business line". Confirmed real with the earlier screenshot (Drums
Percussion / Folk World Percussion both pointing at the same drum shop) and
live Daraz taxonomy data — Musical Instruments alone has Drums Percussion,
Folk World Percussion, Marching Percussion, and Concert Percussion as
separate category_gap rows, every one of which a single real percussion
retailer in Sri Lanka would plausibly carry at once. Sourcing two (or four)
near-identical categories in the same week wastes slots that could have
gone to genuinely different opportunities.

User explicitly rejected a plain word-overlap heuristic ("fix this but
lets change the model a bit") in favor of a SEMANTIC judgment — same
pattern as worth_pursuing.py and specialization.py: one batched Claude CLI
call reasons over the real-world business question "would ONE realistic
Sri Lankan retailer plausibly carry both of these as the same product
line?", not a mechanical word-match (which would both over-trigger on
coincidental shared words and under-trigger on real same-line pairs that
don't share vocabulary, e.g. "Guitars" + "Guitar Amplifiers").

Fail-open, same as every other filter in this pipeline: on any error, no
candidate is merged away — every row proceeds exactly as if the dedupe
pass never ran. A false merge here silently costs a real distinct
opportunity its weekly slot; a false non-merge just means two categories
that happen to share a partner (not a correctness error, just one unused
efficiency on that reviewer's end).
"""
from __future__ import annotations
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from claude_cli import run_claude_cli, extract_json  # noqa: E402

_PROMPT_TEMPLATE = """You are reviewing a shortlist of candidate product categories being considered for
Kapruka.com partner-outreach this week. Each category becomes an outreach slot, and the goal is
weekly VARIETY — each slot should represent a genuinely different partner-outreach opportunity.

The practical problem: if two categories on this list would realistically be carried by the SAME
single Sri Lankan retailer as one product line (e.g. "Drums Percussion" and "Folk World
Percussion" — any real percussion shop sells both; or "Guitars" and "Guitar Amplifiers" — a real
guitar shop sells both), sourcing BOTH wastes a weekly slot reaching out to what is functionally
the same partner twice, when that second slot could have gone to a real, different opportunity.

Judge this as a real retail business question, not a word-matching exercise: would one ordinary,
realistically-scoped Sri Lankan retailer plausibly carry BOTH as the same core business line?
Two categories that merely share a word are NOT automatically the same line (e.g. "Car Video" and
"Video Game Consoles" share no real retail overlap despite unrelated meanings; "Interior
Accessories" and "Exterior Accessories" for cars often ARE the same auto-accessories retailer).
Conversely two categories with no shared vocabulary can still be the same line (e.g. "Guitars"
and "Guitar Amplifiers").

Below is the candidate list, each with an id. Group them into clusters where every category in a
cluster would be carried by the same realistic single retailer. A category with no real overlap
to anything else is its own cluster of one. Output ONLY a JSON array of clusters, each cluster an
array of the ids that belong together, covering every id exactly once. No other text, no markdown
fences. Example shape: [["c1"],["c2","c5"],["c3"],["c4","c6","c7"]]

Candidates:
{candidates_block}
"""


def _build_candidates_block(candidates: list[dict]) -> str:
    lines = []
    for i, c in enumerate(candidates):
        lines.append(f'- id "c{i}": "{c["concept"]}" (department: {c.get("department") or "unknown"})')
    return "\n".join(lines)


def dedupe_same_business_line(candidates: list[dict], model: str = "claude-sonnet-5",
                                batch_size: int = 25, timeout: int = 180,
                                score_key: str = "_dedupe_score") -> list[dict]:
    """Groups `candidates` into same-business-line clusters via one batched
    Claude CLI call and keeps only the HIGHEST-scored survivor per
    cluster (score read from candidate[score_key] if present, else the
    list's own order — callers that have already computed a priority
    score should attach it under `score_key` before calling so the right
    survivor wins, not just whichever happened to sort first).

    Batched (not one global call) for the same reason worth_pursuing.py
    batches: keeps each prompt a manageable size. A real near-duplicate
    pair split across two batches won't be caught — acceptable, since
    batches preserve the pool's existing priority order (highest-score
    first), so same-line pairs that matter most (both high-scoring) are
    likely to land in the same early batch together.

    Fail-open on any error: that batch's candidates are returned
    unmerged, exactly as given."""
    if not candidates:
        return []

    out: list[dict] = []
    for i in range(0, len(candidates), batch_size):
        batch = candidates[i:i + batch_size]
        prompt = _PROMPT_TEMPLATE.format(candidates_block=_build_candidates_block(batch))
        try:
            raw = run_claude_cli(prompt, model=model, timeout=timeout)
            clusters = extract_json(raw)
            id_to_candidate = {f"c{j}": c for j, c in enumerate(batch)}
            seen_ids = set()
            survivors = []
            for cluster in clusters:
                cluster_candidates = [id_to_candidate[cid] for cid in cluster if cid in id_to_candidate]
                if not cluster_candidates:
                    continue
                for cid in cluster:
                    seen_ids.add(cid)
                best = max(cluster_candidates, key=lambda c: c.get(score_key, 0) or 0)
                survivors.append(best)
            # any id the model dropped from its own output — fail-open, keep it
            for j, c in enumerate(batch):
                if f"c{j}" not in seen_ids:
                    survivors.append(c)
            out.extend(survivors)
        except Exception:
            out.extend(batch)

    return out
