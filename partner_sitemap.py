"""Live Kapruka Partner Central roster — https://www.kapruka.com/scripts/sitemap_partners.jsp
lists every onboarded partner as https://www.kapruka.com/partner/<slug> (~700
entries, confirmed live 2026-10-01). This is an EXACT onboarded-partner list,
unlike the fuzzy live-product-search check metrics.py uses for category
coverage — brand_discovery.py uses this to skip (or flag) a candidate brand
that's already a Partner Central partner, so Kogul never re-pitches a brand
that's already on board (the Caravan Fresh / Skrumptious / Sokk Republic
confirmed-live cases: Sokk Republic and Skrumptious ARE already partner
slugs, Caravan Fresh and Mazeted are NOT, even though Caravan Fresh's
products appear elsewhere on kapruka.com outside the Partner Central
directory — this module answers "is it a Partner Central partner", nothing
broader).

Cached to a local file (same spirit as the main project's other scraped
snapshots) so every brand-discovery run doesn't re-fetch 700 rows of mostly
unchanging data; refreshed when the cache is older than CACHE_MAX_AGE_HOURS.
"""
from __future__ import annotations
import json
import re
import time
from pathlib import Path

CACHE_PATH = Path(__file__).resolve().parent / "data" / "partner_sitemap_cache.json"
CACHE_MAX_AGE_HOURS = 24
SITEMAP_URL = "https://www.kapruka.com/scripts/sitemap_partners.jsp"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

_STOPWORDS = {"the", "a", "an", "and", "or", "of", "pvt", "ltd", "inc", "store", "shop"}


def _words(s: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", (s or "").lower()) if w not in _STOPWORDS and len(w) > 1}


def _slug_to_words(slug: str) -> set[str]:
    return _words(re.sub(r"[-_]+", " ", slug))


def fetch_partner_slugs(force: bool = False) -> list[str]:
    """Returns every partner slug currently live in the sitemap. Cached to
    disk; a fetch failure falls back to the last good cache (if any) rather
    than crashing the whole brand-discovery run — stale-but-real data beats
    no data here, same fail-open spirit as the rest of this pipeline.

    2026-10-01, gap-audit fixes:
    - `import requests` is now OUTSIDE the network try/except and raises
      loudly (ImportError) rather than being swallowed identically to a
      network blip. Previously a missing/broken `requests` install looked
      exactly like "sitemap unreachable" with zero distinguishing signal —
      meaning the ENTIRE already-onboarded-partner check would silently
      return [] forever (every candidate treated as "not a partner," the
      exclusion permanently defeated) with nothing anywhere pointing at
      the real cause. A genuinely broken environment should fail loud here
      so it gets fixed, not fail quiet and corrupt every brand-discovery
      decision indefinitely.
    - Cache write is now atomic (temp file + os.replace), same reasoning as
      brand_discovery._mark_seen's fix — a crash mid-write can no longer
      leave a corrupt cache file that then fails every subsequent read too."""
    import requests  # noqa: F401 — intentionally outside the try/except below, see docstring

    if not force and CACHE_PATH.exists():
        try:
            cached = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
            age_hours = (time.time() - cached.get("fetched_at", 0)) / 3600
            if age_hours < CACHE_MAX_AGE_HOURS:
                return cached.get("slugs", [])
        except Exception:
            pass

    try:
        r = requests.get(SITEMAP_URL, headers={"User-Agent": UA}, timeout=30)
        r.raise_for_status()
        slugs = sorted(set(re.findall(r"/partner/([^<\s]+)", r.text)))
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = CACHE_PATH.with_suffix(".tmp")
        tmp_path.write_text(json.dumps({"fetched_at": time.time(), "slugs": slugs}, ensure_ascii=False),
                              encoding="utf-8")
        tmp_path.replace(CACHE_PATH)
        return slugs
    except Exception:
        if CACHE_PATH.exists():
            try:
                return json.loads(CACHE_PATH.read_text(encoding="utf-8")).get("slugs", [])
            except Exception:
                pass
        return []


def match_existing_partner(business_name: str, slugs: list[str] | None = None) -> dict | None:
    """Fuzzy word-overlap match (same style as metrics.match_daraz_category)
    against the live partner slug list. Returns None for no match at all.
    Otherwise returns {"slug", "confidence": "exact"|"likely"|"uncertain"} —
    the caller decides what to do with each tier: exact/likely should
    exclude the candidate outright, "uncertain" should keep the card but
    show a human-review flag rather than silently hiding a possibly-new
    brand (2026-10-01 user decision: fuzzy match + flag uncertain ones,
    never a hard silent exclude on a weak signal)."""
    if slugs is None:
        slugs = fetch_partner_slugs()
    if not slugs or not business_name:
        return None

    name_norm = re.sub(r"\s+", " ", business_name.strip().lower())
    # 2026-10-01, gap-audit fix: a domain-derived name guess (e.g.
    # "Sokkrepublic" from sokkrepublic.com) has no internal word boundaries
    # at all, so it can never overlap a hyphenated slug's words ("sokk",
    # "republic") via name_words & slug_words below — that comparison
    # requires actual word splitting on BOTH sides, which a single
    # concatenated token structurally cannot provide. A concatenated-form
    # comparison (strip every space/hyphen from both sides, compare as one
    # token) catches this case losslessly before falling through to
    # word-overlap scoring, independent of whichever name form the caller
    # happened to pass in.
    name_concat = re.sub(r"[\s_-]+", "", name_norm)
    for slug in slugs:
        slug_concat = re.sub(r"[\s_-]+", "", slug.strip().lower())
        if name_concat and name_concat == slug_concat:
            return {"slug": slug, "confidence": "exact"}

    name_words = _words(business_name)
    if not name_words:
        return None

    for slug in slugs:
        if re.sub(r"[-_]+", " ", slug.strip().lower()) == name_norm:
            return {"slug": slug, "confidence": "exact"}

    best_slug = None
    best_score = 0.0
    for slug in slugs:
        slug_words = _slug_to_words(slug)
        if not slug_words:
            continue
        overlap = len(name_words & slug_words)
        if overlap == 0:
            continue
        score = overlap / max(len(name_words), len(slug_words))
        if score > best_score:
            best_score = score
            best_slug = slug

    if best_slug is None:
        return None
    if best_score >= 0.75:
        return {"slug": best_slug, "confidence": "likely"}
    if best_score >= 0.4:
        return {"slug": best_slug, "confidence": "uncertain"}
    return None
