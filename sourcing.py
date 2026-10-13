"""Weekly category sourcing for Kogul — reads the main Opportunity Tool's
own data modules directly (never re-implements the underlying analysis),
same files/functions app_ship.py's own tabs are built on. No dependency on
app_ship.py actually running — these are plain file/SQLite reads via the
project's existing scripts/ modules, same pattern _daraz_product_titles_for_slug
etc. already use elsewhere in this project.

Per user direction 2026-09-30 ("start with source GSC, Daraz and from
expand catalog and new category opportunities"): candidates now come from
THREE real sources, not just the keyword-bank verdict file (which had
confirmed noise — cricket matches, brand names — see memory:
kogul-partner-onboarding-agent, Build 1's noise finding):

1. GSC thin-catalog (Expand Catalog tab's GSC sub-tab) — our own real
   search demand for a category/find_online page whose live product count
   is thin. Real GSC impressions, real product counts, no fuzzy matching.
2. Daraz thin-catalog (Expand Catalog tab's Daraz sub-tab) — we already
   have the page and Daraz proves the demand (revenue_proxy), but our
   product depth is thin. Category-native, no keyword-to-category join
   needed.
3. Daraz new-category gap (New Category Opportunities tab's Daraz
   sub-tab) — status='category_gap' rows: a real, named Daraz category
   with proven revenue_proxy and (near-)zero Kapruka presence. This is the
   strongest, cleanest signal available — a real product category, not a
   search term, with real revenue evidence attached directly (no join).

The old keyword-bank source (category_opportunities_bank/*.json) is kept
as a 4th, lowest-priority fallback only when the other three don't fill
the week's quota — its known noise means it should never be the primary
source, but a formal cricket-free category can still surface real gaps in
departments the Daraz scan hasn't covered as deeply.
"""
from __future__ import annotations
import json
import re
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

BANK_DIR = PROJECT_ROOT / "data" / "category_opportunities_bank"
DB_STORE_PATH = PROJECT_ROOT / "data_store.sqlite"

_STOPWORDS = {"the", "a", "an", "for", "and", "or", "in", "sri", "lanka", "online", "buy"}


