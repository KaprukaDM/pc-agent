"""Per-category metrics for the Weekly Outreach tab: Google Ads demand (from
the bank, already Ads-sourced), GSC impressions (also already in the bank
row), Daraz revenue proxy (overseas-excluded, best-effort matched from the
bank's keyword concept to a Daraz category — two different keys, no direct
join exists elsewhere in this project, see memory:
kogul-partner-onboarding-agent), and a LIVE Kapruka product count (sitemap
existence + a live site search seeded from the matched Daraz category's own
real product names, reusing kapruka_live_verify.py rather than
re-implementing search-scraping here).

User's routing rule, reused verbatim from category_opportunities_from_bank.py
(memory: category-opportunities-from-bank) for the final verdict column:
  0 products, no page    -> new_opportunity
  0 products, page exists -> real_opportunity
  1-4 products            -> expand
  5+, no page             -> seo_profile_opportunity
  5+, page exists         -> covered
"""
from __future__ import annotations
import json
import re
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

DB_STORE_PATH = PROJECT_ROOT / "data_store.sqlite"
BIG_BANK_DIR = PROJECT_ROOT / "data" / "keyword_bank_big"

_STOPWORDS = {"the", "a", "an", "for", "and", "or", "in", "sri", "lanka", "online", "buy"}

# Sri-Lanka-vs-international Ads demand split (user, 2026-09-30: "google
# ads demand is not only lanka demand" / "break google ads in sri lanka
# demand and international demand"). category_opportunities_bank's
# concept_volume is a SUM across every geo a concept was matched in
# (confirmed by reading build_keyword_bank_big.py's group_concepts/
# save_merged directly — real example: "seiko 5" sums Australia + Canada +
# Sri Lanka + UAE + UK + US into one 900/mo figure with no per-geo
# breakdown carried into the output file). The real per-geo rows DO exist,
# one level earlier in the pipeline, in data/keyword_bank_big/<dept>.json
# — re-deriving the split means re-grouping those raw rows by the same
# concept_key() the bank pipeline itself uses, so results match exactly.
#
# Cost: a department's raw file is large (electronics: 464,537 rows, ~11s
# to load + ~10s to scan for ONE concept measured live) — reloading/
# rescanning per concept would make Weekly Outreach generation
# impractically slow (up to num_cats*6 concepts checked per run). Fixed
# with a per-department in-process cache: the raw file is loaded and
# grouped into a {concept_key: {sl, intl}} index ONCE per department per
# process lifetime, then every concept lookup in that department is an
# O(1) dict hit. Cache is module-level and never invalidated within a
# process — acceptable because keyword_bank_big files only change via an
# explicit rebuild script the user runs manually, never during normal
# Kogul operation.
_geo_split_cache: dict[str, dict[str, dict]] = {}


