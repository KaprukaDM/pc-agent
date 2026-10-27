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


# ---------------------------------------------------------------------------------------------------------------
# 2026-10-27, user: "make sure it checks the sitemap, finds possible links, goes into those links and filters out
# which are already in the sitemap". The word-overlap match alone missed real partners (a domain-derived name
# has no word boundaries; "Umandawa" vs slug "umandawa-online" scored only "uncertain"). Now: pick the possible
# partner pages from the sitemap, OPEN each one, and compare what is on the page with the candidate.

PAGES_CACHE_PATH = Path(__file__).resolve().parent / "data" / "partner_pages_cache.json"
PAGES_CACHE_DAYS = 7
PARTNER_URL = "https://www.kapruka.com/partner/"
_GENERIC = {"ceylon", "lanka", "srilanka", "sri", "foods", "food", "store", "stores", "shop", "shops", "online", "pvt",
            "ltd", "lk", "products", "product", "company", "international", "trading", "group", "collection",
            "collections", "fashion", "fashions", "traders", "enterprises", "creations", "designs", "design",
            "original", "natural", "organic", "premium", "best", "the", "and", "worldwide", "global", "supplies",
            "mart", "world", "house", "home", "gift", "gifts", "pet", "pets", "kids", "baby", "auto", "cars", "car"}


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _domain_stem(url: str) -> str:
    host = re.sub(r"^https?://(www\.)?", "", (url or "").strip().lower()).split("/")[0]
    host = re.sub(r"\.(lk|com|net|org|co|io|shop|store|info)$", "", host)
    stem = _norm(host.split(".")[-1] if "." in host else host)  # shop.alston.lk -> alston, not "shop"
    return "" if stem in _GENERIC else stem


def possible_partner_slugs(business_name: str, url: str | None = None, slugs: list[str] | None = None, limit: int = 3) -> list[str]:
    """Sitemap slugs worth opening: they share a distinctive word with the name, or one concatenated form
    contains the other (name or domain stem vs slug), 5+ characters."""
    slugs = slugs if slugs is not None else fetch_partner_slugs()
    name_words = {w for w in _words(business_name) if w not in _GENERIC and len(w) >= 4}
    keys = {k for k in (_norm(business_name), _domain_stem(url or "")) if len(k) >= 5}
    scored = []
    for slug in slugs:
        s_concat = _norm(slug)
        s_words = {w for w in _slug_to_words(slug) if w not in _GENERIC and len(w) >= 4}
        score = 0.0
        if name_words & s_words:
            score += 1 + len(name_words & s_words)
        for k in keys:
            if len(s_concat) >= 5 and (k in s_concat or s_concat in k):
                score += 3
        if score:
            scored.append((score, slug))
    scored.sort(key=lambda t: (-t[0], t[1]))
    return [s for _, s in scored[:limit]]


def _load_pages() -> dict:
    try:
        return json.loads(PAGES_CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def partner_page(slug: str) -> dict | None:
    """{title, h1, text_norm, digits} of https://www.kapruka.com/partner/<slug>, cached for a week. None if it
    could not be read (the caller then keeps the candidate: a false reject silently loses a real business)."""
    import requests
    cache = _load_pages()
    hit = cache.get(slug)
    if hit and time.time() - hit.get("at", 0) < PAGES_CACHE_DAYS * 86400:
        return hit
    try:
        r = requests.get(PARTNER_URL + slug, headers={"User-Agent": UA}, timeout=40)
    except Exception:
        return None
    if r.status_code != 200:
        return None
    import html as _html
    raw = _html.unescape(r.text)
    title = re.search(r"<title>(.*?)</title>", raw, re.S)
    h1 = re.search(r"<h1[^>]*>(.*?)</h1>", raw, re.S)
    body = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", raw, flags=re.S | re.I)
    body = re.sub(r"<[^>]+>", " ", body)
    entry = {"at": time.time(), "title": re.sub(r"\s+", " ", (title.group(1) if title else "")).strip()[:160],
             "h1": re.sub(r"<[^>]+>|\s+", " ", (h1.group(1) if h1 else "")).strip()[:160],
             "text_norm": _norm(body)[:60000], "digits": re.sub(r"\D", "", body)[:30000]}
    # an unknown slug serves a generic "YOUR LISTED ITEMS" page with no heading: that is not a partner page
    entry["real"] = bool(entry["h1"])
    cache[slug] = entry
    try:
        PAGES_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = PAGES_CACHE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
        tmp.replace(PAGES_CACHE_PATH)
    except Exception:
        pass
    return entry


def verify_already_partner(business_name: str, url: str | None = None, phones: list[str] | None = None,
                           slugs: list[str] | None = None) -> dict | None:
    """Open each possible partner page and compare. A match needs real evidence on the page: the business name or
    its domain in the page heading/title, or in the page body at least twice (their products are listed there), or
    one of its phone numbers. Returns {"slug", "reason"} or None."""
    name_key = _norm(business_name)
    dom_key = _domain_stem(url or "")
    tails = {re.sub(r"\D", "", p)[-9:] for p in (phones or []) if len(re.sub(r"\D", "", p)) >= 9}
    for slug in possible_partner_slugs(business_name, url, slugs):
        pg = partner_page(slug)
        if not pg or not pg.get("real"):
            continue
        head = _norm(pg["title"] + " " + pg["h1"])
        body = pg["text_norm"]
        for label, key in (("name", name_key), ("website name", dom_key)):
            # the page heading must START with it (or contain a long, distinctive form): a shared product word such as
            # "candle" in "Candle House Ceylon" must not count as the same business
            if len(key) >= 7 and key not in _GENERIC and (head.startswith(key) or (len(key) >= 9 and key in head)):
                return {"slug": slug, "reason": f"its {label} is the Partner Central page title ({pg['h1'] or pg['title']})"}
        # their products are listed on a partner's page under the brand name (Nai Miris sells through Artisan Miris)
        if len(name_key) >= 7 and name_key not in _GENERIC and body.count(name_key) >= 2:
            return {"slug": slug, "reason": f"its name appears {body.count(name_key)} times on the Partner Central page /partner/{slug}"}
        if any(t and t in pg["digits"] for t in tails):
            return {"slug": slug, "reason": f"its phone number is on the Partner Central page /partner/{slug}"}
    return None