def _words(s: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", (s or "").lower()) if w not in _STOPWORDS and len(w) > 2}


# Reused, not reinvented — same real vocabulary already proven against live
# data elsewhere in this project (memory: keyword-bank-off-topic-gate,
# build_keyword_bank_big.py's is_off_topic): restaurant/fast-food-chain
# queries and generic price/near-me modifiers are searches for information,
# not a product category a partner could supply. GSC's thin-catalog rows
# don't carry a department vocabulary the way the bank does, so this is a
# simpler, department-agnostic pass — reject on ANY hit rather than the
# bank's more nuanced "does the department's own vocabulary rescue it"
# escape hatch. That's intentionally more aggressive here because a false
# reject just means one fewer GSC-sourced pick for a human to review (there
# are 3 other sources plus the bank fallback), not a lost opportunity the
# way a bank-only false reject would be.
_GSC_FOOD_CHAINS = {
    "kfc", "mcdonald", "mcdonalds", "domino", "dominos", "pizzahut",
    "subway", "starbucks", "burgerking", "wendys", "popeyes", "nando", "nandos",
}
_GSC_FOOD_SERVICE = {
    "restaurant", "restaurants", "pizzeria", "cafe", "buffet", "takeaway",
    "dine", "diner", "eatery", "hotel", "hotels", "resort",
}
_GSC_INFO_MODIFIERS = [
    "price in sri lanka", "price sri lanka", "sri lanka price", "near me",
    "near by me", "nearby", "cost of", "how much",
]

# 2026-10-01, user: "Avoid accessories also" — said right after the 3D
# Printing/accessories false-positive fix (that category's Daraz page was
# overwhelmingly filament/nozzles/parts, not printers). Same family of
# problem, generalized: a category whose own NAME signals it's an
# accessories/parts/components sub-line (e.g. "3D Printer Parts",
# "Interior Accessories", "Camera Accessories", "Microwave Accessories")
# is not a good standalone partner-outreach target — a partner selling
# accessories for category X is a much weaker, narrower pitch than one
# selling X itself, and these are frequently just the "junk drawer"
# sub-category under the umbrella-row problem the earlier fix already
# targets. Matched on the category's own words (not substring, so
# "Accessory Dwelling Units" style false hits are structurally
# impossible here since this is Daraz product taxonomy, not free text).
_ACCESSORY_WORDS = {"accessories", "accessory", "parts", "spares", "components"}


def _is_accessory_category(concept: str) -> bool:
    return bool(_words(concept) & _ACCESSORY_WORDS)


def _looks_onboardable_gsc(query: str) -> bool:
    """Live-confirmed 2026-09-30: source_gsc_thin_catalog's top rows included
    'beer price in sri lanka', 'kfc sri lanka', 'liquor price in sri lanka' —
    real GSC demand, but for a price lookup or a specific restaurant chain,
    neither of which is a category a Sri Lankan business could partner with
    Kapruka to supply. Filtering these here (not in category_health.py
    itself, which serves other, unrelated tabs that have their own reasons
    to keep this data unfiltered)."""
    q_lower = (query or "").lower()
    for phrase in _GSC_INFO_MODIFIERS:
        if phrase in q_lower:
            return False
    words = set(re.findall(r"[a-z0-9]+", q_lower))
    if words & _GSC_FOOD_CHAINS:
        return False
    if words & _GSC_FOOD_SERVICE:
        return False
    return True


# ------------------------------------------------------- Source 1: GSC thin-catalog

def source_gsc_thin_catalog(limit: int = 60) -> list[dict]:
    """Real GSC demand vs. a thin page — same computation Expand Catalog's
    GSC sub-tab runs (category_health.thin_catalog_opportunities), read
    directly rather than over HTTP so this works without app_ship.py
    running."""
    try:
        import category_health as ch
    except Exception:
        return []
    try:
        rows = ch.flatten_nodes()
        # Ask for more than `limit` raw items up front since
        # _looks_onboardable_gsc discards some (chain/price/info noise) —
        # filtering AFTER an early slice would silently under-fill the
        # quota below what the caller asked for.
        res = ch.thin_catalog_opportunities(rows, limit=limit * 3)
    except Exception:
        return []
    out = []
    for it in res.get("items", []):
        if len(out) >= limit:
            break
        query = it.get("query") or it.get("path") or ""
        if not _looks_onboardable_gsc(query):
            continue
        # thin_catalog_opportunities' rows carry "path" (e.g. "Cakes >
        # Caravan Fresh"), not a bank-style department slug — its first
        # segment is the department label. weekly_categories.department is
        # NOT NULL, so this can never be left None (confirmed live: an
        # earlier version left this None and would have thrown a SQLite
        # IntegrityError on every GSC-sourced row's insert).
        department = (it.get("path") or "").split(">")[0].strip() or "Unknown"
        out.append({
            "source": "gsc_thin_catalog",
            "department": department,
            "concept": it.get("query") or it.get("path"),
            "concept_volume": it.get("query_impressions") or 0,
            "gsc_impressions": it.get("query_impressions"),
            "kapruka_url": it.get("url"),
            "kapruka_product_count": it.get("product_count"),
            "daraz_revenue_proxy": None,
            "opportunity_type": None,
            "action": f"GSC demand ({it.get('query_impressions')} impressions) for a thin page "
                       f"({it.get('product_count')} products) — {it.get('path')}",
            "source_ref": it.get("url"),
        })
    return out


# ---------------------------------------------------- Source 2: Daraz thin-catalog

def source_daraz_thin_catalog(limit: int = 60) -> list[dict]:
    """We have the page, Daraz proves the demand, our depth is thin — same
    join Expand Catalog's Daraz sub-tab uses (daraz_categories JOIN
    category_product_counts.json), read directly from the DB/file rather
    than app_ship.py's cached in-process view."""
    if not DB_STORE_PATH.exists():
        return []
    counts_path = PROJECT_ROOT / "data" / "category_product_counts.json"
    try:
        counts = json.loads(counts_path.read_text(encoding="utf-8"))
    except Exception:
        counts = {}

    conn = sqlite3.connect(DB_STORE_PATH)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT slug, daraz_name, breadcrumb, kapruka_department, genuine_count, "
            "revenue_proxy, units_sold_sample, daraz_url, kapruka_url, kapruka_url_type "
            "FROM daraz_categories WHERE kapruka_url IS NOT NULL ORDER BY revenue_proxy DESC"
        ).fetchall()
    finally:
        conn.close()

    out = []
    for r in rows:
        if r["kapruka_url_type"] == "find_online":
            continue
        c = counts.get(r["kapruka_url"])
        if not isinstance(c, dict):
            continue
        listed = c.get("product_count")
        if listed is None or listed >= 10:
            continue
        out.append({
            "source": "daraz_thin_catalog",
            "department": r["kapruka_department"] or "other",
            "concept": r["daraz_name"],
            "concept_volume": r["units_sold_sample"] or 0,
            "gsc_impressions": None,
            "kapruka_url": r["kapruka_url"],
            "kapruka_product_count": listed,
            "daraz_revenue_proxy": r["revenue_proxy"],
            "opportunity_type": None,
            "action": f"Daraz proves demand (revenue proxy {r['revenue_proxy']:,} LKR) for "
                       f"'{r['daraz_name']}' — we have the page but only {listed} product(s)",
            "source_ref": r["daraz_url"],
        })
        if len(out) >= limit:
            break
    return out