def _load_geo_split_index(department: str) -> dict[str, dict]:
    if department in _geo_split_cache:
        return _geo_split_cache[department]
    import build_keyword_bank_big as bkbb

    path = BIG_BANK_DIR / f"{department}.json"
    index: dict[str, dict] = {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        for r in data.get("rows", []):
            key = bkbb.concept_key(r.get("keyword") or "")
            if not key:
                continue
            vol = r.get("avg_monthly_searches") or 0
            entry = index.setdefault(key, {"sl": 0, "intl": 0})
            if r.get("geo") == "Sri Lanka":
                entry["sl"] += vol
            else:
                entry["intl"] += vol
    except Exception:
        index = {}
    _geo_split_cache[department] = index
    return index


def split_ads_demand(concept: str, department: str | None) -> dict:
    """Returns {"sri_lanka": int|None, "international": int|None} — None
    for both when the department's raw big-bank file isn't available or
    the concept isn't found in it (e.g. it came from GSC/Daraz sourcing,
    not the keyword bank, so it was never Ads-priced in the first place)."""
    if not department:
        return {"sri_lanka": None, "international": None}
    import build_keyword_bank_big as bkbb
    index = _load_geo_split_index(department)
    if not index:
        return {"sri_lanka": None, "international": None}
    key = bkbb.concept_key(concept or "")
    entry = index.get(key)
    if not entry:
        return {"sri_lanka": None, "international": None}
    return {"sri_lanka": entry["sl"], "international": entry["intl"]}


def _words(s: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", (s or "").lower()) if w not in _STOPWORDS and len(w) > 2}


def match_daraz_category(concept: str) -> dict | None:
    """Best-effort word-overlap match of a bank concept against
    daraz_categories.daraz_name/breadcrumb — same style as
    relevant_title_count/matching_category_nodes elsewhere in this project
    (word-subset scoring, not substring). Returns the single best-scoring
    row (score > 0) or None — a None result is shown as 'no Daraz category
    matched' by the caller, never coerced to a zero revenue figure.

    Real bug found + fixed 2026-09-30: scoring by word-overlap against
    daraz_name UNION breadcrumb (not daraz_name alone) let a PARENT
    category's row outscore or tie the actual exact-name match, because
    Daraz's own breadcrumb embeds the parent chain — "Baby Diapers"'s
    breadcrumb is literally "Home > Diapering & Potty > Mother & Baby >
    Disposable Diapers", so searching for concept "Diapering & Potty"
    scored "Baby Diapers" a perfect 1.0 (both concept words present via
    the breadcrumb) tying the ACTUAL "Diapering & Potty" row at the same
    score — and iteration order (not correctness) decided which one won.
    Confirmed live: this picked "Baby Diapers" (wrong) over the real
    "Diapering & Potty" row, though it happened not to corrupt that
    particular verdict because live_kapruka_check's raw-concept fallback
    term still found the right products independently — a category
    without that lucky fallback could get a genuinely wrong Daraz match.
    Fix: an EXACT name match (case/whitespace-insensitive) is checked
    FIRST and wins immediately if found, before any breadcrumb-inflated
    scoring runs at all — the real, exact category always outranks a
    same-scoring relative."""
    concept_words = _words(concept)
    if not concept_words:
        return None
    if not DB_STORE_PATH.exists():
        return None
    conn = sqlite3.connect(DB_STORE_PATH)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT slug, daraz_name, breadcrumb, revenue_proxy, genuine_count, total_products, "
            "units_sold_sample, kapruka_department, daraz_url FROM daraz_categories"
        ).fetchall()
    finally:
        conn.close()

    concept_norm = re.sub(r"\s+", " ", concept.strip().lower())
    for r in rows:
        if re.sub(r"\s+", " ", (r["daraz_name"] or "").strip().lower()) == concept_norm:
            return dict(r)

    best = None
    best_score = 0.0
    for r in rows:
        label_words = _words(r["daraz_name"]) | _words(r["breadcrumb"])
        if not label_words:
            continue
        overlap = len(concept_words & label_words)
        if overlap == 0:
            continue
        score = overlap / max(len(concept_words), 1)
        if score > best_score:
            best_score = score
            best = r
    if best is None or best_score < 0.5:
        return None
    return dict(best)


def _daraz_product_names(slug: str, n: int = 8) -> list[str]:
    """Real Daraz product titles for a category slug, for seeding the live
    Kapruka search — mirrors app_ship.py's _daraz_product_titles_for_slug
    (same field name gotcha already documented there: product_examples
    entries use "name", not "title")."""
    import json
    path = PROJECT_ROOT / "data" / "daraz_scan_results.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    row = next((r for r in data.get("results", []) if isinstance(r, dict) and r.get("slug") == slug), None)
    if row is None:
        return []
    ex = [p for p in (row.get("product_examples") or []) if isinstance(p, dict)]
    ex.sort(key=lambda p: (p.get("sold") or 0), reverse=True)
    return [p.get("name") for p in ex[:n] if p.get("name")]


def live_kapruka_check(concept: str, daraz_match: dict | None) -> dict:
    """LIVE product-count + page-existence check on kapruka.com.

    Primary search/profile-check term is a DERIVED real product term when
    one is available, not the raw Daraz category label. Two real bugs led
    here, both live-confirmed 2026-09-30:

    1. Anchoring on raw Daraz product TITLES (long, spec-heavy strings)
       broke via smart_live_search's head-noun fallback reducing an
       unmatched title to its LAST WORD — "...Wallpaper...Home Decoration"
       became a search for "decoration", falsely matching 11 unrelated
       party-decor products and reporting wallpaper as "covered".
    2. Anchoring on the raw Daraz CATEGORY LABEL instead (the first fix for
       #1) broke differently: "Serum & Essence" and "Portable Audio
       Microphones" are taxonomy labels, not real product vocabulary —
       Kapruka genuinely has "serum" and "portable microphone" products,
       confirmed live by the user (a real find_online/serum page, a real
       search hit for Portable Microphone), but find_existing_profile and
       the search both correctly reported no match for the BROADER label,
       by deliberate design (a broad label is never treated as covered by
       a narrower existing page — see find_existing_profile's own
       docstring history). This is the exact "Daraz category names are
       taxonomy labels, not product vocabulary" bug already fixed for
       other call sites in this project (category-search-term-derivation).

    Fix: derive_category_search_term() builds a real term from what
    Daraz's OWN product titles in that category actually call the thing
    (word co-occurrence, never a single independently-common word stitched
    on — see its own docstring for the false-positive history that
    shaped it).

    2026-09-30 redesign, per user request ("I need of search terms links
    generated - u can generated the search term from daraz products, url
    category name and then try each on kapruka and build the product
    count across alll"): generates a term from EACH of 3 sources — the
    derived Daraz-product-title term, the Daraz category's own URL slug
    (words split out of it), and the raw concept/category name — tries
    EVERY one on Kapruka's live search, and UNIONS the relevant products
    found across all of them (deduped by product URL, so a product two
    terms both happen to match is counted once, not twice) rather than
    just keeping whichever single term scored highest. Every term's real,
    clickable Kapruka search URL is returned in search_terms_tried so a
    human can verify any of them directly (same transparency principle as
    the Google-search/Daraz-link additions on the category card).

    Every result is still relevance-filtered against the CONCEPT's own
    words (not each individual search term's), preserving the wallpaper
    fix's guarantee that a noisy/narrower term can't launder an off-topic
    match back in as a hit."""
    import urllib.parse
    import kapruka_live_verify as klv
    import partner_report as pr

    daraz_titles = _daraz_product_names(daraz_match["slug"], n=12) if daraz_match else []
    derived_term = klv.derive_category_search_term(concept, daraz_titles) if daraz_titles else None

    # Terms to search, one per source, deduped (derived from Daraz titles, the Daraz URL slug,
    # the raw concept). 2026-10-15 (user: "change it app wide"): the count now comes from the
    # SAME like-for-like engine as the Partner Report (partner_report.kapruka_check): the main
    # term is read to the last results page (not just page 1), the other terms are searched too,
    # and every Kapruka product is judged against what this category really is (the concept plus
    # Daraz's own example products), so "oil" for skin care no longer counts cooking oil.
    candidate_terms: list[str] = []
    if derived_term:
        candidate_terms.append(derived_term)
    if daraz_match and daraz_match.get("slug"):
        slug_term = re.sub(r"[-_]+", " ", daraz_match["slug"]).strip()
        if slug_term:
            candidate_terms.append(slug_term)
    candidate_terms.append(concept)
    seen_terms, search_terms = set(), []
    for t in candidate_terms:
        key = t.lower().strip()
        if key and key not in seen_terms:
            seen_terms.add(key)
            search_terms.append(t)

    # The category's own name leads. Words derived from Daraz product titles are only extra search
    # terms, and only when they share a word with the category: "cap turban head" (for Hijabs: baby
    # turban hats) and "light tea" (for Soy Candles: tea lights) were found wrong live 2026-10-15.
    primary_term = concept
    cw = klv._wordset(concept)
    kind_terms = [t for t in search_terms if t.lower() != concept.lower() and (klv._wordset(t) & cw)][:2]
    rng = {"search": primary_term, "kind": f"{concept} (the product category, as Daraz sells it)",
           "kind_terms": kind_terms, "examples": daraz_titles[:5]}
    try:
        res = pr.kapruka_check([rng])
    except Exception as e:
        return {"ok": False, "error": str(e)[:200], "live_product_count": None, "search_terms_tried": [],
                "primary_term": primary_term, "sample_titles": [], "category_exists": None,
                "existing_profile": None, "verdict": "check_failed"}
    k = res[0] if res else {"verdict": "failed", "error": "no result"}
    if k.get("verdict") == "failed":
        return {"ok": False, "error": k.get("error"), "live_product_count": None, "search_terms_tried": [],
                "primary_term": primary_term, "sample_titles": [], "category_exists": None,
                "existing_profile": None, "verdict": "check_failed"}

    count = k["count"]
    terms_tried_detail = [{"term": t.split(" (")[0], "ok": True, "error": None,
                           "search_url": f"{klv.SEARCH_URL}?d={urllib.parse.quote(t.split(' (')[0])}",
                           "count": None} for t in (k.get("terms_tried") or [])]
    if terms_tried_detail:
        terms_tried_detail[0]["count"] = count
    sample_titles = [x["title"] for x in (k.get("samples") or [])[:8]]

    try:
        profile = klv.find_existing_profile(primary_term)
    except Exception:
        profile = None
    category_exists = bool(profile)

    if count == 0 and not category_exists:
        verdict = "new_opportunity"
    elif count == 0 and category_exists:
        verdict = "real_opportunity"
    elif 1 <= count <= 4:
        verdict = "expand"
    elif count >= 5 and not category_exists:
        verdict = "seo_profile_opportunity"
    else:
        verdict = "covered"

    return {"ok": True, "error": None, "live_product_count": count,
             "category_product_count": k.get("category_count"),
             "search_terms_tried": terms_tried_detail, "primary_term": primary_term,
             "sample_titles": sample_titles, "category_exists": category_exists,
             "existing_profile": profile, "verdict": verdict}


def build_category_metrics(concept: str, concept_volume: int, gsc_impressions: int | None,
                             department: str | None = None, source: str | None = None) -> dict:
    """Ads + GSC come straight from the bank row (already-sourced,
    concept_volume IS the Ads demand figure — see build_keyword_bank_big.py).
    Daraz + live Kapruka are computed here.

    source-aware demand fields (2026-09-30 fix — user found "Google Ads
    Demand" was mislabeled: daraz_new_category_gap/daraz_thin_catalog rows
    set concept_volume to Daraz's OWN units_sold_sample, never real Ads
    data, so every current Weekly Outreach card was showing a Daraz sales
    figure under an "Ads" label):
      - source == "keyword_bank": concept_volume IS real Ads data.
        ads_sri_lanka/ads_international carry the real per-geo split
        (split_ads_demand, only attempted for this source — it's a
        wasted 10-35s department-file load for every other source, which
        was never Ads-priced under this concept in the first place).
      - source in ("daraz_new_category_gap", "daraz_thin_catalog"):
        concept_volume is Daraz's units_sold_sample — surfaced as
        daraz_units_sold, NOT as ads_volume/ads_sri_lanka/ads_international
        (all three left None so the UI shows a dash, never a wrong number).
      - source == "gsc_thin_catalog" or unknown: no Ads data either way;
        all Ads fields None."""
    daraz_match = match_daraz_category(concept)
    live = live_kapruka_check(concept, daraz_match)

    is_daraz_source = source in ("daraz_new_category_gap", "daraz_thin_catalog")
    is_bank_source = source == "keyword_bank"

    geo_split = split_ads_demand(concept, department) if is_bank_source else {"sri_lanka": None, "international": None}

    return {
        "ads_volume": concept_volume if is_bank_source else None,
        "ads_sri_lanka": geo_split["sri_lanka"],
        "ads_international": geo_split["international"],
        "daraz_units_sold": concept_volume if is_daraz_source else None,
        "gsc_impressions": gsc_impressions,
        "daraz_matched_category": daraz_match["daraz_name"] if daraz_match else None,
        "daraz_matched_slug": daraz_match["slug"] if daraz_match else None,
        "daraz_revenue_proxy": daraz_match["revenue_proxy"] if daraz_match else None,
        "daraz_units_sold_sample": daraz_match["units_sold_sample"] if daraz_match else None,
        "live_kapruka_product_count": live.get("live_product_count"),
        "live_kapruka_category_exists": live.get("category_exists"),
        "live_kapruka_sample_titles": live.get("sample_titles"),
        "live_kapruka_search_terms_tried": live.get("search_terms_tried"),
        "live_check_error": live.get("error"),
        "final_verdict": live.get("verdict") if live.get("ok") else "check_failed",
    }