# ------------------------------------------------- Source 3: Daraz new-category gap

def source_daraz_new_category_gap(limit: int = 60) -> list[dict]:
    """status='category_gap' — a real, named Daraz category with proven
    revenue and (near-)zero Kapruka presence. The strongest signal: no
    keyword-to-category fuzzy join needed, real revenue evidence attached
    directly (see New Category Opportunities tab's Daraz sub-tab,
    app_ship.py's _new_opps_daraz_all).

    UMBRELLA-ROW EXCLUSION (2026-10-01, user screenshot of daraz.lk/3d-printing/:
    "this is not a good category to pursue coz its not technically 3d
    printers") — Daraz's own taxonomy nests leaf categories under broader
    umbrella ones (e.g. "3D Printing" > "3D Printers" / "3D Printer Parts" /
    "3D Printing Materials", all four independently present as their own
    category_gap rows with their own breadcrumbs). The PARENT row's product
    count/revenue is an aggregate across its own children's products, so
    "3D Printing" (parent) looked like a bigger opportunity than "3D
    Printers" (the actual hardware leaf) purely by revenue, even though
    pitching a partner on "3D Printing" the umbrella is incoherent — Daraz's
    own page under that label is overwhelmingly filament/nozzles/resin/
    enclosures, not printers. Confirmed app-wide, not a one-off: 90 of 3185
    category_gap rows are a parent of another category_gap row in this
    dataset (Diapering & Potty, Formula Milk, Laundry And Home Care, Media
    Players, etc.). Fix: skip any row whose breadcrumb is a strict PREFIX
    of another category_gap row's breadcrumb — i.e. it has a sibling leaf
    already in this same gap set — so the more specific, more pitchable
    child is sourced instead of the vaguer parent. A parent row with NO
    leaf children present in category_gap (most of them) is unaffected and
    still sourced normally."""
    if not DB_STORE_PATH.exists():
        return []
    conn = sqlite3.connect(DB_STORE_PATH)
    conn.row_factory = sqlite3.Row
    try:
        all_rows = conn.execute(
            "SELECT slug, daraz_name, breadcrumb, kapruka_department, genuine_count, "
            "total_products, units_sold_sample, avg_price, revenue_proxy, daraz_url "
            "FROM daraz_categories WHERE status = 'category_gap'"
        ).fetchall()
    finally:
        conn.close()

    breadcrumbs = [(r["breadcrumb"] or "") for r in all_rows]
    rows = []
    for r in all_rows:
        bc = r["breadcrumb"] or ""
        is_umbrella = any(other_bc.startswith(bc + " > ") for other_bc in breadcrumbs if other_bc != bc)
        if is_umbrella:
            continue
        rows.append(r)
    rows.sort(key=lambda r: r["revenue_proxy"] or 0, reverse=True)
    rows = rows[:limit]

    out = []
    for r in rows:
        out.append({
            "source": "daraz_new_category_gap",
            "department": r["kapruka_department"] or "other",
            "concept": r["daraz_name"],
            "concept_volume": r["units_sold_sample"] or 0,
            "gsc_impressions": None,
            "kapruka_url": None,
            "kapruka_product_count": r["genuine_count"],
            "daraz_revenue_proxy": r["revenue_proxy"],
            "opportunity_type": "new",
            "action": f"No Kapruka category for '{r['daraz_name']}' (breadcrumb: {r['breadcrumb']}) — "
                       f"Daraz revenue proxy {r['revenue_proxy']:,} LKR, we have {r['genuine_count']} product(s)",
            "source_ref": r["daraz_url"],
        })
    return out


# --------------------------------------------------- Source 4: keyword bank (fallback)

def _looks_onboardable(row: dict) -> bool:
    """Defensive filter on top of the bank's own 'opportunity' verdict.
    Live-confirmed 2026-09-30: sports_fitness's top 'opportunity' rows by
    volume are cricket-match search terms ("sri vs ind", "pak vs sri live",
    751K/mo, is_foreign=True, relevant_in_sample 2-3 out of a 20-sample
    against unrelated grocery products) — informational/foreign search
    noise, not a real product category, that slipped through the bank's
    own verdict pass. Same failure class already documented in memory
    (category-opportunities-from-bank: 'the verdict logic has no semantic
    is-this-actually-a-product-category check'). A false keep here means
    Kogul spends real partner outreach on a category nobody can sell into;
    filtering is cheap and the signal (near-zero relevant-sample ratio, or
    is_foreign) is strong enough not to risk a false reject on a real
    niche/diaspora product term."""
    if row.get("is_foreign"):
        return False
    sample_size = row.get("sample_size") or 0
    relevant = row.get("relevant_in_sample") or 0
    if sample_size >= 10 and relevant / sample_size < 0.15:
        return False
    return True


def _load_bank_dept(dept_file: Path) -> list[dict]:
    try:
        data = json.loads(dept_file.read_text(encoding="utf-8"))
    except Exception:
        return []
    dept = data.get("summary", {}).get("department") or dept_file.stem
    out = []
    for row in data.get("rows", []):
        if row.get("verdict") != "opportunity":
            continue
        if not _looks_onboardable(row):
            continue
        out.append({
            "source": "keyword_bank",
            "department": dept,
            "concept": row.get("concept"),
            "concept_volume": row.get("concept_volume") or 0,
            "gsc_impressions": row.get("gsc_impressions"),
            "kapruka_url": None,
            "kapruka_product_count": row.get("live_product_count"),
            "daraz_revenue_proxy": None,
            "opportunity_type": row.get("opportunity_type"),
            "action": row.get("action"),
            "source_ref": f"data/category_opportunities_bank/{dept_file.name}#{row.get('concept_key')}",
        })
    return out


def source_keyword_bank(limit: int = 60) -> list[dict]:
    if not BANK_DIR.exists():
        return []
    all_rows: list[dict] = []
    for f in sorted(BANK_DIR.glob("*.json")):
        all_rows.extend(_load_bank_dept(f))
    type_rank = {"new": 0, "real": 1, None: 2}
    all_rows.sort(key=lambda r: (-r["concept_volume"], type_rank.get(r["opportunity_type"], 2)))
    return all_rows[:limit]


# ------------------------------------------------------------------ Combined picker

def top_category_opportunities(limit: int, exclude_keys: set[str] | None = None) -> list[dict]:
    """Combines all 4 sources, GSC/Daraz-native sources first (real
    category names, real revenue/impression evidence, no fuzzy matching),
    keyword-bank only as a fallback to fill remaining quota. Within each
    source, ranked by its own best demand signal (revenue proxy for Daraz
    sources, impressions for GSC, concept_volume for the bank fallback) —
    these are different units, so sources are concatenated in priority
    order rather than merged into one cross-source numeric sort, which
    would silently let LKR revenue outrank impression counts or vice versa
    with no real basis for the comparison."""
    exclude_keys = exclude_keys or set()

    daraz_gap = source_daraz_new_category_gap(limit=limit * 2)
    daraz_thin = source_daraz_thin_catalog(limit=limit * 2)
    gsc_thin = source_gsc_thin_catalog(limit=limit * 2)

    combined = daraz_gap + daraz_thin + gsc_thin
    combined = [r for r in combined if r["concept"] not in exclude_keys]
    combined = [r for r in combined if not _is_accessory_category(r["concept"])]

    if len(combined) < limit:
        bank_rows = source_keyword_bank(limit=limit * 3)
        seen_concepts = {r["concept"] for r in combined}
        bank_rows = [r for r in bank_rows if r["concept"] not in exclude_keys and r["concept"] not in seen_concepts
                     and not _is_accessory_category(r["concept"])]
        combined += bank_rows

    return combined[:limit]


def available_departments() -> list[str]:
    if not BANK_DIR.exists():
        return []
    return sorted(f.stem for f in BANK_DIR.glob("*.json"))
