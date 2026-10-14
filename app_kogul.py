"""Kogul — partner-onboarding researcher & outreach agent. Standalone Flask
app (own DB, own port) that reads the main Opportunity Tool's category-gap
data read-only and calls its /api/find_partners over HTTP for candidate
partners — same integration shape Business Scout already uses (a separate
app calling INTO app_ship.py), not the reverse. See memory:
kogul-partner-onboarding-agent for the full spec and decisions.

WhatsApp has two modes, set by the `whatsapp_live` setting: "0" (default)
logs every partner-facing message as simulated (nothing is sent), "1" sends
it for real through the Cloud API. Every outbound message goes through
_send_and_log, so the mode is enforced in exactly one place.

Access: requests from this machine are allowed; anything else needs HTTP
basic auth with KOGUL_ADMIN_PASSWORD from .env (only /webhook/whatsapp is
public, and it verifies Meta's signature itself).

Run: python kogul/app_kogul.py   (defaults to :5010)
"""
from __future__ import annotations
import json
import os
import re
import sys
import threading
import time
import uuid
from pathlib import Path

from flask import Flask, jsonify, render_template, request

sys.path.insert(0, str(Path(__file__).resolve().parent))
import db  # noqa: E402
import specialization  # noqa: E402
import brain  # noqa: E402
import brand_discovery  # noqa: E402
import attachments  # noqa: E402
import partner_report  # noqa: E402
from claude_cli import run_claude_cli, run_openai_chat_text  # noqa: E402

APP_DIR = Path(__file__).resolve().parent
app = Flask(__name__, template_folder=str(APP_DIR / "templates"), static_folder=str(APP_DIR / "static"))
# 2026-10-06 fix: without this, Flask caches the compiled kogul.html template
# in memory on first render and never re-reads the file — confirmed live, a
# template edit (new empty-state copy) didn't show up until the process was
# restarted, unlike KOGUL.md/settings which are read fresh every time. Static
# files (kogul.css) were never affected by this — Flask serves those straight
# off disk regardless.
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.config["MAX_CONTENT_LENGTH"] = 80 * 1024 * 1024  # 5 attachments x 15 MB + form overhead

_SCRIPTS_DIR = str(APP_DIR.parent / "scripts")
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)
from _env import env as _project_env  # noqa: E402

OPPORTUNITY_TOOL_URL = "http://127.0.0.1:5002"  # app_ship.py — override below if the VPS port differs
FIND_PARTNERS_TIMEOUT = 150


class _JobStore(dict):
    """In-memory job results. Finished jobs older than an hour are dropped on
    each new write so the dict can't grow forever."""
    _TTL = 3600

    def __setitem__(self, key, value):
        now = time.time()
        if isinstance(value, dict):
            value.setdefault("_t", now)
        stale = [k for k, v in self.items()
                 if isinstance(v, dict) and v.get("status") != "running" and now - v.get("_t", now) > self._TTL]
        for k in stale:
            del self[k]
        super().__setitem__(key, value)


_JOBS: dict[str, dict] = _JobStore()
_JOBS_LOCK = threading.Lock()

db.init_db()


# ------------------------------------------------------------- access guard

def _is_local_request() -> bool:
    # A reverse proxy on the same box adds X-Forwarded-For, so proxied traffic
    # is never mistaken for a local user.
    return (request.remote_addr in ("127.0.0.1", "::1")
            and not request.headers.get("X-Forwarded-For")
            and not request.headers.get("Forwarded"))


@app.before_request
def _access_guard():
    if request.path.startswith("/webhook/whatsapp"):
        return None
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        source = request.headers.get("Origin") or request.headers.get("Referer")
        if source:
            from urllib.parse import urlparse
            if urlparse(source).netloc != request.host:
                return jsonify({"ok": False, "error": "cross-site request refused"}), 403
    if _is_local_request():
        return None
    password = os.environ.get("KOGUL_ADMIN_PASSWORD") or _project_env().get("KOGUL_ADMIN_PASSWORD") or ""
    if not password:
        return ("Remote access is disabled: set KOGUL_ADMIN_PASSWORD in .env to allow it.", 403)
    import hmac
    auth = request.authorization
    if not auth or not hmac.compare_digest((auth.password or "").encode(), password.encode()):
        return ("Login required.", 401, {"WWW-Authenticate": 'Basic realm="PC Agent"'})
    return None


# --------------------------------------------------------------------- util

def _find_partners(business: str, breadcrumb: str | None = None) -> list[dict]:
    """Calls the existing /api/find_partners endpoint in app_ship.py — never
    re-implements business discovery here (same principle app_ship.py's own
    docstring states for why IT proxies to Business Scout instead of
    re-implementing scraping).

    Then runs the results through specialization.judge_specialization
    (2026-10-01, user: "make sure now u pick candidates where they
    specialize in that category they have that category as a whole
    business") and re-sorts specialists first. Business Scout's own
    `product_verified` flag only confirms the product is MENTIONED
    somewhere on the domain (a general pharmacy selling one lash product
    passes that too) — this is a stronger, separate signal: is the
    category the business's actual core offering. A non-specialist is
    NOT dropped, only ranked lower — same fail-open/graduated-priority
    principle as every other filter in this pipeline, since a hard
    exclude here could remove the only real candidate for a niche
    category with no dedicated specialists in Sri Lanka at all."""
    import requests
    params = {"business": business}
    if breadcrumb:
        params["breadcrumb"] = breadcrumb
    r = requests.get(f"{OPPORTUNITY_TOOL_URL}/api/find_partners", params=params, timeout=FIND_PARTNERS_TIMEOUT)
    r.raise_for_status()
    data = r.json()
    if not data.get("ok"):
        return []
    partners = data.get("partners", [])
    if not partners:
        return partners

    try:
        model = db.get_settings().get("claude_model", "claude-sonnet-5")
        judged = specialization.judge_specialization(business, partners, model=model)
    except Exception:
        return partners  # fail-open: unjudged, original order, nothing lost

    judged.sort(key=lambda p: (0 if p.get("is_specialist") else 1,
                                0 if p.get("product_verified") else 1,
                                p.get("name") or ""))
    return judged


def _week_start(ts: float | None = None) -> str:
    t = time.localtime(ts or time.time())
    # Monday of the current week
    import datetime
    d = datetime.date(t.tm_year, t.tm_mon, t.tm_mday)
    monday = d - datetime.timedelta(days=d.weekday())
    return monday.isoformat()


# ------------------------------------------------------------- dashboard

@app.route("/")
def index():
    return render_template("kogul.html")


@app.route("/api/dashboard")
def api_dashboard():
    """Pipeline stage counts across a DATE RANGE (2026-10-13 redesign, user:
    "remove this week, add a date range filter that will dynamically change
    the values... a pipeline interface" — replaces the old single-week-only
    view). Scoped by partners.created_at — user confirmed "first contacted",
    so a partner stays in the cohort it was first reached in regardless of
    when it last replied, matching how weekly batches already group things.
    No date params = all-time (every partner ever sourced), not "this week".

    2026-10-13, user: "make sure partner chats have only these fitlers and
    every chat falls into these filteres" — a bare status='new' partner (not
    yet messaged, "Start outreach" button shown) fell outside every pipeline
    stage: not reached (nothing sent yet), not in_progress, not any outcome.
    REDESIGN per user decision: "Reached" dropped entirely (was really just
    "status != new", a rollup, not a real stage). status='new' promoted to
    its own real, MUTUALLY EXCLUSIVE stage "uncontacted" — same pattern as
    agreed/unresponsive/not_agreed, no overlap with in_progress.
    "in_progress" renamed "Still chatting" in the UI (kept as in_progress
    here in the API — only the label changed, still contacted/replied/
    negotiating only, unchanged).
    Final 6 real, mutually exclusive stages (every status value maps to
    exactly one): uncontacted -> in_progress ("Still chatting") -> agreed ->
    unresponsive -> not_agreed. needs_handover stays a separate OVERLAY flag
    on top of whichever of those a partner is in (user confirmed earlier
    this session), not one of the 6 itself."""
    date_from = request.args.get("date_from", "").strip()
    date_to = request.args.get("date_to", "").strip()
    with db.conn() as c:
        # Global, NEVER date-filtered — the nav badge polls this every 30s
        # regardless of whatever date range the Dashboard tab happens to be
        # showing (see kogul.html's setInterval); a handover from outside
        # the visible range is just as real and just as easy to miss.
        handover_count_global = c.execute(
            "SELECT COUNT(*) AS n FROM partners WHERE needs_handover = 1"
        ).fetchone()["n"]

        q = "SELECT status, not_agreed_reason, needs_handover FROM partners WHERE status != 'removed'"
        params = []
        if date_from:
            q += " AND created_at >= ?"
            params.append(date_from)
        if date_to:
            q += " AND created_at <= ?"
            params.append(date_to + "T23:59:59")
        rows = c.execute(q, params).fetchall()

    uncontacted = sum(1 for r in rows if r["status"] == "new")
    agreed = sum(1 for r in rows if r["status"] == "agreed")
    not_agreed = sum(1 for r in rows if r["status"] == "not_agreed")
    unresponsive = sum(1 for r in rows if r["status"] == "unresponsive")
    in_progress = sum(1 for r in rows if r["status"] in ("contacted", "replied", "negotiating"))
    needs_handover = sum(1 for r in rows if r["needs_handover"])

    reasons: dict[str, int] = {}
    for r in rows:
        if r["status"] == "not_agreed" and r["not_agreed_reason"]:
            key = r["not_agreed_reason"].strip()
            # 2026-10-13: "pending — asked why" is a placeholder brain.py
            # writes the instant a bare decline arrives (see partner_reply's
            # NOT_AGREED handling) so the badge flips immediately rather
            # than waiting on the reason round-trip — it's not a real reason
            # and would just pollute this breakdown while the real answer is
            # still in flight, so it's excluded here specifically.
            if key.lower() == "pending — asked why":
                continue
            reasons[key] = reasons.get(key, 0) + 1
    top_reasons = sorted(reasons.items(), key=lambda kv: -kv[1])[:10]

    return jsonify({"ok": True, "date_from": date_from or None, "date_to": date_to or None,
                     "total_partners": len(rows),
                     "uncontacted": uncontacted, "in_progress": in_progress, "needs_handover": needs_handover,
                     "agreed": agreed, "unresponsive": unresponsive, "not_agreed": not_agreed,
                     "top_not_agreed_reasons": [{"reason": k, "count": v} for k, v in top_reasons],
                     "handover_count": handover_count_global})


@app.route("/api/stats/by_department")
def api_stats_by_department():
    """All-time (across every weekly batch, not just the current week) per-
    department breakdown — 2026-10-06, user: "also i want a stats n
    dashboard how many foundfrom each dep and how many agreed from dep and
    disagreed and no repsosnse".

    2026-10-08 fix (user, seeing a real empty "No partners sourced yet" card
    despite 61 real businesses already found: "this should fill with so far
    sourced") — root cause: this used to INNER JOIN weekly_categories to
    partners, so 'found' only counted a row once a human had clicked
    Approve (the step that actually creates the partners row — see
    api_category_approve). Every business brand_discovery finds becomes a
    weekly_categories row immediately, long before anyone approves it, so
    with 0 approvals yet, the inner join legitimately returned nothing —
    'found' was silently measuring 'approved', not 'found'. Switched to a
    LEFT JOIN from weekly_categories (every real sourced business, rejected
    ones excluded — a rejection means it was never actually pursued) so
    'found' counts every one regardless of whether outreach has started;
    agreed/not_agreed/unresponsive/in_progress still come from the
    partners row's real status where one exists, and 'not_yet_contacted'
    covers a found business with no partners row yet (not approved, or
    approved but no partner row created yet)."""
    with db.conn() as c:
        rows = c.execute(
            """SELECT wc.department, p.status FROM weekly_categories wc
               LEFT JOIN partners p ON p.weekly_category_id = wc.id AND p.status != 'removed'
               WHERE wc.review_status != 'rejected'"""
        ).fetchall()
    by_dept: dict[str, dict] = {}
    for r in rows:
        d = by_dept.setdefault(r["department"], {
            "department": r["department"], "found": 0, "agreed": 0,
            "not_agreed": 0, "unresponsive": 0, "in_progress": 0, "not_yet_contacted": 0,
        })
        d["found"] += 1
        if r["status"] == "agreed":
            d["agreed"] += 1
        elif r["status"] == "not_agreed":
            d["not_agreed"] += 1
        elif r["status"] == "unresponsive":
            d["unresponsive"] += 1
        elif r["status"] in ("contacted", "replied", "negotiating", "new"):
            d["in_progress"] += 1
        else:  # no partners row at all yet (status is NULL from the LEFT JOIN)
            d["not_yet_contacted"] += 1
    departments = sorted(by_dept.values(), key=lambda d: -d["found"])
    totals = {
        "found": sum(d["found"] for d in departments),
        "agreed": sum(d["agreed"] for d in departments),
        "not_agreed": sum(d["not_agreed"] for d in departments),
        "unresponsive": sum(d["unresponsive"] for d in departments),
        "in_progress": sum(d["in_progress"] for d in departments),
        "not_yet_contacted": sum(d["not_yet_contacted"] for d in departments),
    }
    return jsonify({"ok": True, "departments": departments, "totals": totals})


# --------------------------------------------------------- weekly categories

@app.route("/api/weekly/current")
def api_weekly_current():
    """2026-10-05, user: "bring all last week business" / "only if rejected
    move it out" — a new calendar week starting should NOT reset the
    Weekly Outreach list to empty. If there's no batch for the CURRENT
    week yet, this falls back to the most recent PRIOR batch (whatever week
    it belongs to) instead — same batch_id, so every partner/conversation
    there stays exactly as it was, no copying/duplication. A business only
    ever leaves the visible list by being explicitly rejected (already
    filtered below); "a new week started" is never itself a reason to lose
    one. api_weekly_generate's top-up path (see its docstring) targets this
    SAME carried-forward batch when the user clicks "Find more businesses",
    so new finds land in it rather than starting a separate parallel batch."""
    week = _week_start()
    with db.conn() as c:
        batch = c.execute(
            "SELECT * FROM weekly_batches WHERE week_start = ? ORDER BY id DESC LIMIT 1", (week,)
        ).fetchone()
        if not batch:
            batch = c.execute(
                "SELECT * FROM weekly_batches ORDER BY id DESC LIMIT 1"
            ).fetchone()
        if not batch:
            return jsonify({"ok": True, "has_batch": False, "week": week})
        cats = c.execute(
            "SELECT * FROM weekly_categories WHERE batch_id = ? ORDER BY concept_volume DESC", (batch["id"],)
        ).fetchall()
        # 2026-10-03, user: looking at a screenshot of 5 already-rejected
        # category-gap rows still rendering as full cards ("delete these") —
        # a rejected row is never actually deleted (per the earlier "keep
        # until it gets rejected" decision: rejecting just flips status, the
        # row and its history stay in the DB forever), but it has no reason
        # to keep occupying a full card's worth of visual space in the feed
        # once it's been acted on. Filtered out here rather than client-side
        # so the payload itself drops them too; rejected_count is still
        # reported separately so "20 live, 5 rejected" stays visible as a
        # one-line fact instead of the total silently looking smaller with
        # no explanation.
        rejected_count = sum(1 for cat in cats if cat["review_status"] == "rejected")
        cats = [cat for cat in cats if cat["review_status"] != "rejected"]
        out = []
        for cat in cats:
            partners = c.execute(
                "SELECT * FROM partners WHERE weekly_category_id = ? AND status != 'removed' ORDER BY id",
                (cat["id"],)
            ).fetchall()
            cat_dict = dict(cat)
            try:
                cat_dict["metrics"] = json.loads(cat_dict.pop("metrics_json") or "{}")
            except Exception:
                cat_dict["metrics"] = {}
                cat_dict.pop("metrics_json", None)
            for json_col in ("brand_images", "social_links", "brand_phones", "brand_emails"):
                try:
                    cat_dict[json_col] = json.loads(cat_dict.get(json_col) or "[]")
                except Exception:
                    cat_dict[json_col] = []
            out.append({**cat_dict, "partners": [dict(p) for p in partners],
                        "report": _report_summary(c, cat["id"])})
        # Re-sort display by the stored priority_score rather than the SQL
        # ORDER BY above (raw concept_volume DESC) — kept from the old
        # mixed-pool design where this mattered a lot more (an 'expand'
        # category could otherwise outrank a true 0-product gap). Since
        # 2026-10-03 every weekly_categories row is a brand_discovery row
        # (see _run_weekly_generate's REDESIGN note — bare category rows are
        # never inserted any more), so this is now mostly a no-op tie-break
        # among equal-scored rows; it's kept only so any OLDER batch still in
        # the DB from before that change (a real mix of category + brand
        # rows) keeps rendering with brand cards pinned above category cards,
        # each group by its own priority_score — older rows with no
        # priority_score fall back to 0 and sort last, never crash.
        out.sort(key=lambda c: (0 if c.get("source") == "brand_discovery" else 1,
                                 -(c.get("priority_score") or 0)))
    # "week" reflects the BATCH actually being shown (which may be a prior
    # week carried forward, see this route's docstring), not necessarily
    # today's calendar week — the frontend's "Week of ..." label should
    # never claim a date that doesn't match the data underneath it.
    # 2026-10-13, user: "have a rejection tab" — the top-reject-reasons
    # breakdown (and individual rejected rows) moved to its own
    # /api/rejections endpoint + Rejections tab, so it isn't duplicated here.
    return jsonify({"ok": True, "has_batch": True, "week": batch["week_start"], "batch": dict(batch),
                     "categories": out, "rejected_count": rejected_count})


@app.route("/api/weekly/generate", methods=["POST"])
def api_weekly_generate():
    """Sources this week's businesses via brand_discovery. Runs as a
    background job — multiple rounds of real web search + site fetches can
    take minutes.

    2026-10-03 (user: "i dont want u to loose currently whats found keep
    that in the list until it gets rejected", then later "no keep on
    adding" after a first version of this blocked a second Generate click
    once the batch reached num_cats live businesses): running Generate
    again for a week that ALREADY has a batch no longer errors out, and
    does NOT cap the week's total at num_cats either — every click searches
    for num_cats MORE real businesses on top of whatever's already there,
    same as the very first generate for a week. num_cats (the "categories
    to expand per week" setting) is really "how many to add per Generate
    click" now, not a week-total ceiling. Existing rows are NEVER touched
    (no delete, no status change, no re-insert) — a rejected row still
    exists in the DB (just hidden from the feed, see api_weekly_current),
    and _run_weekly_generate's own exclude_business_names (every concept
    ever stored for this batch, regardless of status) prevents a
    previously-rejected business from silently reappearing."""
    week = _week_start()
    settings = db.get_settings()
    num_cats = int(settings.get("num_categories_per_week", 10))
    per_cat = int(settings.get("partners_per_category", 4))

    # 2026-10-02 redesign: self-discovery + Opportunity-Tool-verdict-driven
    # search are the two equal-priority sources now; BRAND_SEEDS.txt is a
    # supplementary idea-list, not the primary quota-filler (see
    # brand_discovery.source_brand_discovery's docstring). Default is 1.0 —
    # search always gets full room; this override exists mainly for a test
    # run that wants to force an all-seeds pool (pass 0.0) or verify a
    # specific split.
    body = request.get_json(silent=True) or {}
    try:
        web_search_share = float(body.get("web_search_share", 1.0))
    except (TypeError, ValueError):
        web_search_share = 1.0
    web_search_share = min(max(web_search_share, 0.0), 1.0)

    with db.conn() as c:
        existing = c.execute("SELECT id FROM weekly_batches WHERE week_start = ?", (week,)).fetchone()
        if not existing:
            # 2026-10-05, user: "bring all last week business" — mirrors
            # api_weekly_current's fallback: a new calendar week with no
            # batch of its own yet still has a carried-forward batch from a
            # prior week, and clicking "Find more businesses" should top up
            # THAT one rather than starting a separate parallel batch the
            # user would never see (api_weekly_current always shows the
            # latest batch, so a second one here would be orphaned).
            existing = c.execute("SELECT id FROM weekly_batches ORDER BY id DESC LIMIT 1").fetchone()
        if existing:
            batch_id = existing["id"]
            # 2026-10-03 (user: "no keep on adding" — after "nothing to top
            # up" blocked a Generate click once the batch reached num_cats
            # live businesses). num_cats is no longer treated as a hard cap
            # on the week's total; clicking Generate always searches for
            # `num_cats` MORE businesses on top of whatever's already there,
            # same as the first-ever generate for a week does. The earlier
            # "only top up the shortfall, refuse once full" behavior fully
            # defeats a user who explicitly wants to keep growing the list —
            # existing rows are still never touched (no delete, no status
            # change, no re-insert), only new ones get added.
            job_id = uuid.uuid4().hex
            with _JOBS_LOCK:
                _JOBS[job_id] = {"status": "running", "error": None, "batch_id": batch_id}
            threading.Thread(target=_run_weekly_generate,
                              args=(job_id, week, num_cats, per_cat, web_search_share),
                              kwargs={"existing_batch_id": batch_id}, daemon=True).start()
            return jsonify({"ok": True, "job_id": job_id, "top_up": True,
                             "batch_id": batch_id, "requested": num_cats})

    job_id = uuid.uuid4().hex
    with _JOBS_LOCK:
        _JOBS[job_id] = {"status": "running", "error": None, "batch_id": None}
    threading.Thread(target=_run_weekly_generate,
                      args=(job_id, week, num_cats, per_cat, web_search_share), daemon=True).start()
    return jsonify({"ok": True, "job_id": job_id})


_BRAND_DISCOVERY_SCORE = 4.5  # ABOVE new_opportunity (4), the highest category verdict
# weight — raised from 3.5 on 2026-10-01, user: "now we agreed put business
# name at first over categories / so weekly it should generate businesss...
# with the criterias". Businesses are the PRIMARY weekly output now: a brand
# candidate has already cleared its own semantic fit gate (judge_brand_fit)
# against its real fetched site — strictly more evidence than any category
# row's unverified Daraz/GSC signal.
#
# 2026-10-03: category rows no longer exist as a second pool at all (see
# _run_weekly_generate's REDESIGN note) — every weekly_categories row is now
# a brand_discovery row, so this score is really just the fixed
# priority_score every row gets, kept as a named constant (rather than a
# bare literal at the insert site) since /api/weekly/current's sort and
# historical rows inserted at the old 3.5 both still read it as "the brand
# tier."


def _insert_weekly_category_row(batch_id: int, p: dict, score: float, metrics_json: str) -> int:
    """Shared INSERT for one brand_discovery candidate into weekly_categories
    — factored out of _run_weekly_generate so the Potential Product
    Opportunities tab's targeted 'Find partner now' search (see
    brand_discovery.search_for_missing_item) can insert into the SAME batch/
    table the weekly generate uses, with no parallel insert path to drift
    out of sync. Returns the new row's id."""
    with db.conn() as c:
        cur = c.execute(
            "INSERT INTO weekly_categories (batch_id, department, concept, concept_volume, "
            "source, worth_pursuing_reason, priority_score, opportunity_type, action_text, "
            "source_ref, business_url, products_to_bring_in, already_partner_flag, "
            "found_via_web_search, discovery_source, brand_images, social_links, brand_phones, brand_emails, brand_whatsapp, ecommerce_platform, review_status, metrics_json, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'pending_review',?,?)",
            (batch_id, p["department"], p["concept"], p["concept_volume"], p.get("source"),
             p.get("worth_pursuing_reason"), score, p.get("opportunity_type"), p["action"], p.get("source_ref"),
             p.get("business_url"), p.get("products_to_bring_in"), p.get("already_partner_flag"),
             int(bool(p.get("found_via_web_search"))),
             p.get("discovery_source") if p.get("source") == "brand_discovery" else None,
             json.dumps(p.get("brand_images") or [], ensure_ascii=False) if p.get("source") == "brand_discovery" else None,
             json.dumps(p.get("social_links") or [], ensure_ascii=False) if p.get("source") == "brand_discovery" else None,
             json.dumps(p.get("phones") or [], ensure_ascii=False) if p.get("source") == "brand_discovery" else None,
             json.dumps(p.get("emails") or [], ensure_ascii=False) if p.get("source") == "brand_discovery" else None,
             p.get("whatsapp") if p.get("source") == "brand_discovery" else None,
             p.get("ecommerce_platform") if p.get("source") == "brand_discovery" else None,
             metrics_json, db.now())
        )
        return cur.lastrowid


def _run_weekly_generate(job_id: str, week: str, num_cats: int, per_cat: int,
                           web_search_share: float = 1.0,
                           existing_batch_id: int | None = None) -> None:
    """Sources this week's businesses via brand_discovery.source_brand_discovery
    (all start 'pending_review'). Does NOT contact anyone here — see
    api_category_approve, which only now sends the opening message, after a
    human clicks Approve.

    2026-10-03 REDESIGN (user: "daraz kapruka verdict should be the source to
    find new brands to expand or add that category" — a bare Daraz/GSC
    category gap must never be its own weekly card): this used to pick
    categories from sourcing.py/worth_pursuing.py as ITS OWN pool and merge
    them with brand_pool, so a week with too few brand candidates fell back
    to showing raw category gaps with their own Approve/Reject button —
    exactly the pattern the user is rejecting. That whole category pool,
    the live-product-count cutoff, the worth_pursuing gate, and the
    same-business-line dedupe pass for it are all removed — see the
    REDESIGN comment just below for what replaced it.

    2026-10-03 (user: "i dont want u to loose currently whats found keep
    that in the list until it gets rejected"): `existing_batch_id`, when
    given (by api_weekly_generate's top-up path), makes this a TOP-UP run —
    `num_cats` here is already just the shortfall to find, no new
    weekly_batches row is created, and every new row is inserted into the
    CALLER'S existing batch alongside whatever's already there. Nothing
    about an existing row (status, content) is ever touched; prior_keys
    below already covers every business ever stored for ANY batch
    (regardless of status), so a previously-rejected business from this
    same batch can't silently reappear in the top-up."""
    try:
        with db.conn() as c:
            prior_keys = {r["concept"] for r in c.execute("SELECT DISTINCT concept FROM weekly_categories").fetchall()}

        # 2026-10-03 REDESIGN (user: "daraz kapruka verdict should be the
        # source to find new brands to expand or add that category" — a bare
        # Daraz/GSC category gap is NEVER its own weekly card any more; it
        # only exists as search fuel fed into brand_discovery's gap-driven
        # stream (_daraz_gap_hints_for_department / cluster_daraz_gaps). The
        # old behavior inserted leftover category rows (sourcing.py +
        # worth_pursuing.py) as standalone "Approve -> source partners" cards
        # whenever brand discovery didn't fill every slot on its own — that
        # reintroduced exactly the raw-category-as-actionable-unit pattern
        # the user is rejecting here, so that whole pool/merge/dedupe path is
        # removed. brand_discovery.source_brand_discovery now retries
        # internally (progressively widened search, up to 3 rounds) before
        # accepting fewer than num_cats businesses for the week, per user
        # decision: "keep searching with that gap as a hint, don't fall back
        # to a card."
        brand_discovery_error = None
        try:
            settings_for_brands = db.get_settings()
            brand_pool = brand_discovery.source_brand_discovery(
                limit=num_cats, model=settings_for_brands.get("claude_model", "claude-sonnet-5"),
                exclude_business_names=prior_keys, web_search_share=web_search_share)
        except Exception as e:
            # fail-open: a brand-discovery failure never blocks the rest of
            # the weekly generate. Gap-audit fix: the error is still
            # recorded on the job result (not just silently swallowed) so
            # "brand_discovery genuinely errored" is distinguishable from
            # "ran fine, found zero candidates this week" — otherwise both
            # looked identical (brand_pool=[]) with no visible marker
            # anywhere, unlike worth_pursuing.py/specialization.py's
            # per-row "(check failed — kept unfiltered)" reason strings.
            brand_pool = []
            brand_discovery_error = str(e)

        if existing_batch_id is not None:
            batch_id = existing_batch_id
        else:
            with db.conn() as c:
                cur = c.execute(
                    "INSERT INTO weekly_batches (week_start, created_at, num_categories, partners_per_category) "
                    "VALUES (?, ?, ?, ?)", (week, db.now(), num_cats, per_cat)
                )
                batch_id = cur.lastrowid

        # Brand-discovery rows skip the Daraz/GSC live-check metrics step
        # entirely — there's no Daraz category to check a live product count
        # against (the opportunity IS the business), and judge_brand_fit
        # already did the equivalent real-evidence judgment. metrics_json
        # still gets a minimal shape so the UI's shared metrics table
        # renders without KeyErrors on a brand card.
        #
        # 2026-10-03: this used to be ONE of two pools merged together
        # (category rows from sourcing.py/worth_pursuing.py filled any slots
        # brand_pool didn't). That's removed — see the REDESIGN note above
        # source_brand_discovery's call — so brand_pool is now the entire
        # week's candidate set; no category-label row is ever scored,
        # deduped or inserted as its own card again. skipped_well_stocked
        # stays 0 (nothing to report; kept in the job result shape for the
        # UI/poll response's sake).
        skipped_well_stocked = 0
        scored: list[tuple[float, dict, str]] = []  # (score, pick, metrics_json)
        for p in brand_pool:
            # 2026-10-08, user: "for each business identified can i get the
            # demand calculation for the product category they sell with
            # data" — see brand_discovery.attach_demand_evidence. Carried in
            # metrics_json (free-form per-row blob, no schema migration
            # needed) rather than a new column, same pattern final_verdict
            # already uses.
            metrics_json = json.dumps({"final_verdict": None, "brand_discovery": True,
                                        "demand_evidence": p.get("demand_evidence") or [],
                                        "search_keywords": p.get("search_keywords") or []})
            scored.append((_BRAND_DISCOVERY_SCORE, p, metrics_json))

        scored.sort(key=lambda t: -t[0])
        chosen = scored[:num_cats]

        inserted = 0
        new_ids: list[int] = []
        for score, p, metrics_json in chosen:
            new_ids.append(_insert_weekly_category_row(batch_id, p, score, metrics_json))
            inserted += 1
        # 2026-10-14, user: "the partner report should be filled before reaching out
        # / partner report is the decision point" — build every new card's report now.
        _queue_report_builds(new_ids)

        with _JOBS_LOCK:
            _JOBS[job_id] = {"status": "done", "error": None, "batch_id": batch_id,
                              "inserted": inserted, "skipped_well_stocked": skipped_well_stocked,
                              "brand_discovery_error": brand_discovery_error}
    except Exception as e:
        with _JOBS_LOCK:
            _JOBS[job_id] = {"status": "error", "error": str(e), "batch_id": None}


@app.route("/api/categories/<int:cat_id>/approve", methods=["POST"])
def api_category_approve(cat_id):
    """Human approves a candidate category — only now are partners actually
    sourced (real HTTP calls to Business Scout via /api/find_partners), so
    a noise row (cricket match, brand name) never burns a real lookup or
    ends up in front of a partner. Runs as a background job since sourcing
    can take up to ~2 minutes."""
    with db.conn() as c:
        cat = c.execute("SELECT * FROM weekly_categories WHERE id = ?", (cat_id,)).fetchone()
        if not cat:
            return jsonify({"ok": False, "error": "unknown category"}), 404
        # 2026-10-14, user: "partner report is the decision point" — a business is
        # never contacted before its report has been built and could be read.
        if cat["source"] == "brand_discovery" and not c.execute(
                "SELECT 1 FROM card_reports WHERE category_id = ? AND profile_json IS NOT NULL", (cat_id,)).fetchone():
            _queue_report_builds([cat_id])
            return jsonify({"ok": False, "error": "The partner report isn't ready yet. Open it, review it, "
                                                  "then approve from there (it's being built now)."}), 409
        if cat["review_status"] == "approved":
            # an approve whose sourcing job died (error / server restart) left
            # no partners behind — allow it to be retried instead of stuck forever
            has_partners = c.execute("SELECT 1 FROM partners WHERE weekly_category_id = ? LIMIT 1",
                                     (cat_id,)).fetchone()
            if has_partners:
                return jsonify({"ok": False, "error": "already approved"}), 400
        c.execute("UPDATE weekly_categories SET review_status = 'approved', reviewed_at = ? WHERE id = ?",
                   (db.now(), cat_id))
        batch = c.execute("SELECT partners_per_category FROM weekly_batches WHERE id = ?", (cat["batch_id"],)).fetchone()
    per_cat = batch["partners_per_category"] if batch else int(db.get_settings().get("partners_per_category", 4))

    job_id = uuid.uuid4().hex
    with _JOBS_LOCK:
        _JOBS[job_id] = {"status": "running", "error": None}
    threading.Thread(target=_run_category_approve, args=(job_id, cat_id, dict(cat), per_cat), daemon=True).start()
    return jsonify({"ok": True, "job_id": job_id})


def _run_category_approve(job_id: str, cat_id: int, cat: dict, per_cat: int) -> None:
    """Sources partners for an approved category, then — per user decision
    2026-09-30 ("agent sends the first message" = automatic on approve,
    not a manual per-partner click) — immediately sends PC Agent's opening
    message to each one via _do_partner_auto_reply. A per-partner send
    failure (e.g. one Claude CLI call times out) is recorded but does not
    abort the rest — every other sourced partner still gets contacted, and
    the job result reports exactly how many opened vs failed rather than
    silently leaving some partners stuck at 'new' with no indication why.

    A brand_discovery row (2026-10-01) is a BUSINESS, not an abstract
    category — the candidate IS the single partner to reach out to, found by
    visiting its own website, so this skips _find_partners (Business Scout)
    entirely and builds one partner row directly from the category row's own
    business_url/concept. Everything downstream (opening message, chat,
    status tracking) is identical to a Business-Scout-sourced partner."""
    concept = cat["concept"]
    try:
        if cat.get("source") == "brand_discovery":
            # 2026-10-06 fix: this used to hardcode phone=None even when
            # brand_discovery had already found real numbers for this
            # business (weekly_categories.brand_phones, a JSON array) — the
            # 24h follow-up escalation ("send the second number if no reply")
            # needs a real phone_2, so both numbers are carried over here
            # instead of being silently dropped on approve.
            try:
                brand_phones = json.loads(cat.get("brand_phones") or "[]")
            except (json.JSONDecodeError, TypeError):
                brand_phones = []
            # a proven WhatsApp number first, then mobiles, then landlines —
            # WhatsApp outreach to a landline (+94 11 …) can never be delivered
            wa_first = [cat["brand_whatsapp"]] if cat.get("brand_whatsapp") else []
            ordered = []
            for ph in wa_first + sorted(brand_phones, key=lambda x: 0 if _is_sl_mobile(x) else 1):
                if ph and _to_wa_number(ph) not in {_to_wa_number(o) for o in ordered}:
                    ordered.append(ph)
            brand_phones = ordered
            partners = [{
                "name": concept,
                "phone": brand_phones[0] if len(brand_phones) > 0 else None,
                "phone_2": brand_phones[1] if len(brand_phones) > 1 else None,
                "source": "brand_discovery",
                "address": None,
                "url": cat.get("business_url"),
                "wa_link": None,
                "is_landline": bool(brand_phones) and not _is_sl_mobile(brand_phones[0]),
                "product_verified": True,  # the fetched site itself is the evidence
                "is_specialist": True,     # this candidate IS the specialist business by construction
                "specialization_reason": cat.get("worth_pursuing_reason") or "",
            }]
        else:
            try:
                partners = _find_partners(concept)[:per_cat]
            except Exception as e:
                _reopen_category_review(cat_id)
                with _JOBS_LOCK:
                    _JOBS[job_id] = {"status": "error", "error": f"find_partners failed: {e}"}
                return
        partner_ids = []
        with db.conn() as c:
            for partner in partners:
                cur = c.execute(
                    "INSERT INTO partners (weekly_category_id, name, phone, phone_2, source, address, url, "
                    "wa_link, is_landline, product_verified, is_specialist, specialization_reason, "
                    "status, created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (cat_id, partner.get("name"), partner.get("phone"), partner.get("phone_2"), partner.get("source"),
                     partner.get("address"), partner.get("url"), partner.get("wa_link"),
                     int(bool(partner.get("is_landline"))), int(bool(partner.get("product_verified"))),
                     int(bool(partner.get("is_specialist"))), partner.get("specialization_reason"),
                     "new", db.now())
                )
                partner_ids.append(cur.lastrowid)

        opened = 0
        open_failures = []
        for pid in partner_ids:
            try:
                result, status = _do_partner_auto_reply(pid, opening=True)
            except Exception as e:
                result, status = {"ok": False, "error": str(e)}, 500
            if status == 200 and result.get("auto_replied") and not result.get("send_error"):
                opened += 1
            else:
                open_failures.append({"partner_id": pid,
                                      "error": result.get("send_error") or result.get("error") or result.get("reason")})

        # 2026-10-14: build each partner report straight away (store SKU count,
        # live Kapruka search of their product types, suggested questions) so
        # it's ready when someone opens it.
        _start_report_build(cat_id)

        with _JOBS_LOCK:
            _JOBS[job_id] = {"status": "done", "error": None, "partners_found": len(partners),
                              "opened": opened, "open_failures": open_failures, "live": _wa_live()}
    except Exception as e:
        with db.conn() as c:
            has_partners = c.execute("SELECT 1 FROM partners WHERE weekly_category_id = ? LIMIT 1",
                                     (cat_id,)).fetchone()
        if not has_partners:
            _reopen_category_review(cat_id)
        with _JOBS_LOCK:
            _JOBS[job_id] = {"status": "error", "error": str(e)}


def _reopen_category_review(cat_id: int) -> None:
    with db.conn() as c:
        c.execute("UPDATE weekly_categories SET review_status = 'pending_review', reviewed_at = NULL WHERE id = ?",
                  (cat_id,))


@app.route("/api/categories/<int:cat_id>/reject", methods=["POST"])
def api_category_reject(cat_id):
    # 2026-10-13, user: "when rejecting weekly outreach i want why it is
    # getting rjected... so agent should learn from that when doing weekly
    # outreach" — reason is stored on the row (shown on the card going
    # forward) AND fed into brand_discovery.judge_brand_fit's prompt on every
    # future run (see _recent_reject_reasons / _FIT_PROMPT_TEMPLATE), so a
    # real human pattern ("too small, one-product shop", "already Daraz-only
    # reseller, not an independent brand") actually changes what gets
    # surfaced next week, not just blocking this exact name from reappearing
    # (prior_keys/exclude_business_names already did that).
    reason = ((request.get_json(silent=True) or {}).get("reason") or "").strip()
    if not reason:
        return jsonify({"ok": False, "error": "a reject reason is required (it trains next week's sourcing)"}), 400
    with db.conn() as c:
        cat = c.execute("SELECT id FROM weekly_categories WHERE id = ?", (cat_id,)).fetchone()
        if not cat:
            return jsonify({"ok": False, "error": "unknown category"}), 404
        c.execute("UPDATE weekly_categories SET review_status = 'rejected', reviewed_at = ?, reject_reason = ? WHERE id = ?",
                   (db.now(), reason or None, cat_id))
    return jsonify({"ok": True})


@app.route("/api/partners/<int:partner_id>/remove", methods=["POST"])
def api_partner_remove(partner_id):
    """Manually removes a sourced partner from a category's outreach slate
    (soft-delete via status='removed', not a hard delete — its chat history,
    if any, is kept). Immediately tries to refill the freed slot with the
    next-best candidate from a fresh Find Partners call, excluding every
    name/phone already seen for that category (sourced, removed, or
    otherwise) so the same removed partner never just comes right back."""
    with db.conn() as c:
        partner = c.execute(
            """SELECT p.*, wc.concept, wc.source AS category_source FROM partners p
               JOIN weekly_categories wc ON wc.id = p.weekly_category_id WHERE p.id = ?""",
            (partner_id,)
        ).fetchone()
        if not partner:
            return jsonify({"ok": False, "error": "unknown partner"}), 404
        cat_id = partner["weekly_category_id"]
        concept = partner["concept"]
        c.execute("UPDATE partners SET status = 'removed' WHERE id = ?", (partner_id,))
        seen = c.execute(
            "SELECT name, phone FROM partners WHERE weekly_category_id = ?", (cat_id,)
        ).fetchall()

    if partner["category_source"] == "brand_discovery":
        # A brand_discovery category's single partner IS the business itself
        # — there's no Business Scout search that could find a "replacement"
        # for a specific brand's own website, so removing it just empties
        # the slot rather than calling _find_partners with a business name
        # (which would silently search for generic businesses under that
        # name and return something unrelated, not a real alternative).
        return jsonify({"ok": True, "removed": True, "refilled": False,
                         "refill_error": "brand_discovery candidates have no Find Partners alternative — "
                                          "add another URL to BRAND_SEEDS.txt for next week's run instead"})

    seen_names = {(r["name"] or "").strip().lower() for r in seen}
    seen_phones = {re.sub(r"\D", "", r["phone"] or "") for r in seen if r["phone"]}

    try:
        candidates = _find_partners(concept)
    except Exception as e:
        return jsonify({"ok": True, "removed": True, "refilled": False,
                         "refill_error": f"find_partners failed: {e}"})

    replacement = None
    for cand in candidates:
        name_key = (cand.get("name") or "").strip().lower()
        phone_key = re.sub(r"\D", "", cand.get("phone") or "")
        if name_key in seen_names:
            continue
        if phone_key and phone_key in seen_phones:
            continue
        replacement = cand
        break

    if not replacement:
        return jsonify({"ok": True, "removed": True, "refilled": False,
                         "refill_error": "no new candidate available (all Find Partners results already seen for this category)"})

    with db.conn() as c:
        c.execute(
            "INSERT INTO partners (weekly_category_id, name, phone, source, address, url, "
            "wa_link, is_landline, product_verified, is_specialist, specialization_reason, "
            "status, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (cat_id, replacement.get("name"), replacement.get("phone"), replacement.get("source"),
             replacement.get("address"), replacement.get("url"), replacement.get("wa_link"),
             int(bool(replacement.get("is_landline"))), int(bool(replacement.get("product_verified"))),
             int(bool(replacement.get("is_specialist"))), replacement.get("specialization_reason"),
             "new", db.now())
        )
    return jsonify({"ok": True, "removed": True, "refilled": True, "new_partner": replacement})


@app.route("/api/categories/approve/poll/<job_id>")
def api_category_approve_poll(job_id):
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
    if not job:
        return jsonify({"ok": False, "error": "unknown job_id"}), 404
    return jsonify({"ok": True, **job})


@app.route("/api/weekly/generate/poll/<job_id>")
def api_weekly_generate_poll(job_id):
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
    if not job:
        return jsonify({"ok": False, "error": "unknown job_id"}), 404
    return jsonify({"ok": True, **job})


def _run_demand_evidence_backfill(job_id: str) -> None:
    """One-time backfill (2026-10-08, user: "prefill the existing ones") for
    every weekly_categories row that predates attach_demand_evidence — see
    brand_discovery.backfill_demand_evidence_for_rows. Runs as a background
    job since it's a batched Claude CLI call + a real Google Ads lookup over
    however many rows are missing evidence, which can take a couple of
    minutes for a full backlog."""
    try:
        with db.conn() as c:
            rows = [dict(r) for r in c.execute(
                "SELECT id, concept, department, products_to_bring_in, metrics_json "
                "FROM weekly_categories WHERE source = 'brand_discovery'"
            ).fetchall()]
        to_fill = []
        for r in rows:
            try:
                m = json.loads(r["metrics_json"] or "{}")
            except Exception:
                m = {}
            if not m.get("demand_evidence"):
                to_fill.append(r)

        if not to_fill:
            with _JOBS_LOCK:
                _JOBS[job_id] = {"status": "done", "error": None, "updated": 0, "checked": 0}
            return

        settings = db.get_settings()
        model = settings.get("claude_model", "claude-sonnet-5")
        evidence_by_id = brand_discovery.backfill_demand_evidence_for_rows(to_fill, model=model)

        updated = 0
        with db.conn() as c:
            for r in to_fill:
                evidence = evidence_by_id.get(str(r["id"]))
                if evidence is None:
                    continue
                try:
                    m = json.loads(r["metrics_json"] or "{}")
                except Exception:
                    m = {}
                m["demand_evidence"] = evidence
                c.execute("UPDATE weekly_categories SET metrics_json = ? WHERE id = ?",
                          (json.dumps(m), r["id"]))
                updated += 1

        with _JOBS_LOCK:
            _JOBS[job_id] = {"status": "done", "error": None, "updated": updated, "checked": len(to_fill)}
    except Exception as e:
        with _JOBS_LOCK:
            _JOBS[job_id] = {"status": "error", "error": str(e)}


@app.route("/api/weekly/backfill_demand_evidence", methods=["POST"])
def api_weekly_backfill_demand_evidence():
    job_id = uuid.uuid4().hex
    with _JOBS_LOCK:
        _JOBS[job_id] = {"status": "running", "error": None}
    threading.Thread(target=_run_demand_evidence_backfill, args=(job_id,), daemon=True).start()
    return jsonify({"ok": True, "job_id": job_id})


@app.route("/api/weekly/backfill_demand_evidence/poll/<job_id>")
def api_weekly_backfill_demand_evidence_poll(job_id):
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
    if not job:
        return jsonify({"ok": False, "error": "unknown job_id"}), 404
    return jsonify({"ok": True, **job})


def _run_image_backfill(job_id: str) -> None:
    """One-time re-fetch of every existing business's images (2026-10-14,
    user screenshot of a duplicated/cropped image, confirmed via
    AskUserQuestion to backfill all existing rows, not just fix new ones
    going forward) — see brand_discovery.backfill_brand_images_for_rows for
    why a re-fetch is needed instead of deduping the already-stored list."""
    try:
        with db.conn() as c:
            rows = [dict(r) for r in c.execute(
                "SELECT id, business_url FROM weekly_categories "
                "WHERE source = 'brand_discovery' AND business_url IS NOT NULL"
            ).fetchall()]
        if not rows:
            with _JOBS_LOCK:
                _JOBS[job_id] = {"status": "done", "error": None, "updated": 0, "checked": 0}
            return

        images_by_id = brand_discovery.backfill_brand_images_for_rows(rows)

        updated = 0
        with db.conn() as c:
            for row_id, images in images_by_id.items():
                c.execute("UPDATE weekly_categories SET brand_images = ? WHERE id = ?",
                          (json.dumps(images, ensure_ascii=False), row_id))
                updated += 1

        with _JOBS_LOCK:
            _JOBS[job_id] = {"status": "done", "error": None, "updated": updated, "checked": len(rows)}
    except Exception as e:
        with _JOBS_LOCK:
            _JOBS[job_id] = {"status": "error", "error": str(e)}


@app.route("/api/weekly/backfill_images", methods=["POST"])
def api_weekly_backfill_images():
    job_id = uuid.uuid4().hex
    with _JOBS_LOCK:
        _JOBS[job_id] = {"status": "running", "error": None}
    threading.Thread(target=_run_image_backfill, args=(job_id,), daemon=True).start()
    return jsonify({"ok": True, "job_id": job_id})


@app.route("/api/weekly/backfill_images/poll/<job_id>")
def api_weekly_backfill_images_poll(job_id):
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
    if not job:
        return jsonify({"ok": False, "error": "unknown job_id"}), 404
    return jsonify({"ok": True, **job})


# ------------------------------------------------- Potential Product Opportunities
# Real, human-confirmed missing/badly-covered items — originally a one-time
# pull from the user's "Kapruka - New Opportunity" Google Sheet, now a real
# editable table (db.py's missing_items — see that schema's header). Feeds
# the weekly brand search automatically (brand_discovery.
# _missing_items_hints_for_department / _missing_items_open_hints) AND gets
# its own tab here: add/remove rows directly, see which real businesses were
# found for each (weekly_categories rows tagged discovery_source=
# 'missing_item:<id>', with their own review_status), and trigger an
# immediate out-of-cadence search per item.

@app.route("/api/missing_items")
def api_missing_items():
    with db.conn() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT * FROM missing_items ORDER BY created_at DESC").fetchall()]
        found_rows = c.execute(
            "SELECT id, discovery_source, concept, department, business_url, review_status, created_at, metrics_json "
            "FROM weekly_categories WHERE discovery_source LIKE 'missing_item:%'"
        ).fetchall()
    found_by_id: dict[str, list[dict]] = {}
    for r in found_rows:
        item_id = r["discovery_source"].split(":", 1)[1]
        # 2026-10-08, user screenshot: a "Hijabs" opportunity showed its own
        # "Ads: 880/mo" badge at the top but the 4 businesses found for it
        # (HijAbaya, Aayla Hijabs...) showed no demand breakdown of their
        # own — root cause: this query never selected metrics_json, so each
        # website's real per-business demand_evidence (computed by
        # search_for_missing_item -> attach_demand_evidence when it was
        # found) was sitting in the DB but never reaching this payload.
        try:
            demand_evidence = json.loads(r["metrics_json"] or "{}").get("demand_evidence") or []
        except Exception:
            demand_evidence = []
        found_by_id.setdefault(item_id, []).append({
            "weekly_category_id": r["id"], "concept": r["concept"], "department": r["department"],
            "business_url": r["business_url"], "review_status": r["review_status"], "created_at": r["created_at"],
            "demand_evidence": demand_evidence,
        })
    out = []
    for r in rows:
        websites = found_by_id.get(r["id"], [])
        out.append({**r, "mapped_department": brand_discovery._map_sheet_department(r.get("department") or ""),
                    "found_count": len(websites), "websites": websites})
    return jsonify({"ok": True, "items": out})


@app.route("/api/missing_items/add", methods=["POST"])
def api_missing_items_add():
    body = request.get_json(silent=True) or {}
    missing_item = (body.get("missing_item") or "").strip()
    if not missing_item:
        return jsonify({"ok": False, "error": "missing_item is required"}), 400
    item_id = uuid.uuid4().hex[:12]
    with db.conn() as c:
        c.execute(
            "INSERT INTO missing_items (id, missing_item, example, search_term, demand, department, "
            "summary, url, status, source, created_at) VALUES (?,?,?,?,?,?,?,?,'Pending','manual',?)",
            (item_id, missing_item, (body.get("example") or "").strip() or None,
             (body.get("search_term") or "").strip() or missing_item,
             (body.get("demand") or "medium").strip().lower(), (body.get("department") or "").strip() or None,
             (body.get("summary") or "").strip() or None, (body.get("url") or "").strip() or None, db.now())
        )
    return jsonify({"ok": True, "id": item_id})


@app.route("/api/missing_items/<item_id>", methods=["DELETE"])
def api_missing_items_delete(item_id):
    with db.conn() as c:
        c.execute("DELETE FROM missing_items WHERE id = ?", (item_id,))
    return jsonify({"ok": True})


def _run_missing_item_ads_refresh(job_id: str, item_ids: list[str]) -> None:
    """Real Google Ads Keyword Planner lookup for each item's search_term
    (user: "try to get google ads demand" — the sheet's demand column was
    just a human's low/medium/high judgment call, never a real number).
    Batches ALL search terms into one generate_keyword_ideas call (that API
    accepts multiple seeds per request and is comparatively slow per call —
    confirmed by google_ads_keyword_ideas.py's own DEFAULT_TIMEOUT=300s note
    on repeated-call degradation), then writes each item's own volume back,
    matched by keyword text (case-insensitive) since the API doesn't echo
    request order back reliably when seeds overlap/expand."""
    try:
        with db.conn() as c:
            rows = c.execute(
                "SELECT id, search_term FROM missing_items WHERE id IN ({})".format(
                    ",".join("?" * len(item_ids))), item_ids
            ).fetchall()
        terms = [r["search_term"] for r in rows if r["search_term"]]
        if not terms:
            with _JOBS_LOCK:
                _JOBS[job_id] = {"status": "done", "error": None, "updated": 0}
            return

        import google_ads_keyword_ideas as gaki
        ideas = gaki.generate_keyword_ideas(terms, geo_target="Sri Lanka")
        volume_by_term = {idea["keyword"].strip().lower(): idea["avg_monthly_searches"] for idea in ideas}

        updated = 0
        with db.conn() as c:
            for r in rows:
                term = (r["search_term"] or "").strip().lower()
                vol = volume_by_term.get(term)
                if vol is None:
                    continue
                c.execute("UPDATE missing_items SET ads_volume = ?, ads_checked_at = ? WHERE id = ?",
                          (vol, db.now(), r["id"]))
                updated += 1

        with _JOBS_LOCK:
            _JOBS[job_id] = {"status": "done", "error": None, "updated": updated, "checked": len(terms)}
    except Exception as e:
        with _JOBS_LOCK:
            _JOBS[job_id] = {"status": "error", "error": str(e)}


@app.route("/api/missing_items/refresh_ads", methods=["POST"])
def api_missing_items_refresh_ads():
    """Real Google Ads demand for every (or one, via body.item_ids) missing
    item — see _run_missing_item_ads_refresh. Runs as a background job since
    the Ads API call itself can take tens of seconds."""
    body = request.get_json(silent=True) or {}
    item_ids = body.get("item_ids")
    if not item_ids:
        with db.conn() as c:
            item_ids = [r["id"] for r in c.execute("SELECT id FROM missing_items").fetchall()]
    if not item_ids:
        return jsonify({"ok": False, "error": "no items to check"}), 400
    job_id = uuid.uuid4().hex
    with _JOBS_LOCK:
        _JOBS[job_id] = {"status": "running", "error": None}
    threading.Thread(target=_run_missing_item_ads_refresh, args=(job_id, item_ids), daemon=True).start()
    return jsonify({"ok": True, "job_id": job_id})


@app.route("/api/missing_items/refresh_ads/poll/<job_id>")
def api_missing_items_refresh_ads_poll(job_id):
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
    if not job:
        return jsonify({"ok": False, "error": "unknown job_id"}), 404
    return jsonify({"ok": True, **job})


def _run_missing_item_find_partner(job_id: str, row: dict) -> None:
    try:
        settings = db.get_settings()
        model = settings.get("claude_model", "claude-sonnet-5")
        found = brand_discovery.search_for_missing_item(row, model=model, n=4)

        with db.conn() as c:
            batch = c.execute("SELECT id FROM weekly_batches ORDER BY id DESC LIMIT 1").fetchone()
            prior_keys = {r["concept"] for r in c.execute("SELECT DISTINCT concept FROM weekly_categories").fetchall()}
        if not batch:
            week = _week_start()
            with db.conn() as c:
                cur = c.execute(
                    "INSERT INTO weekly_batches (week_start, created_at, num_categories, partners_per_category) "
                    "VALUES (?, ?, ?, ?)", (week, db.now(), 0, int(settings.get("partners_per_category", 4)))
                )
                batch_id = cur.lastrowid
        else:
            batch_id = batch["id"]

        inserted = 0
        for p in found:
            if (p.get("concept") or "").strip().lower() in {k.strip().lower() for k in prior_keys}:
                continue  # already a weekly_categories row (any status) — same dedupe as the weekly generate
            metrics_json = json.dumps({"final_verdict": None, "brand_discovery": True,
                                        "missing_item_id": row.get("id"),
                                        "demand_evidence": p.get("demand_evidence") or [],
                                        "search_keywords": p.get("search_keywords") or []})
            _queue_report_builds([_insert_weekly_category_row(batch_id, p, _BRAND_DISCOVERY_SCORE, metrics_json)])
            inserted += 1

        with _JOBS_LOCK:
            _JOBS[job_id] = {"status": "done", "error": None, "batch_id": batch_id,
                              "inserted": inserted, "found": len(found)}
    except Exception as e:
        with _JOBS_LOCK:
            _JOBS[job_id] = {"status": "error", "error": str(e)}


@app.route("/api/missing_items/<item_id>/find_partner", methods=["POST"])
def api_missing_items_find_partner(item_id):
    with db.conn() as c:
        row = c.execute("SELECT * FROM missing_items WHERE id = ?", (item_id,)).fetchone()
    if not row:
        return jsonify({"ok": False, "error": "unknown item_id"}), 404
    row = dict(row)
    job_id = uuid.uuid4().hex
    with _JOBS_LOCK:
        _JOBS[job_id] = {"status": "running", "error": None}
    threading.Thread(target=_run_missing_item_find_partner, args=(job_id, row), daemon=True).start()
    return jsonify({"ok": True, "job_id": job_id})


@app.route("/api/missing_items/find_partner/poll/<job_id>")
def api_missing_items_find_partner_poll(job_id):
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
    if not job:
        return jsonify({"ok": False, "error": "unknown job_id"}), 404
    return jsonify({"ok": True, **job})


# ------------------------------------------------------------- Chat w/ Kogul

@app.route("/api/kogul_chat/history")
def api_kogul_chat_history():
    limit = request.args.get("limit", 100, type=int)
    with db.conn() as c:
        rows = c.execute(
            "SELECT * FROM kogul_chat ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    items = [dict(r) for r in rows][::-1]
    return jsonify({"ok": True, "items": items})


@app.route("/api/kogul_chat/clear", methods=["POST"])
def api_kogul_chat_clear():
    """2026-10-04, user: "clear button for chat with kogule" — wipes the
    whole Q&A history (this chat has no per-scenario concept the way
    simulations do, just one running transcript)."""
    with db.conn() as c:
        c.execute("DELETE FROM kogul_chat")
    return jsonify({"ok": True})


@app.route("/api/kogul_chat/ask", methods=["POST"])
def api_kogul_chat_ask():
    # 2026-10-06, user: "make sure the kogul has the ability to send images,
    # files, read images if recieved, also in chat stimulation and partner
    # chats". multipart/form-data when files are attached (so werkzeug
    # parses request.files), plain JSON otherwise — same route handles both,
    # matching how a browser <form> with a file input actually submits.
    if request.content_type and "multipart/form-data" in request.content_type:
        question = (request.form.get("question") or "").strip()
        files = request.files.getlist("files")
    else:
        body = request.get_json(silent=True) or {}
        question = (body.get("question") or "").strip()
        files = []
    if not question and not files:
        return jsonify({"ok": False, "error": "missing question"}), 400
    try:
        atts = attachments.save_uploads(files) if files else []
    except attachments.AttachmentError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    job_id = uuid.uuid4().hex
    with _JOBS_LOCK:
        _JOBS[job_id] = {"status": "running", "answer": None, "error": None}
    threading.Thread(target=_run_kogul_chat_job, args=(job_id, question, atts), daemon=True).start()
    return jsonify({"ok": True, "job_id": job_id})


def _build_weekly_chat_context() -> str | None:
    """Compact text summary of the CURRENT week's batch for the chat prompt
    — 2026-10-03, user: "have stimulate chat with kogul option to see how
    kogul chats". A live test question ("what did you find this week")
    exposed that the chat had zero real data to answer from (see
    build_kogul_chat_prompt's docstring); this reads the exact same rows
    /api/weekly/current serves (same query, same week-resolution logic) so
    chat answers are grounded in the real batch, never guessed. Returns None
    when no batch exists yet, so the prompt can say so plainly instead of
    describing an empty list as if it meant something.

    Deliberately compact (business name, department, status, one-line
    reason, contact-found yes/no) rather than dumping the full metrics/image
    JSON every row carries — the chat prompt doesn't need brand image URLs
    or raw metrics_json to answer "what did you find," and keeping this
    small matters since it's added to EVERY chat turn's token cost, not
    computed once."""
    week = _week_start()
    with db.conn() as c:
        batch = c.execute(
            "SELECT * FROM weekly_batches WHERE week_start = ? ORDER BY id DESC LIMIT 1", (week,)
        ).fetchone()
        if not batch:
            return None
        cats = c.execute(
            "SELECT * FROM weekly_categories WHERE batch_id = ? ORDER BY id", (batch["id"],)
        ).fetchall()
    if not cats:
        return f"Week of {week}: batch exists (id={batch['id']}) but has 0 rows."

    lines = [f"Week of {week}, batch id={batch['id']}, {len(cats)} total rows:"]
    for cat in cats:
        is_brand = cat["source"] == "brand_discovery"
        has_contact = bool(cat["brand_phones"] and cat["brand_phones"] != "[]") or \
                      bool(cat["brand_emails"] and cat["brand_emails"] != "[]")
        bits = [
            f"- {cat['concept']}" + (f" ({cat['department']})" if is_brand else f" [category: {cat['department']}]"),
            f"status={cat['review_status']}",
        ]
        if is_brand:
            bits.append("contact found" if has_contact else "NO contact found yet")
            if cat["business_url"]:
                bits.append(cat["business_url"])
        if cat["worth_pursuing_reason"]:
            bits.append(f"reason: {cat['worth_pursuing_reason'][:140]}")
        lines.append(" | ".join(bits))
    return "\n".join(lines)


def _run_kogul_chat_job(job_id: str, question: str, atts: list[dict] | None = None) -> None:
    try:
        with db.conn() as c:
            hist_rows = c.execute("SELECT * FROM kogul_chat ORDER BY id DESC LIMIT 10").fetchall()
        history = []
        pending_q = None
        for r in reversed(hist_rows):
            if r["role"] == "user":
                pending_q = r["body"]
            elif r["role"] == "kogul" and pending_q is not None:
                history.append({"question": pending_q, "answer": r["body"]})
                pending_q = None
        weekly_context = _build_weekly_chat_context()
        # run_claude_cli is tool-free text-only (no vision input) — any
        # image attached here goes through gpt-4o vision FIRST to become a
        # text description, then that text is handed to Claude like any
        # other prompt content. See attachments.describe_for_text_model.
        attachments_block = attachments.describe_for_text_model(atts or [], caller="kogul.kogul_chat_attachment")
        shareables_block = attachments.shareables_prompt_block()
        prompt = brain.build_kogul_chat_prompt(question, history, weekly_context=weekly_context,
                                                attachments_block=attachments_block, shareables_block=shareables_block)
        # 2026-10-04, user: "let kogul use open ai key to respond" — moved
        # Chat with PC Agent onto OpenAI. 2026-10-06, same day, user: "no no
        # chat with pc agent will be claude cli based... have model
        # selection drop down for claude model" — moved back to the Claude
        # CLI. "chat_model" setting now holds a CLAUDE model id (dropdown on
        # the Chat with PC Agent tab), not an OpenAI one; Simulate as
        # Partner and real outreach replies are unaffected, still OpenAI.
        chat_model = db.get_settings().get("chat_model", "claude-sonnet-5")
        raw_answer = run_claude_cli(prompt, model=chat_model, timeout=90)
        answer, out_atts = attachments.extract_attachments(raw_answer)
        with db.conn() as c:
            c.execute("INSERT INTO kogul_chat (role, body, created_at, attachments) VALUES ('user', ?, ?, ?)",
                      (question, db.now(), attachments.dumps(atts)))
            c.execute("INSERT INTO kogul_chat (role, body, created_at, attachments) VALUES ('kogul', ?, ?, ?)",
                      (answer, db.now(), attachments.dumps(out_atts)))
            c.execute(
                "INSERT INTO token_usage (context, model, input_chars, output_chars, created_at) VALUES (?,?,?,?,?)",
                ("kogul_chat", chat_model, len(prompt), len(answer), db.now())
            )
        with _JOBS_LOCK:
            _JOBS[job_id] = {"status": "done", "answer": answer, "error": None}
    except Exception as e:
        with _JOBS_LOCK:
            _JOBS[job_id] = {"status": "error", "answer": None, "error": str(e)}


@app.route("/api/kogul_chat/poll/<job_id>")
def api_kogul_chat_poll(job_id):
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
    if not job:
        return jsonify({"ok": False, "error": "unknown job_id"}), 404
    return jsonify({"ok": True, **job})


# ------------------------------------------------------------- Partner chats

@app.route("/api/partners")
def api_partners():
    status = request.args.get("status", "").strip()
    # 2026-10-13, user: "a pipeline interface" — the pipeline view's stage
    # cards link here with status=in_progress/reached, neither of which is
    # a real partners.status value (same "virtual status" pattern
    # needs_handover already used below). date_from/date_to filter on
    # p.created_at (user confirmed: "first contacted", not last activity —
    # a partner stays in the cohort it was first reached in, matching how
    # weekly batches already work) rather than last_message_at.
    date_from = request.args.get("date_from", "").strip()
    date_to = request.args.get("date_to", "").strip()
    with db.conn() as c:
        q = """SELECT p.*, wc.concept, wc.department FROM partners p
               JOIN weekly_categories wc ON wc.id = p.weekly_category_id
               WHERE p.status != 'removed'"""
        params = []
        if status == "needs_handover":
            # needs_handover is a FLAG, not a status value (a handed-over
            # chat usually still sits at status='contacted'), so this can't
            # reuse the plain "p.status = ?" branch below.
            q += " AND p.needs_handover = 1"
        elif status == "in_progress":
            q += " AND p.status IN ('contacted', 'replied', 'negotiating')"
        elif status == "uncontacted":
            # 2026-10-13, user: "make sure partner chats have only these
            # fitlers and every chat falls into these filteres" — status=new
            # is now its own real stage (was previously unfiltered/
            # unreachable except via "All" or the removed "New" option),
            # same explicit-value shape "reached" used to have.
            q += " AND p.status = 'new'"
        elif status:
            q += " AND p.status = ?"
            params.append(status)
        if date_from:
            q += " AND p.created_at >= ?"
            params.append(date_from)
        if date_to:
            q += " AND p.created_at <= ?"
            params.append(date_to + "T23:59:59")
        q += " ORDER BY p.last_message_at DESC, p.id DESC"
        rows = c.execute(q, params).fetchall()
    return jsonify({"ok": True, "partners": [dict(r) for r in rows]})


@app.route("/api/rejections")
def api_rejections():
    """2026-10-13, user: "have a rejection tab, 2 sections - pc team rejected
    before reaching out - why / partner rejecting te proposition - why" —
    these are two genuinely different kinds of "no" that were previously
    scattered (reject_reason only surfaced inline on Weekly Outreach,
    not_agreed_reason only as an aggregated count on the Dashboard): a
    business the PC team itself screened out before any outreach
    (weekly_categories.reject_reason, see api_category_reject) vs. a real
    partner who was contacted and declined the actual proposition
    (partners.not_agreed_reason, see api_partner_set_status /
    partner_reply's NOT_AGREED handling). Returns individual rows, not just
    counts, so each can actually be read."""
    with db.conn() as c:
        pc_rejected = c.execute(
            "SELECT id, concept AS business_name, department, reject_reason, reviewed_at "
            "FROM weekly_categories WHERE review_status = 'rejected' "
            "ORDER BY reviewed_at DESC"
        ).fetchall()
        # 2026-10-13: "pending — asked why" is a placeholder brain.py writes
        # the instant a bare decline arrives so the status badge flips
        # immediately (see partner_reply's NOT_AGREED handling) — excluded
        # here same as the Dashboard's top_not_agreed_reasons, since showing
        # it as a real rejection reason before the follow-up answer lands
        # would be misleading.
        partner_rejected = c.execute(
            """SELECT p.id, p.name AS business_name, wc.department, p.not_agreed_reason,
                      p.last_message_at FROM partners p
               JOIN weekly_categories wc ON wc.id = p.weekly_category_id
               WHERE p.status = 'not_agreed' AND p.not_agreed_reason IS NOT NULL
                 AND TRIM(p.not_agreed_reason) != '' AND LOWER(p.not_agreed_reason) != 'pending — asked why'
               ORDER BY p.last_message_at DESC"""
        ).fetchall()
    def _top_reasons(rows, key):
        counts: dict[str, int] = {}
        for r in rows:
            k = (r[key] or "").strip()
            if k:
                counts[k] = counts.get(k, 0) + 1
        return [{"reason": k, "count": v} for k, v in sorted(counts.items(), key=lambda kv: -kv[1])[:10]]

    return jsonify({"ok": True,
                     "pc_rejected": [dict(r) for r in pc_rejected],
                     "partner_rejected": [dict(r) for r in partner_rejected],
                     "top_pc_reject_reasons": _top_reasons(pc_rejected, "reject_reason"),
                     "top_partner_reject_reasons": _top_reasons(partner_rejected, "not_agreed_reason")})


@app.route("/api/partners/<int:partner_id>/messages")
def api_partner_messages(partner_id):
    with db.conn() as c:
        partner = c.execute(
            """SELECT p.*, wc.concept, wc.department FROM partners p
               JOIN weekly_categories wc ON wc.id = p.weekly_category_id WHERE p.id = ?""",
            (partner_id,)
        ).fetchone()
        if not partner:
            return jsonify({"ok": False, "error": "unknown partner"}), 404
        msgs = c.execute(
            "SELECT * FROM messages WHERE partner_id = ? ORDER BY id", (partner_id,)
        ).fetchall()
    return jsonify({"ok": True, "partner": dict(partner), "messages": [dict(m) for m in msgs]})


_ANALYSIS_PROMPT = """You are reviewing a real partner-onboarding conversation between PC Agent
(Kapruka's outreach agent) and a prospective partner business, "{partner_name}" ({department}).
Current status: {status}.

Conversation so far:
{convo}

Give a short analysis for a human reviewer, grounded ONLY in what's actually in the conversation
above — never invent a detail, number, or intention the partner didn't actually say. Output ONLY a
JSON object, exactly:
{{"summary": "<2-3 sentences on where this conversation actually stands>",
"interest_level": "high"|"medium"|"low"|"unclear",
"interest_reason": "<one short sentence citing what in the conversation shows this>",
"next_step": "<one short, concrete suggestion for what PC Agent or a human should do next>"}}
No other text, no markdown fences."""


@app.route("/api/partners/<int:partner_id>/analyze")
def api_partner_analyze(partner_id):
    """Real, on-demand LLM analysis of one partner's conversation (2026-10-13,
    user: "an ai analysis of the conversation on all partners... from there
    when i click it should take to the partner conversation and read" — a
    real per-partner call when a human opens that partner, not a
    precomputed field on every partner up front, since most conversations
    are never opened). Fail-open: a model/parse failure returns ok:false
    with a plain error, never a fabricated analysis."""
    with db.conn() as c:
        partner = c.execute(
            """SELECT p.*, wc.concept, wc.department FROM partners p
               JOIN weekly_categories wc ON wc.id = p.weekly_category_id WHERE p.id = ?""",
            (partner_id,)
        ).fetchone()
        if not partner:
            return jsonify({"ok": False, "error": "unknown partner"}), 404
        msgs = c.execute(
            "SELECT sender, body FROM messages WHERE partner_id = ? ORDER BY id", (partner_id,)
        ).fetchall()
    if not msgs:
        return jsonify({"ok": False, "error": "no conversation yet to analyze"}), 400

    who = {"kogul": "PC Agent", "partner": partner["name"], "human": "Kapruka (human)", "system": "SYSTEM"}
    convo = "\n".join(f"{who.get(m['sender'], m['sender'])}: {m['body']}" for m in msgs)
    prompt = _ANALYSIS_PROMPT.format(partner_name=partner["name"], department=partner["department"],
                                       status=partner["status"], convo=convo)
    try:
        raw = run_openai_chat_text(prompt, model="gpt-4o", timeout=60, caller=f"kogul.partner_analysis:{partner_id}")
        from claude_cli import extract_json
        data = extract_json(raw)
        if isinstance(data, list):
            data = data[0] if data else {}
    except Exception as e:
        return jsonify({"ok": False, "error": f"analysis failed: {e}"}), 502

    return jsonify({"ok": True, "analysis": {
        "summary": data.get("summary") or "",
        "interest_level": data.get("interest_level") or "unclear",
        "interest_reason": data.get("interest_reason") or "",
        "next_step": data.get("next_step") or "",
    }})


# ------------------------------------------------------------------ Partner report
# 2026-10-14 (see kogul/partner_report.py). Keyed to the BUSINESS CARD
# (weekly_categories row), not the chat partner — user: "i want the partner
# report before reaching out". Once approved, the card's partner row and chat
# feed into it. GET is cheap (DB only, live status) so the report polls it;
# the AI + live-search build runs as a background job, only when the chat
# changed (or on demand).

def _approved_partner_questions(partner_id: int) -> list[str]:
    """Approved questions for the business this partner row belongs to."""
    with db.conn() as c:
        return [r["question"] for r in c.execute(
            "SELECT q.question FROM card_questions q JOIN partners p ON p.weekly_category_id = q.category_id "
            "WHERE p.id = ? AND q.status = 'approved' ORDER BY q.id", (partner_id,)).fetchall()]


def _report_card(category_id: int) -> dict | None:
    """The business card plus (if approved) its partner row, flattened."""
    with db.conn() as c:
        wc = c.execute("SELECT * FROM weekly_categories WHERE id = ?", (category_id,)).fetchone()
        if not wc:
            return None
        p = c.execute("SELECT * FROM partners WHERE weekly_category_id = ? ORDER BY id LIMIT 1",
                      (category_id,)).fetchone()
    card = dict(wc)
    card["category_id"] = category_id
    card["name"] = card.get("concept")
    card["url"] = card.get("business_url")
    card["partner_id"] = None
    card["status"] = None
    if p:
        p = dict(p)
        card.update({"partner_id": p["id"], "status": p["status"], "phone": p.get("phone"),
                     "needs_handover": p.get("needs_handover"), "human_takeover": p.get("human_takeover"),
                     "not_agreed_reason": p.get("not_agreed_reason"), "last_message_at": p.get("last_message_at"),
                     "call_slot": p.get("call_slot"), "call_contact_name": p.get("call_contact_name"),
                     "url": p.get("url") or card["url"], "name": p.get("name") or card["name"]})
    return card


def _card_messages(card: dict, with_body: bool = False) -> list[dict]:
    if not card.get("partner_id"):
        return []
    cols = "sender, body, created_at" if with_body else "sender, created_at"
    with db.conn() as c:
        return [dict(m) for m in c.execute(f"SELECT {cols} FROM messages WHERE partner_id = ? ORDER BY id",
                                            (card["partner_id"],)).fetchall()]


def _loads(s, default):
    try:
        return json.loads(s) if s else default
    except (TypeError, ValueError):
        return default


def _brief_missing(profile: dict, skus) -> list[str]:
    """The four pre-collected brief items research must fill BEFORE outreach
    (2026-10-14, user chose "Research fills it, no chat"): what they sell,
    product count, location, Colombo-or-not. Whether an outside-Colombo
    business accepts consignment is their decision, so it's not a research
    item — the report shows it as the one question for the chat."""
    if not profile:
        return ["what they sell", "product count", "location", "Colombo or not"]
    return [name for name, ok in (("what they sell", bool(profile.get("product_types"))),
                                  ("product count", skus is not None),
                                  ("location", bool(profile.get("location"))),
                                  ("Colombo or not", profile.get("in_colombo") in ("yes", "no"))) if not ok]


def _report_pipeline(card: dict, msgs: list, profile: dict, skus=None) -> list[dict]:
    """Steps for the report's status bar, from live DB state. 2026-10-14, user:
    "Brief collected ... should be the first step and then approve and opening
    message sent": the brief is filled from research BEFORE outreach; the chat
    only fills what research couldn't find. The goal is a booked team call."""
    senders = {m["sender"] for m in msgs}
    missing = _brief_missing(profile, skus)
    brief = {"label": "Brief collected" if not missing else "Brief incomplete",
             "state": "done" if not missing else ("attention" if profile else "todo"),
             "detail": "all 4 from research" if not missing else f"missing: {', '.join(missing)}"}
    review = card.get("review_status")
    if review == "rejected":
        return [{"label": "Found", "state": "done"}, brief,
                {"label": f"Rejected: {card.get('reject_reason') or 'no reason'}", "state": "stopped"}]
    steps = [{"label": "Found", "state": "done"}, brief,
             {"label": "Approved", "state": "done" if review == "approved" else "active"}]
    if review != "approved":
        return steps + [{"label": "Opening sent", "state": "todo"}, {"label": "Partner replied", "state": "todo"},
                        {"label": "Call booked", "state": "todo"}, {"label": "Decision", "state": "todo"}]
    status = card.get("status")
    call = card.get("call_slot")
    call_step = {"label": "Call booked", "state": "done" if call else ("active" if "partner" in senders else "todo"),
                 "detail": call + (f" · {card['call_contact_name']}" if card.get("call_contact_name") else "") if call else None}
    if status == "agreed":
        outcome, state = "Agreed, with PC team", "done"
    elif status == "not_agreed":
        outcome, state = f"Not agreed: {card.get('not_agreed_reason') or 'no reason'}", "stopped"
    elif status == "unresponsive":
        outcome, state = "Unresponsive", "stopped"
    elif card.get("needs_handover"):
        outcome, state = "Needs a human", "attention"
    else:
        outcome, state = "Decision pending", "todo"
    return steps + [
        {"label": "Opening sent", "state": "done" if "kogul" in senders else "todo"},
        {"label": "Partner replied", "state": "done" if "partner" in senders else "todo"},
        call_step,
        {"label": outcome, "state": state},
    ]


def _known_facts_for_partner(partner_id: int) -> dict:
    """Facts the report already has from research (website / store / verified
    Google), so PC Agent doesn't re-ask them in the brief: {text, location,
    in_colombo, sku_count, products} — brain._skip_known drops the answered
    questions from the list, `text` is shown to the model."""
    with db.conn() as c:
        row = c.execute("SELECT r.* FROM card_reports r JOIN partners p ON p.weekly_category_id = r.category_id "
                        "WHERE p.id = ?", (partner_id,)).fetchone()
    if not row or not row["profile_json"]:
        return {}
    catalog, profile = _loads(row["catalog_json"], {}), _loads(row["profile_json"], {})
    profile = partner_report.merge_research_location(profile, _loads(row["research_json"], {}))
    facts = []
    if profile.get("product_types"):
        facts.append("- What they sell: " + "; ".join(t.get("kind") or t.get("search") for t in profile["product_types"]))
    if catalog.get("count") is not None:
        facts.append(f"- Product count: {catalog['count']} products on their online store (confirm this number, don't ask how many)")
    if profile.get("location") and profile.get("location_source") in ("website", "google"):
        facts.append(f"- Based in: {profile['location']} (from their {'website' if profile['location_source'] == 'website' else 'Google listing'})"
                     + (" — within Colombo District, so no need to ask about consignment" if profile.get("in_colombo") == "yes" else ""))
    researched_loc = profile.get("location") if profile.get("location_source") in ("website", "google") else None
    return {"text": "\n".join(facts), "location": researched_loc,
            "in_colombo": profile.get("in_colombo") if researched_loc else None,
            "sku_count": catalog.get("count"), "products": bool(profile.get("product_types"))}


_REPORT_BUILDS: dict[int, str] = {}  # category_id -> job id of a running build
_report_queue: list[int] = []         # category_ids waiting for a background build, one at a time
_report_queue_lock = threading.Lock()
_report_workers: list[threading.Thread] = []
_report_taken: set[int] = set()  # being built by a worker right now


def _queue_report_builds(category_ids: list[int]) -> None:
    """Builds reports ONE AT A TIME in the background (each makes live Kapruka
    searches, which must stay paced — memory: kapruka-cloudflare-rate-limit)."""
    with _report_queue_lock:
        for cid in category_ids:
            if cid and cid not in _report_queue:
                _report_queue.append(cid)
        _report_workers[:] = [w for w in _report_workers if w.is_alive()]
        while len(_report_workers) < 3:
            w = threading.Thread(target=_report_queue_loop, daemon=True)
            _report_workers.append(w)
            w.start()


def _report_queue_loop() -> None:
    while True:
        with _report_queue_lock:
            free = [x for x in _report_queue if x not in _report_taken]
            if not free:
                return
            cid = free[0]
            _report_taken.add(cid)
        try:
            with db.conn() as c:
                done = c.execute("SELECT 1 FROM card_reports WHERE category_id = ? AND profile_json IS NOT NULL "
                                 "AND research_json IS NOT NULL", (cid,)).fetchone()
            # already built (e.g. someone opened it meanwhile) -> skip; a running
            # build is simply waited on (_start_report_build returns its job)
            job = None if done else _start_report_build(cid)
            while job:
                with _JOBS_LOCK:
                    if _JOBS.get(job, {}).get("status") != "running":
                        break
                time.sleep(2)
        except Exception as e:
            print(f"[report queue] {cid}: {e}", file=sys.stderr)
        with _report_queue_lock:
            _report_taken.discard(cid)
            if cid in _report_queue:
                _report_queue.remove(cid)


def _report_summary(c, category_id: int) -> dict:
    """Small summary for the Weekly Outreach card (verdict + key facts)."""
    rep = c.execute("SELECT catalog_json, profile_json, kapruka_json, research_json FROM card_reports WHERE category_id = ?",
                    (category_id,)).fetchone()
    with _report_queue_lock:
        queued = category_id in _report_queue
    if not rep or not rep["profile_json"]:
        return {"built": False, "queued": queued}
    catalog, kapruka, research = _loads(rep["catalog_json"], {}), _loads(rep["kapruka_json"], []), _loads(rep["research_json"], {})
    profile = partner_report.merge_research_location(_loads(rep["profile_json"], {}), research)
    skus, src = partner_report.effective_skus(profile, catalog, research)
    v = partner_report.lucrative_verdict(profile, catalog, kapruka, research)
    return {"built": True, "queued": queued, "level": v["level"], "label": v["label"], "skus": skus,
            "sku_lower_bound": bool(src and "at least" in src), "location": profile.get("location"),
            "in_colombo": profile.get("in_colombo"),
            "gaps": sum(1 for k in kapruka if k.get("verdict") in ("none", "thin")), "ranges": len(kapruka),
            "brand_value": (research.get("brand_value") or {}).get("level")}


def _report_payload(category_id: int) -> dict | None:
    card = _report_card(category_id)
    if not card:
        return None
    msgs = [m for m in _card_messages(card) if m["sender"] != "system"]
    with db.conn() as c:
        rep = c.execute("SELECT * FROM card_reports WHERE category_id = ?", (category_id,)).fetchone()
        qs = [dict(q) for q in c.execute(
            "SELECT * FROM card_questions WHERE category_id = ? ORDER BY id", (category_id,)).fetchall()]
    rep = dict(rep) if rep else {}
    catalog = _loads(rep.get("catalog_json"), {})
    research = _loads(rep.get("research_json"), {})
    profile = partner_report.merge_research_location(_loads(rep.get("profile_json"), {}), research)
    kapruka = _loads(rep.get("kapruka_json"), [])
    skus, sku_src = partner_report.effective_skus(profile, catalog, research) if (profile or catalog) else (None, None)
    with _JOBS_LOCK:
        job = _REPORT_BUILDS.get(category_id)
        building = bool(job and _JOBS.get(job, {}).get("status") == "running")
    return {
        "category_id": category_id,
        "partner": {k: card.get(k) for k in ("partner_id", "name", "concept", "department", "url", "phone", "status",
                                              "review_status", "needs_handover", "human_takeover", "not_agreed_reason",
                                              "ecommerce_platform", "products_to_bring_in", "worth_pursuing_reason",
                                              "last_message_at", "source", "call_slot", "call_contact_name")},
        "department_label": brain.department_label(card.get("department")),
        "images": _loads(card.get("brand_images"), [])[:4],
        "pipeline": _report_pipeline(card, msgs, profile, skus),
        "research": research,
        "call": {"slot": card.get("call_slot"), "contact": card.get("call_contact_name")} if card.get("call_slot") else None,
        "next_slots": brain.call_slots(),
        "msg_count": len(msgs),
        "built": bool(profile),
        "built_at": rep.get("built_at"),
        "stale": not profile or rep.get("msg_count") != len(msgs),
        "building": building,
        "build_job": job if building else None,
        "queued": category_id in _report_queue,
        "error": rep.get("error"),
        "catalog": {k: catalog.get(k) for k in ("count", "source", "note")},
        "profile": profile,
        "kapruka": kapruka,
        "skus": skus, "sku_source": sku_src, "min_skus": partner_report.MIN_SKUS,
        "verdict": partner_report.lucrative_verdict(profile, catalog, kapruka, research) if profile else None,
        "standard_questions": brain.standard_brief_questions(),
        "partner_questions": qs,
    }


def _upgrade_existing_reports() -> None:
    """Startup pass for reports built before 2026-10-14's two fixes (user: "why
    no facebook, instagram followers detected, tiktok" / "theres SKU more than
    six"): (1) follower counts read straight from each profile replace web
    research's estimates — brand value recomputed, no rebuild needed; (2) cards
    with no exact product list are rebuilt so the shop-subdomain check runs.
    Idempotent: a report whose followers were already read directly is skipped."""
    try:
        with db.conn() as c:
            rows = c.execute(
                "SELECT r.category_id, r.research_json, r.catalog_json, wc.social_links FROM card_reports r "
                "JOIN weekly_categories wc ON wc.id = r.category_id WHERE wc.source = 'brand_discovery' "
                "AND wc.review_status = 'pending_review' AND r.research_json IS NOT NULL").fetchall()
        rebuild = []
        for r in rows:
            research = _loads(r["research_json"], {})
            if research and not any(f.get("read") for f in research.get("followers") or []) \
                    and not research.get("followers_read_at"):
                research = partner_report.refresh_followers(research, r["social_links"])
                research["followers_read_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
                with db.conn() as c:
                    c.execute("UPDATE card_reports SET research_json = ? WHERE category_id = ?",
                              (json.dumps(research, ensure_ascii=False), r["category_id"]))
                time.sleep(1)  # gentle on the social sites
            cat = _loads(r["catalog_json"], {})
            if not cat.get("hosts_checked") and (cat.get("count") is None or "sitemap" in str(cat.get("source") or "")):
                rebuild.append(r["category_id"])
        if rebuild:
            _queue_report_builds(rebuild)
    except Exception as e:
        print(f"[report upgrade] {e}")


def _run_report_build(job_id: str, category_id: int, force_kapruka: bool, force_site: bool) -> None:
    errors = []
    try:
        card = _report_card(category_id)
        conv = [m for m in _card_messages(card, with_body=True) if m["sender"] != "system"]
        with db.conn() as c:
            rep = c.execute("SELECT * FROM card_reports WHERE category_id = ?", (category_id,)).fetchone()
            has_qs = c.execute("SELECT 1 FROM card_questions WHERE category_id = ? LIMIT 1", (category_id,)).fetchone()
            qs = [r["question"] for r in c.execute("SELECT question FROM card_questions WHERE category_id = ? "
                                                    "AND status = 'approved' ORDER BY id", (category_id,)).fetchall()]
        rep = dict(rep) if rep else {}

        catalog = _loads(rep.get("catalog_json"), None)
        if catalog is None or force_site or catalog.get("count") is None \
                or "sitemap" in str(catalog.get("source") or ""):  # no exact list yet: always re-check (cheap)
            catalog = partner_report.store_catalog(card.get("url"), card.get("ecommerce_platform"))
        site = _loads(rep.get("site_json"), None)
        if site is None or force_site or "product_pages" not in site:  # older scans lack the product-page count
            site = partner_report.site_location_text(card.get("url"))
        if (catalog.get("count") is None or "sitemap" in str(catalog.get("source") or "")) and site.get("product_pages"):
            # no store product API: distinct product pages linked on their site, a real lower bound
            catalog = {**catalog, "seen_count": site["product_pages"]}
        research = _loads(rep.get("research_json"), None)
        if research is None or force_site:
            try:
                research = partner_report.web_research(card, model=db.get_settings().get("claude_model", "claude-sonnet-5"))
            except Exception as e:
                # keep the last good research (2026-10-14: a timed-out refresh wiped Cecrafto's found location)
                research = _loads(rep.get("research_json"), None)
                errors.append(f"web research failed{' (kept the previous result)' if research else ''}: {str(e)[:150]}")
        if research:  # exact follower counts straight from each profile (cheap; web search misses IG/TikTok)
            research = partner_report.refresh_followers(research, card.get("social_links"))

        profile = _loads(rep.get("profile_json"), {})
        try:
            profile = partner_report.extract_profile(card, catalog, conv, brain.standard_brief_questions() + qs,
                                                     site_text=site.get("snippets", ""))
        except Exception as e:
            errors.append(f"summary/analysis failed: {str(e)[:200]}")

        kapruka = _loads(rep.get("kapruka_json"), [])
        terms = profile.get("product_types") or []
        if terms and (force_kapruka or json.dumps(terms) != rep.get("kapruka_terms")):
            kapruka = partner_report.kapruka_check(terms)

        if not has_qs:
            try:
                for s in partner_report.suggest_questions(card, catalog, brain.standard_brief_questions()):
                    with db.conn() as c:
                        c.execute("INSERT INTO card_questions (category_id, question, why, source, status, created_at) "
                                  "VALUES (?,?,?, 'ai', 'suggested', ?)", (category_id, s["question"], s["why"], db.now()))
            except Exception as e:
                errors.append(f"question suggestions failed: {str(e)[:150]}")

        with db.conn() as c:
            c.execute(
                "INSERT INTO card_reports (category_id, catalog_json, site_json, research_json, profile_json, kapruka_json, "
                "kapruka_terms, error, msg_count, built_at) VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(category_id) DO UPDATE SET "
                "catalog_json=excluded.catalog_json, site_json=excluded.site_json, research_json=excluded.research_json, "
                "profile_json=excluded.profile_json, "
                "kapruka_json=excluded.kapruka_json, kapruka_terms=excluded.kapruka_terms, error=excluded.error, "
                "msg_count=excluded.msg_count, built_at=excluded.built_at",
                (category_id, json.dumps(catalog), json.dumps(site), json.dumps(research) if research is not None else None,
                 json.dumps(profile) if profile else None,
                 json.dumps(kapruka), json.dumps(terms), "; ".join(errors) or None, len(conv), db.now()))
        with _JOBS_LOCK:
            _JOBS[job_id] = {"status": "done", "error": "; ".join(errors) or None}
    except Exception as e:
        with _JOBS_LOCK:
            _JOBS[job_id] = {"status": "error", "error": str(e)[:300]}


def _start_report_build(category_id: int, force_kapruka: bool = False, force_site: bool = False) -> str:
    with _JOBS_LOCK:
        running = _REPORT_BUILDS.get(category_id)
        if running and _JOBS.get(running, {}).get("status") == "running":
            return running
        job_id = uuid.uuid4().hex
        _JOBS[job_id] = {"status": "running", "error": None}
        _REPORT_BUILDS[category_id] = job_id
    threading.Thread(target=_run_report_build, args=(job_id, category_id, force_kapruka, force_site),
                     daemon=True).start()
    return job_id


@app.route("/api/cards/<int:category_id>/report")
def api_card_report(category_id):
    data = _report_payload(category_id)
    if not data:
        return jsonify({"ok": False, "error": "unknown business"}), 404
    return jsonify({"ok": True, **data})


@app.route("/api/cards/<int:category_id>/report/build", methods=["POST"])
def api_card_report_build(category_id):
    if not _report_card(category_id):
        return jsonify({"ok": False, "error": "unknown business"}), 404
    body = request.get_json(silent=True) or {}
    job = _start_report_build(category_id, bool(body.get("force_kapruka")), bool(body.get("force_site")))
    return jsonify({"ok": True, "job_id": job})


@app.route("/api/report_jobs/<job_id>")
def api_report_job_poll(job_id):
    with _JOBS_LOCK:
        job = dict(_JOBS.get(job_id, {"status": "error", "error": "unknown job"}))
    return jsonify({"ok": True, **job})


@app.route("/api/cards/<int:category_id>/questions", methods=["POST"])
def api_card_question_add(category_id):
    body = request.get_json(silent=True) or {}
    q = (body.get("question") or "").strip()
    if not q:
        return jsonify({"ok": False, "error": "write the question first"}), 400
    if not _report_card(category_id):
        return jsonify({"ok": False, "error": "unknown business"}), 404
    with db.conn() as c:
        c.execute("INSERT INTO card_questions (category_id, question, source, status, added_by, created_at, decided_at) "
                  "VALUES (?,?, 'human', 'approved', ?, ?, ?)",
                  (category_id, q[:300], (body.get("added_by") or "").strip()[:80] or None, db.now(), db.now()))
    return jsonify({"ok": True})


@app.route("/api/cards/<int:category_id>/questions/suggest", methods=["POST"])
def api_card_question_suggest(category_id):
    card = _report_card(category_id)
    if not card:
        return jsonify({"ok": False, "error": "unknown business"}), 404
    with db.conn() as c:
        rep = c.execute("SELECT catalog_json FROM card_reports WHERE category_id = ?", (category_id,)).fetchone()
        existing = [r["question"] for r in c.execute(
            "SELECT question FROM card_questions WHERE category_id = ?", (category_id,)).fetchall()]
    catalog = _loads(rep["catalog_json"] if rep else None, {})
    try:
        new = partner_report.suggest_questions(card, catalog, brain.standard_brief_questions() + existing)
    except Exception as e:
        return jsonify({"ok": False, "error": f"could not suggest questions: {str(e)[:200]}"}), 502
    with db.conn() as c:
        for s in new:
            c.execute("INSERT INTO card_questions (category_id, question, why, source, status, created_at) "
                      "VALUES (?,?,?, 'ai', 'suggested', ?)", (category_id, s["question"], s["why"], db.now()))
    return jsonify({"ok": True, "added": len(new)})


@app.route("/api/card_questions/<int:qid>", methods=["POST"])
def api_card_question_decide(qid):
    status = ((request.get_json(silent=True) or {}).get("status") or "").strip()
    if status not in ("approved", "rejected", "suggested"):
        return jsonify({"ok": False, "error": "status must be approved, rejected or suggested"}), 400
    with db.conn() as c:
        cur = c.execute("UPDATE card_questions SET status = ?, decided_at = ? WHERE id = ?", (status, db.now(), qid))
    if not cur.rowcount:
        return jsonify({"ok": False, "error": "unknown question"}), 404
    return jsonify({"ok": True})


@app.route("/api/card_questions/<int:qid>", methods=["DELETE"])
def api_card_question_delete(qid):
    with db.conn() as c:
        c.execute("DELETE FROM card_questions WHERE id = ?", (qid,))
    return jsonify({"ok": True})


def _log_message(partner_id: int, sender: str, body: str, simulated: bool = True,
                  atts: list[dict] | None = None, send_status: str | None = None,
                  wa_message_id: str | None = None, send_error: str | None = None) -> int:
    """Records one message row. Only records — outbound partner-facing text
    must go through _send_and_log, which decides whether it is really sent.
    simulated=True means no WhatsApp API call delivered it."""
    if send_status is None:
        send_status = "simulated" if simulated else "sent"
    with db.conn() as c:
        cur = c.execute(
            "INSERT INTO messages (partner_id, sender, body, simulated, created_at, attachments, "
            "send_status, wa_message_id, send_error) VALUES (?,?,?,?,?,?,?,?,?)",
            (partner_id, sender, body, int(simulated), db.now(), attachments.dumps(atts),
             send_status, wa_message_id, send_error)
        )
        c.execute("UPDATE partners SET last_message_at = ? WHERE id = ?", (db.now(), partner_id))
        return cur.lastrowid


# ---------------------------------------------------------- outbound WhatsApp

def _wa_live() -> bool:
    return db.get_settings().get("whatsapp_live") == "1"


def _to_wa_number(raw: str | None) -> str:
    """E.164 digits without '+'. Local Sri Lankan numbers (0771234567 /
    771234567) get the 94 country code."""
    digits = re.sub(r"\D", "", raw or "")
    if digits.startswith("00"):
        digits = digits[2:]
    if digits.startswith("0") and len(digits) == 10:
        digits = "94" + digits[1:]
    elif len(digits) == 9:
        digits = "94" + digits
    return digits


def _is_sl_mobile(raw: str | None) -> bool:
    d = _to_wa_number(raw)
    return d.startswith("947") and len(d) == 11


_WINDOW_SAFE_HOURS = 23.5  # WhatsApp's window is 24h; stop free-form text a little early


def _hours_since_partner_wrote(partner_id: int) -> float | None:
    """Hours since the partner's last REAL inbound message, or None if they
    never wrote (or the timestamp is unreadable)."""
    with db.conn() as c:
        row = c.execute(
            "SELECT created_at FROM messages WHERE partner_id = ? AND sender = 'partner' AND simulated = 0 "
            "ORDER BY id DESC LIMIT 1", (partner_id,)
        ).fetchone()
    if not row:
        return None
    try:
        return _hours_since(row["created_at"])
    except ValueError:
        return None


def _customer_window_open(partner_id: int) -> bool:
    """WhatsApp only allows free-form text within 24h of the partner's last
    REAL inbound message; outside it, only an approved template goes through."""
    hours = _hours_since_partner_wrote(partner_id)
    return hours is not None and hours < _WINDOW_SAFE_HOURS


def _send_and_log(partner_id: int, sender: str, body: str, atts: list[dict] | None = None,
                  to_phone: str | None = None) -> dict:
    """The one place a partner-facing message leaves the app. In simulation
    mode it is only logged (send_status='simulated'). In live mode it is sent
    for real; a failure is logged as send_status='failed' with the reason and
    reported back (ok=False) — never silently recorded as sent."""
    if not _wa_live():
        mid = _log_message(partner_id, sender, body, simulated=True, atts=atts)
        return {"ok": True, "sent": False, "simulated": True, "message_id": mid}

    if to_phone is None:
        with db.conn() as c:
            row = c.execute("SELECT phone FROM partners WHERE id = ?", (partner_id,)).fetchone()
        to_phone = row["phone"] if row else None
    to = _to_wa_number(to_phone)
    if len(to) < 10:
        err = "no valid phone number on file"
        mid = _log_message(partner_id, sender, body, simulated=True, atts=atts,
                           send_status="failed", send_error=err)
        return {"ok": False, "sent": False, "error": err, "message_id": mid}

    import notify_whatsapp
    cfg = _project_env()
    settings = db.get_settings()
    logged_body = body
    try:
        if _customer_window_open(partner_id):
            res = notify_whatsapp.send_text(cfg, to, body)
        else:
            template = (settings.get("wa_outreach_template") or "").strip()
            if not template:
                raise RuntimeError("partner is outside WhatsApp's 24h reply window and no approved outreach "
                                   "template is set (Settings → WhatsApp), so free-form text would be rejected")
            res = notify_whatsapp.send_template(cfg, to, template,
                                                (settings.get("wa_outreach_template_lang") or "en_US").strip())
            logged_body = f"[sent as approved template '{template}'] {body}"
        if isinstance(res, dict) and res.get("error"):
            raise RuntimeError(res.get("message") or f"WhatsApp API error {res.get('code')}")
        wa_id = ((res or {}).get("messages") or [{}])[0].get("id")
        if not wa_id:
            raise RuntimeError("WhatsApp API returned no message id")
        for a in atts or []:
            try:
                notify_whatsapp.send_document(cfg, to, attachments.path_of(a), None, False)
            except (Exception, SystemExit) as e:
                _log_message(partner_id, "system", f"Attachment {a.get('orig')} failed to send: {e}", simulated=True)
    except (Exception, SystemExit) as e:
        err = str(e)[:400]
        mid = _log_message(partner_id, sender, body, simulated=True, atts=atts,
                           send_status="failed", send_error=err)
        return {"ok": False, "sent": False, "error": err, "message_id": mid}
    mid = _log_message(partner_id, sender, logged_body, simulated=False, atts=atts,
                       send_status="sent", wa_message_id=wa_id)
    return {"ok": True, "sent": True, "simulated": False, "message_id": mid, "wa_message_id": wa_id}


def _auto_reply_blocked(partner) -> str | None:
    """Why PC Agent must stay quiet on this chat, or None if it may reply."""
    if partner["human_takeover"]:
        return "a human has taken over this chat"
    if partner["needs_handover"]:
        return "waiting for a human (handover requested)"
    if partner["status"] == "agreed":
        return "partner agreed — the Partner Central team owns this chat now"
    if partner["status"] == "removed":
        return "partner was removed"
    return None


def _record_partner_inbound(partner_id: int, text: str, simulated: bool,
                            atts: list[dict] | None = None, wa_message_id: str | None = None) -> int:
    """Logs a partner message and moves the funnel: a reply revives an
    unresponsive chat and stops the no-reply clock."""
    mid = _log_message(partner_id, "partner", text, simulated=simulated, atts=atts,
                       send_status="received", wa_message_id=wa_message_id)
    with db.conn() as c:
        c.execute("UPDATE partners SET status = 'replied' WHERE id = ? AND status IN ('new','contacted','unresponsive')",
                  (partner_id,))
        c.execute("UPDATE partners SET followup_stage = 'none', followup_anchor_at = NULL WHERE id = ?",
                  (partner_id,))
    return mid


def _hold_for_human(partner_id: int, partner, why: str, detail: str = "") -> None:
    """Something went wrong generating a reply — park the chat in the
    'Needs human intervention' queue and tell the team, instead of either
    sending something broken or going silent unseen."""
    with db.conn() as c:
        c.execute("UPDATE partners SET needs_handover = 1 WHERE id = ?", (partner_id,))
    _log_message(partner_id, "system", f"PC Agent held this chat for a human: {why}"
                 + (f"\n{detail[:500]}" if detail else ""), simulated=True)
    name = partner["name"] if partner else f"partner #{partner_id}"
    _notify_pc_team(f"PC Agent needs you: {name}", f"{name} needs a human.\n\nReason: {why}\n\n{detail[:1500]}")
    _notify_pc_team_whatsapp(f"PC Agent needs you: {name} — {why}")


def _notify_partner_agreed(partner_id: int, how: str) -> None:
    """Hot-lead alert with the full transcript. `how` says whether a human
    clicked Mark Agreed or PC Agent detected the yes itself."""
    with db.conn() as c:
        partner = c.execute(
            """SELECT p.*, wc.concept, wc.department FROM partners p
               JOIN weekly_categories wc ON wc.id = p.weekly_category_id WHERE p.id = ?""",
            (partner_id,)
        ).fetchone()
        msgs = c.execute("SELECT * FROM messages WHERE partner_id = ? ORDER BY id", (partner_id,)).fetchall()
    if not partner:
        return
    names = {"kogul": "PC Agent", "partner": partner["name"], "human": "Kapruka (human)", "system": "SYSTEM"}
    transcript = "\n".join(f"{names.get(m['sender'], m['sender'])}: {m['body']}" for m in msgs)
    # 2026-10-14, user: "along with the time and name - it should send the email and whatsapp notification"
    slot, contact = partner["call_slot"], partner["call_contact_name"]
    call_line = (f"CALL BOOKED: {slot} (Sri Lanka time)" + (f", ask for {contact}" if contact else "")
                 if slot else "No call time booked yet: please contact them to arrange one.")
    _notify_pc_team(
        f"HOT LEAD — call booked: {partner['name']}" + (f" — {slot}" if slot else ""),
        f"{partner['name']} has agreed to join Partner Central ({how}).\n\n{call_line}\n\n"
        f"Business: {partner['concept']} ({brain.department_label(partner['department'])})\n"
        f"Phone: {partner['phone'] or '(none on file)'}\n\n--- Full conversation ---\n{transcript}\n"
    )
    _notify_pc_team_whatsapp(
        f"HOT LEAD: {partner['name']} ({brain.department_label(partner['department'])}) wants to join "
        f"Partner Central. {call_line}. Phone: {partner['phone'] or 'none on file'}"
    )


# 2026-10-14, user: "i want in settings to Partner Central team email and
# number to fill so notification will go ino theirs we can add upto 3 email
# and 3 numbers" — recipients now come from db settings (pc_team_emails/
# pc_team_phones, set under PC Agent Settings), read fresh on every send,
# not a hardcoded constant. This fallback is only used if the settings
# arrays are genuinely empty (e.g. a fresh install before anyone's filled
# them in) so a human-handover alert is never silently lost to an empty
# recipient list — matches the project's standing fail-open discipline.
_NOTIFY_RECIPIENTS_FALLBACK = ["shanaka@kapruka.com"]


def _pc_team_emails() -> list[str]:
    settings = db.get_settings()
    try:
        emails = [e.strip() for e in json.loads(settings.get("pc_team_emails") or "[]") if e and e.strip()]
    except Exception:
        emails = []
    legacy = (settings.get("pc_team_email") or "").strip()
    return emails or ([legacy] if legacy else _NOTIFY_RECIPIENTS_FALLBACK)


def _pc_team_phones() -> list[str]:
    try:
        phones = json.loads(db.get_settings().get("pc_team_phones") or "[]")
        return [p for p in phones if p]
    except Exception:
        return []


def _notify_pc_team(subject: str, body: str) -> None:
    """Real email notification on the two triggers that matter: a partner
    agreeing (pushed to PC) and a chat needing human handover — 2026-10-06,
    user: "if wat to know how human will be notified if someones is
    interesered or if theres a human takeover needed". Before this, the only
    notification was the in-app handover badge/sound (never fires if the
    browser tab isn't open) and api_partner_set_status's own log message
    literally claimed "pushed to the Partner Central team email" while no
    email was ever sent — a real gap, not just a missing feature.

    Fail-open and silent: notification failure must never break the request
    that triggered it (a status update or a chat reply succeeding is the
    real thing that matters; the email is a best-effort side notification).
    If GMAIL_ADDRESS/GMAIL_APP_PASSWORD aren't set in .env yet, this is a
    no-op (notify_email.send_email returns a clean failure dict, swallowed
    here)."""
    try:
        from _env import env as _mail_env
        import notify_email
        notify_email.send_email(_mail_env(), to=_pc_team_emails(), subject=subject, body=body)
    except Exception:
        pass  # fail-open — see docstring


def _send_team_whatsapp(cfg: dict, to: str, text: str) -> dict:
    """One WhatsApp alert to one PC-team number. {ok, how, error, note}.
    With an alert template set, the alert text travels as the template's
    {{1}} body variable (always delivered). Without one, plain text is sent;
    Meta accepts it but only DELIVERS it if this number messaged the business
    in the last 24h (error 131047 otherwise, reported later, not here)."""
    import notify_whatsapp
    settings = db.get_settings()
    tmpl = (settings.get("wa_alert_template") or "").strip()
    try:
        if tmpl:
            # template variables may not contain newlines, tabs or 4+ spaces
            param = re.sub(r"\s{2,}", " ", re.sub(r"[\r\n\t]+", " | ", text)).strip()[:1000]
            res = notify_whatsapp.send_template(
                cfg, to, tmpl, (settings.get("wa_alert_template_lang") or "en_US").strip(),
                components=[{"type": "body", "parameters": [{"type": "text", "text": param}]}])
            how = f"template '{tmpl}'"
        else:
            res = notify_whatsapp.send_text(cfg, to, text)
            how = "plain text"
        if isinstance(res, dict) and res.get("error"):
            return {"ok": False, "how": how, "error": res.get("message") or str(res.get("error"))}
        mid = ((res or {}).get("messages") or [{}])[0].get("id")
        if not mid:
            return {"ok": False, "how": how, "error": "WhatsApp returned no message id"}
        note = None if tmpl else ("Accepted by WhatsApp, but plain text is only delivered if this number has "
                                  "messaged the business number in the last 24 hours. Set an alert template "
                                  "(Settings → WhatsApp sending) for reliable alerts.")
        return {"ok": True, "how": how, "error": None, "note": note}
    except (Exception, SystemExit) as e:
        return {"ok": False, "how": "template" if tmpl else "plain text", "error": str(e)[:300]}


def _notify_pc_team_whatsapp(text: str) -> None:
    """Real WhatsApp notification to every number in pc_team_phones, mirror
    of _notify_pc_team but over WhatsApp (2026-10-14, user: "i want to know
    how human intervention and interested candidates will be informed... i
    want it both email and to whatsapp message as well").

    Two-step send per recipient (user-confirmed approach): WhatsApp refuses
    a plain text message to a number that hasn't messaged this WABA number
    within the last 24h (error 131047) — only a pre-approved TEMPLATE can
    open that window cold. Kogul only has Meta's free hello_world template
    (fixed greeting, no custom text) available today, so this sends
    hello_world first to guarantee the window is open, then immediately
    sends the real notification text as a normal follow-up message. On a
    recipient whose window was already open (they'd messaged recently),
    this costs one harmless extra "hello_world" greeting; it never blocks
    or delays the real text.

    Fail-open per recipient: one number failing (bad number, API error)
    never stops the others from being notified."""
    phones = _pc_team_phones()
    if not phones:
        return
    try:
        from _env import env as _wa_env
        import notify_whatsapp
        cfg = _wa_env()
        for phone in phones:
            # 2026-10-14: the old "hello_world template, then text" fallback
            # never worked — a business-sent template does not open the 24h
            # window, so the text was dropped (user saw only hello_world).
            r = _send_team_whatsapp(cfg, _to_wa_number(phone), text)
            if not r["ok"]:
                print(f"[team whatsapp] {phone}: {r['error']}", file=sys.stderr)  # fail-open per recipient
    except (Exception, SystemExit):
        pass  # fail-open — see docstring


@app.route("/api/settings/test_notification", methods=["POST"])
def api_settings_test_notification():
    """Real test send, email and/or WhatsApp, to EXPLICIT recipients
    (2026-10-14, user: "have a test button on email... test all or test
    each email or number") — unlike _notify_pc_team/_notify_pc_team_whatsapp
    (fail-open/silent, correct for a real production trigger), this reports
    REAL per-recipient success/failure back to the caller, since the whole
    point of a test button is to see whether it actually worked.

    body: {emails: [...], phones: [...]} — explicit list, NOT read from
    saved settings, so a value can be tested before it's even saved (e.g.
    typed into the form, not yet clicked Save)."""
    body = request.get_json(silent=True) or {}
    emails = [e.strip() for e in (body.get("emails") or []) if e and e.strip()]
    phones = [p.strip() for p in (body.get("phones") or []) if p and p.strip()]
    if not emails and not phones:
        return jsonify({"ok": False, "error": "no email or phone given"}), 400

    results = {"emails": {}, "phones": {}}
    subject = "PC Agent test notification"
    text = ("This is a real test notification from PC Agent (Kapruka Partner Central). "
            "If you're reading this, the notification path to this contact works.")

    if emails:
        try:
            from _env import env as _mail_env
            import notify_email
            cfg = _mail_env()
            for email in emails:
                r = notify_email.send_email(cfg, to=[email], subject=subject, body=text)
                results["emails"][email] = {"ok": bool(r.get("ok")), "error": r.get("error")}
        except Exception as e:
            for email in emails:
                results["emails"].setdefault(email, {"ok": False, "error": str(e)})

    if phones:
        try:
            from _env import env as _wa_env
            import notify_whatsapp
            cfg = _wa_env()
            for phone in phones:
                # same path a real alert takes (_send_team_whatsapp), so the
                # test proves what production will do — incl. the honest note
                # that plain text needs the recipient's 24h window
                r = _send_team_whatsapp(cfg, _to_wa_number(phone), text)
                results["phones"][phone] = {"ok": r["ok"], "error": r["error"], "note": r.get("note"), "how": r["how"]}
        except Exception as e:
            for phone in phones:
                results["phones"].setdefault(phone, {"ok": False, "error": str(e)})

    return jsonify({"ok": True, "results": results})


@app.route("/api/partners/<int:partner_id>/start_outreach", methods=["POST"])
def api_partner_start_outreach(partner_id):
    """Kicks off PC Agent's opening message to a 'new' partner. Kept as a
    manual per-partner button for cases where automatic first-message
    (see _run_category_approve) didn't fire — e.g. a partner added to an
    already-approved category later, or a retry after an error."""
    result, status = _do_partner_auto_reply(partner_id, opening=True)
    return jsonify(result), status


@app.route("/api/partners/<int:partner_id>/reply_from_partner", methods=["POST"])
def api_partner_reply_from_partner(partner_id):
    """Simulated inbound — stands in for the not-yet-built WhatsApp webhook
    (user explicitly deferred webhook setup). Lets a human paste in what the
    partner actually said (e.g. read off a phone) so Kogul can react, or
    lets this be used for manual testing of the conversation flow.

    2026-10-14, user: not wanted for real partner chats. Refused while live
    sending is on: real replies arrive via the WhatsApp webhook, and a pasted
    one would be recorded as if the partner wrote it."""
    if _wa_live():
        return jsonify({"ok": False, "error": "Logging a partner reply is for simulation mode only. "
                                              "In live mode the partner's real replies arrive automatically."}), 403
    if request.content_type and "multipart/form-data" in request.content_type:
        text = (request.form.get("text") or "").strip()
        files = request.files.getlist("files")
    else:
        body = request.get_json(silent=True) or {}
        text = (body.get("text") or "").strip()
        files = []
    if not text and not files:
        return jsonify({"ok": False, "error": "missing text"}), 400
    with db.conn() as c:
        p = c.execute("SELECT status FROM partners WHERE id = ?", (partner_id,)).fetchone()
        if not p:
            return jsonify({"ok": False, "error": "unknown partner"}), 404
    try:
        in_atts = attachments.save_uploads(files) if files else []
    except attachments.AttachmentError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    _record_partner_inbound(partner_id, text, simulated=True, atts=in_atts)
    result, status = _do_partner_auto_reply(partner_id, opening=False)
    return jsonify(result), status


_PARTNER_REPLY_LOCKS: dict[int, threading.Lock] = {}
_PARTNER_REPLY_LOCKS_GUARD = threading.Lock()


def _partner_lock(partner_id: int) -> threading.Lock:
    with _PARTNER_REPLY_LOCKS_GUARD:
        return _PARTNER_REPLY_LOCKS.setdefault(partner_id, threading.Lock())


def _do_partner_auto_reply(partner_id: int, opening: bool) -> tuple[dict, int]:
    """Generates PC Agent's next message for one partner and sends it via
    _send_and_log. Returns (body_dict, http_status) — never a Flask response —
    so it works from background threads too. Serialized per partner so a
    burst of inbound messages, the scheduler and a button click can't
    produce parallel replies.

    opening=True writes a fresh opening message (used on approve, Start
    outreach, and when switching to a second number) even if older messages
    exist. body["auto_replied"] is True only when a partner-facing message
    was produced."""
    with _partner_lock(partner_id):
        return _do_partner_auto_reply_locked(partner_id, opening)


def _do_partner_auto_reply_locked(partner_id: int, opening: bool) -> tuple[dict, int]:
    with db.conn() as c:
        partner = c.execute(
            """SELECT p.*, wc.concept, wc.department, wc.source AS category_source,
                      wc.products_to_bring_in, wc.ecommerce_platform FROM partners p
               JOIN weekly_categories wc ON wc.id = p.weekly_category_id WHERE p.id = ?""",
            (partner_id,)
        ).fetchone()
        if not partner:
            return {"ok": False, "error": "unknown partner"}, 404
        msgs = c.execute("SELECT * FROM messages WHERE partner_id = ? ORDER BY id", (partner_id,)).fetchall()

    blocked = _auto_reply_blocked(partner)
    if blocked:
        return {"ok": True, "auto_replied": False, "blocked": True, "reason": blocked}, 200

    settings = db.get_settings()
    conversation = [dict(m) for m in msgs]
    prompt_conversation = [] if opening else conversation
    last_atts = attachments.loads(msgs[-1]["attachments"]) if msgs and msgs[-1]["sender"] == "partner" else []
    attachments_block = "" if opening else attachments.describe_for_text_model(
        last_atts, caller=f"kogul.partner_reply_attachment:{partner_id}")
    shareables_block = attachments.shareables_prompt_block()
    prompt = brain.build_partner_reply_prompt(
        partner["concept"], partner["department"], partner["name"], prompt_conversation,
        category_source=partner["category_source"], products_to_bring_in=partner["products_to_bring_in"],
        opening_message_template=settings.get("opening_message_template"),
        ecommerce_platform=partner["ecommerce_platform"],
        attachments_block=attachments_block, shareables_block=shareables_block,
        partner_questions=_approved_partner_questions(partner_id),
        known_facts=_known_facts_for_partner(partner_id))
    # Real partner replies and outreach always use OpenAI gpt-4o; only Chat
    # with PC Agent follows the chat_model setting (Claude CLI).
    try:
        raw_reply = run_openai_chat_text(prompt, model="gpt-4o", timeout=90, caller=f"kogul.partner_reply:{partner_id}")
    except Exception as e:
        if not opening:
            _hold_for_human(partner_id, partner, "PC Agent could not generate a reply (AI service error)", str(e))
        return {"ok": False, "error": f"PC Agent reply generation failed: {e}"}, 500

    with db.conn() as c:
        c.execute(
            "INSERT INTO token_usage (context, model, input_chars, output_chars, created_at) VALUES (?,?,?,?,?)",
            (f"partner_reply:{partner_id}", "gpt-4o", len(prompt), len(raw_reply), db.now())
        )

    decision = brain.parse_agent_output(raw_reply)
    if not decision["ok"]:
        _hold_for_human(partner_id, partner, f"PC Agent's reply could not be read safely ({decision['reason']})",
                        f"Raw model output (NOT sent):\n{raw_reply}")
        return {"ok": False, "error": "unreadable model output — held for a human", "held": True}, 200
    decision = brain.guard_agreed_needs_slot(brain.guard_auto_reject(decision, conversation), conversation)
    if decision.get("guarded"):
        _log_message(partner_id, "system", f"Safety check: {decision['guarded']}", simulated=True)

    action, reason = decision["action"], decision["reason"]
    message, out_atts = attachments.extract_attachments(decision["message"])
    send = _send_and_log(partner_id, "kogul", message, atts=out_atts) if message else {"ok": True, "sent": False}
    delivered = bool(message) and send.get("ok")
    base = {"ok": True, "auto_replied": bool(message), "message": message or None,
            "sent": send.get("sent", False), "send_error": send.get("error"), "action": action}
    if message and not send.get("ok"):
        _hold_for_human(partner_id, partner, "PC Agent's message failed to send over WhatsApp", send.get("error") or "")

    if action == "handover":
        with db.conn() as c:
            c.execute("UPDATE partners SET needs_handover = 1 WHERE id = ?", (partner_id,))
        _log_message(partner_id, "system", f"PC Agent requested human handover: {reason}", simulated=True)
        recent = "\n".join(f"{m['sender']}: {m['body']}" for m in conversation[-6:])
        _notify_pc_team(
            f"PC Agent needs you: {partner['name']}",
            f"{partner['name']} ({partner['concept']}, {brain.department_label(partner['department'])}) needs a human.\n\n"
            f"Reason: {reason}\n\nPhone: {partner['phone'] or '(none on file)'}\n\n"
            f"--- Last few messages ---\n{recent}\n"
        )
        _notify_pc_team_whatsapp(
            f"PC Agent needs you: {partner['name']} ({brain.department_label(partner['department'])}) — {reason}"
        )
        return {**base, "handover": True, "reason": reason}, 200

    if action == "agreed":
        slot, contact = decision.get("call_slot") or "", decision.get("contact_name") or ""
        with db.conn() as c:
            c.execute("UPDATE partners SET status = 'agreed', pushed_to_pc_at = ?, not_agreed_reason = NULL, "
                      "call_slot = ?, call_contact_name = ?, call_booked_at = ? WHERE id = ?",
                      (db.now(), slot, contact or None, db.now(), partner_id))
        _log_message(partner_id, "system", f"PC Agent booked a call with the Partner Central team: {slot}"
                     + (f" (ask for {contact})" if contact else "") + " — pushed to the team.", simulated=True)
        _notify_partner_agreed(partner_id, f"detected by PC Agent: {reason or 'said yes'}")
        return {**base, "agreed": True, "reason": reason}, 200

    if action == "not_agreed":
        with db.conn() as c:
            c.execute("UPDATE partners SET status = 'not_agreed', not_agreed_reason = ? WHERE id = ?",
                      (reason or "declined to give a reason", partner_id))
        return {**base, "not_agreed": True, "reason": reason}, 200

    if delivered or not _wa_live():
        with db.conn() as c:
            c.execute("UPDATE partners SET status = 'contacted' WHERE id = ? AND status = 'new'", (partner_id,))
    return {**base, "handover": False}, 200


# ============================================================ Partner role-play
# 2026-10-04, user: "i need to be ble to chat with kogil like im the partner
# to see how kogul responds" — entirely separate from the real
# partners/messages tables (see db.py's partner_simulations schema note).
# Reuses the exact same brain.build_partner_reply_prompt + OpenAI call real
# outreach uses, so what you see here is genuinely how Kogul would respond
# to a real business, not a simplified mock.

@app.route("/api/simulations", methods=["GET"])
def api_simulations_list():
    with db.conn() as c:
        rows = c.execute("SELECT * FROM partner_simulations ORDER BY id DESC").fetchall()
    return jsonify({"ok": True, "simulations": [dict(r) for r in rows]})


_SIM_DEPARTMENT_LABELS = brain.DEPARTMENT_LABELS


@app.route("/api/simulations", methods=["POST"])
def api_simulations_create():
    """2026-10-04, user: "remove category selection and all those, it should
    be category selection only" — simplified from a (business name + free-
    text category + department) form down to just picking a department; a
    placeholder business name and the department's own label are generated
    here so the rest of the pipeline (build_partner_reply_prompt expects a
    business_name + category) needs no change."""
    body = request.get_json(silent=True) or {}
    department = (body.get("department") or "").strip() or "other"
    category = _SIM_DEPARTMENT_LABELS.get(department, department.replace("_", " ").title())
    with db.conn() as c:
        n = c.execute("SELECT COUNT(*) AS n FROM partner_simulations WHERE department = ?", (department,)).fetchone()["n"]
        business_name = f"Test {category} Business" + (f" {n + 1}" if n else "")
        cur = c.execute(
            "INSERT INTO partner_simulations (business_name, category, department, created_at) VALUES (?,?,?,?)",
            (business_name, category, department, db.now())
        )
        sim_id = cur.lastrowid
    return jsonify({"ok": True, "simulation_id": sim_id})


@app.route("/api/simulations/<int:sim_id>", methods=["DELETE"])
def api_simulations_delete(sim_id):
    with db.conn() as c:
        c.execute("DELETE FROM partner_simulation_messages WHERE simulation_id = ?", (sim_id,))
        c.execute("DELETE FROM partner_simulations WHERE id = ?", (sim_id,))
    return jsonify({"ok": True})


@app.route("/api/simulations/<int:sim_id>/clear", methods=["POST"])
def api_simulations_clear(sim_id):
    """2026-10-04, user: "have an option to clear chats and restart" — keeps
    the scenario (business name/category/department) but wipes its message
    history, distinct from DELETE which removes the scenario entirely."""
    with db.conn() as c:
        sim = c.execute("SELECT id FROM partner_simulations WHERE id = ?", (sim_id,)).fetchone()
        if not sim:
            return jsonify({"ok": False, "error": "unknown simulation"}), 404
        c.execute("DELETE FROM partner_simulation_messages WHERE simulation_id = ?", (sim_id,))
        c.execute("UPDATE partner_simulations SET status = 'new', needs_handover = 0 WHERE id = ?", (sim_id,))
    return jsonify({"ok": True})


@app.route("/api/simulations/<int:sim_id>/set_status", methods=["POST"])
def api_simulation_set_status(sim_id):
    """2026-10-13, user: "make sure stimulate partner has all the
    functionalities like sending notifcaiton as the real partner chats" —
    mirrors api_partner_set_status so a human testing a scenario can force
    Agreed/Not Agreed/Unresponsive without waiting for brain.py's model to
    emit the right sentinel on its own. 'agreed' fires the SAME real email +
    WhatsApp alert the real-partner path sends (see that route), clearly
    labelled [SIMULATION] so the Partner Central team is never misled into
    thinking a real business agreed."""
    body = request.get_json(silent=True) or {}
    status = (body.get("status") or "").strip()
    reason = (body.get("reason") or "").strip()
    valid = {"new", "contacted", "agreed", "not_agreed", "unresponsive"}
    if status not in valid:
        return jsonify({"ok": False, "error": f"status must be one of {sorted(valid)}"}), 400
    if status == "not_agreed" and not reason:
        return jsonify({"ok": False, "error": "not_agreed requires a reason"}), 400
    with db.conn() as c:
        sim = c.execute("SELECT * FROM partner_simulations WHERE id = ?", (sim_id,)).fetchone()
        if not sim:
            return jsonify({"ok": False, "error": "unknown simulation"}), 404
        c.execute("UPDATE partner_simulations SET status = ? WHERE id = ?", (status, sim_id))
        if status == "not_agreed":
            c.execute(
                "INSERT INTO partner_simulation_messages (simulation_id, sender, body, created_at) VALUES (?,?,?,?)",
                (sim_id, "system", f"Marked NOT AGREED — {reason}", db.now())
            )
        elif status == "unresponsive":
            c.execute(
                "INSERT INTO partner_simulation_messages (simulation_id, sender, body, created_at) VALUES (?,?,?,?)",
                (sim_id, "system", "Marked UNRESPONSIVE", db.now())
            )
        elif status == "agreed":
            c.execute(
                "INSERT INTO partner_simulation_messages (simulation_id, sender, body, created_at) VALUES (?,?,?,?)",
                (sim_id, "system", "Marked AGREED — [simulation] notified the Partner Central team.", db.now())
            )
        msgs = c.execute(
            "SELECT * FROM partner_simulation_messages WHERE simulation_id = ? ORDER BY id", (sim_id,)
        ).fetchall()

    if status == "agreed":
        who = {"kogul": "PC Agent", "partner": sim["business_name"], "system": "SYSTEM"}
        transcript = "\n".join(f"{who.get(m['sender'], m['sender'])}: {m['body']}" for m in msgs)
        _notify_pc_team(
            f"[SIMULATION] Partner agreed: {sim['business_name']}",
            f"[This is a Simulate-as-Partner test scenario, not a real business]\n\n"
            f"{sim['business_name']} has agreed to join Partner Central.\n\n"
            f"Category: {sim['category']} ({sim['department']})\n\n"
            f"--- Full conversation ---\n{transcript}\n"
        )
        _notify_pc_team_whatsapp(
            f"[SIMULATION] {sim['business_name']} ({sim['department']}) agreed to join Partner Central!"
        )
    return jsonify({"ok": True})


_SIM_ANALYSIS_PROMPT = """You are analyzing a TEST conversation between PC Agent (Kapruka's partner-onboarding
agent) and a human role-playing a fictional business "{business_name}" ({department}), for QA purposes.

Conversation so far (status: {status}):
{convo}

Output ONLY a JSON object: {{"summary": "<2-3 sentence summary of how PC Agent is handling this
scenario>", "interest_level": "high"|"medium"|"low"|"unclear", "interest_reason": "<one sentence>",
"next_step": "<what PC Agent or the human tester should do next>"}}. No other text, no markdown fences."""


@app.route("/api/simulations/<int:sim_id>/analyze")
def api_simulation_analyze(sim_id):
    """Mirrors api_partner_analyze for Simulate as Partner (2026-10-13, user:
    "make sure stimulate partner has all the functionalities like sending
    notifcaiton as the real partner chats") — same real on-demand GPT-4o
    call, same fail-open discipline, just reading a simulation's own
    business_name/category/department/messages instead of a real partner's."""
    with db.conn() as c:
        sim = c.execute("SELECT * FROM partner_simulations WHERE id = ?", (sim_id,)).fetchone()
        if not sim:
            return jsonify({"ok": False, "error": "unknown simulation"}), 404
        msgs = c.execute(
            "SELECT sender, body FROM partner_simulation_messages WHERE simulation_id = ? ORDER BY id", (sim_id,)
        ).fetchall()
    if not msgs:
        return jsonify({"ok": False, "error": "no conversation yet to analyze"}), 400

    who = {"kogul": "PC Agent", "partner": sim["business_name"], "system": "SYSTEM"}
    convo = "\n".join(f"{who.get(m['sender'], m['sender'])}: {m['body']}" for m in msgs)
    prompt = _SIM_ANALYSIS_PROMPT.format(business_name=sim["business_name"], department=sim["department"],
                                           status=sim["status"], convo=convo)
    try:
        raw = run_openai_chat_text(prompt, model="gpt-4o", timeout=60, caller=f"kogul.simulation_analysis:{sim_id}")
        from claude_cli import extract_json
        data = extract_json(raw)
        if isinstance(data, list):
            data = data[0] if data else {}
    except Exception as e:
        return jsonify({"ok": False, "error": f"analysis failed: {e}"}), 502

    return jsonify({"ok": True, "analysis": {
        "summary": data.get("summary") or "",
        "interest_level": data.get("interest_level") or "unclear",
        "interest_reason": data.get("interest_reason") or "",
        "next_step": data.get("next_step") or "",
    }})


@app.route("/api/simulations/<int:sim_id>/messages")
def api_simulation_messages(sim_id):
    with db.conn() as c:
        sim = c.execute("SELECT * FROM partner_simulations WHERE id = ?", (sim_id,)).fetchone()
        if not sim:
            return jsonify({"ok": False, "error": "unknown simulation"}), 404
        msgs = c.execute(
            "SELECT * FROM partner_simulation_messages WHERE simulation_id = ? ORDER BY id", (sim_id,)
        ).fetchall()
    return jsonify({"ok": True, "simulation": dict(sim), "messages": [dict(m) for m in msgs]})


@app.route("/api/simulations/<int:sim_id>/send", methods=["POST"])
def api_simulation_send(sim_id):
    """The human types a message AS the partner — triggers a real Kogul
    reply (background job, same poll pattern as every other long-running
    Kogul call here). Sending with an empty body is how you kick off the
    OPENING message with nothing said yet (mirrors a real partner having
    just been contacted — build_partner_reply_prompt already handles an
    empty conversation as "this is the opening outreach").

    2026-10-04 gap-audit fix: the partner's message is inserted BEFORE the
    OpenAI call runs (so it's visible immediately while Kogul "types"), but
    a transient API failure (confirmed live: a real ConnectionResetError)
    used to leave that row permanently orphaned — retyping the same message
    and hitting Send again then showed it TWICE in the transcript, since the
    failed attempt's row was never cleaned up. new_msg_id is threaded into
    the background job so it can delete exactly that one row if the OpenAI
    call fails, leaving the chat exactly as if the failed send never
    happened — the user can then safely resend without a duplicate."""
    if request.content_type and "multipart/form-data" in request.content_type:
        text = (request.form.get("text") or "").strip()
        files = request.files.getlist("files")
    else:
        body = request.get_json(silent=True) or {}
        text = (body.get("text") or "").strip()
        files = []
    new_msg_id = None
    with db.conn() as c:
        sim = c.execute("SELECT * FROM partner_simulations WHERE id = ?", (sim_id,)).fetchone()
        if not sim:
            return jsonify({"ok": False, "error": "unknown simulation"}), 404
        # 2026-10-06 fix: same real bug found live in the real-partner path —
        # a simulation that already tripped needs_handover (e.g. a commission
        # question) kept getting auto-replies for every message after that,
        # including ones that should have ended the chat (a later "how do i
        # join"). User confirmed the simulation should mirror real behavior
        # exactly here — stop auto-replying once handed over, same as a real
        # chat, so the simulation is a faithful test of what a real partner
        # would actually experience. Clear via /api/simulations/<id>/clear
        # (resets needs_handover to 0) to keep testing further turns.
        if sim["needs_handover"] and (text or files):
            return jsonify({"ok": True, "auto_replied": False,
                             "reason": "needs_handover is on — reset this simulation to continue testing"})
        try:
            in_atts = attachments.save_uploads(files) if files else []
        except attachments.AttachmentError as e:
            return jsonify({"ok": False, "error": str(e)}), 400
        if text or in_atts:
            cur = c.execute(
                "INSERT INTO partner_simulation_messages (simulation_id, sender, body, created_at, attachments) "
                "VALUES (?,?,?,?,?)",
                (sim_id, "partner", text, db.now(), attachments.dumps(in_atts))
            )
            new_msg_id = cur.lastrowid
    job_id = uuid.uuid4().hex
    with _JOBS_LOCK:
        _JOBS[job_id] = {"status": "running", "error": None}
    threading.Thread(target=_run_simulation_reply, args=(job_id, sim_id, new_msg_id), daemon=True).start()
    return jsonify({"ok": True, "job_id": job_id})


def _run_simulation_reply(job_id: str, sim_id: int, new_msg_id: int | None = None) -> None:
    """Same prompt, model, parser and actions as a real partner reply
    (_do_partner_auto_reply), against the simulation tables. new_msg_id: the
    partner message inserted just before this job — deleted again if the AI
    call fails, so a retry never duplicates it."""
    def _add(sender: str, body: str, atts=None) -> None:
        with db.conn() as c:
            c.execute(
                "INSERT INTO partner_simulation_messages (simulation_id, sender, body, created_at, attachments) "
                "VALUES (?,?,?,?,?)", (sim_id, sender, body, db.now(), attachments.dumps(atts)))

    def _done(**kw) -> None:
        with _JOBS_LOCK:
            _JOBS[job_id] = {"status": "done", "error": None, "handover": False, "message": None, **kw}

    try:
        with db.conn() as c:
            sim = c.execute("SELECT * FROM partner_simulations WHERE id = ?", (sim_id,)).fetchone()
            msgs = c.execute(
                "SELECT * FROM partner_simulation_messages WHERE simulation_id = ? ORDER BY id", (sim_id,)
            ).fetchall()
        if sim["needs_handover"] or sim["status"] == "agreed":
            why = ("waiting for a human (handover requested)" if sim["needs_handover"]
                   else "partner agreed — the Partner Central team owns this chat now")
            _add("system", f"(PC Agent stays quiet here: {why}. A real partner would get no automatic reply.)")
            _done(blocked=True, reason=why)
            return

        settings = db.get_settings()
        conversation = [dict(m) for m in msgs]
        last_atts = attachments.loads(msgs[-1]["attachments"]) if msgs and msgs[-1]["sender"] == "partner" else []
        attachments_block = attachments.describe_for_text_model(last_atts, caller=f"kogul.simulation_attachment:{sim_id}")
        shareables_block = attachments.shareables_prompt_block()
        # brand_discovery context: every real partner today comes from brand
        # discovery, so the test must exercise that same prompt branch.
        prompt = brain.build_partner_reply_prompt(
            sim["business_name"], sim["department"], sim["business_name"], conversation,
            category_source="brand_discovery", products_to_bring_in=None,
            opening_message_template=settings.get("opening_message_template"),
            attachments_block=attachments_block, shareables_block=shareables_block)
        raw_reply = run_openai_chat_text(prompt, model="gpt-4o", timeout=90, caller=f"kogul.simulation:{sim_id}")
        with db.conn() as c:
            c.execute(
                "INSERT INTO token_usage (context, model, input_chars, output_chars, created_at) VALUES (?,?,?,?,?)",
                (f"simulation:{sim_id}", "gpt-4o", len(prompt), len(raw_reply), db.now())
            )

        decision = brain.parse_agent_output(raw_reply)
        if not decision["ok"]:
            _add("system", f"(PC Agent's reply could not be read safely ({decision['reason']}) — a real chat "
                           f"would be held for a human and nothing sent. Raw output: {raw_reply[:400]})")
            with db.conn() as c:
                c.execute("UPDATE partner_simulations SET needs_handover = 1 WHERE id = ?", (sim_id,))
            _done(held=True, reason=decision["reason"])
            return
        decision = brain.guard_agreed_needs_slot(brain.guard_auto_reject(decision, conversation), conversation)
        if decision.get("guarded"):
            _add("system", f"(Safety check: {decision['guarded']})")

        action, reason = decision["action"], decision["reason"]
        message, out_atts = attachments.extract_attachments(decision["message"])
        if message:
            _add("kogul", message, out_atts)
        label = f"{sim['business_name']} ({brain.department_label(sim['department'])})"

        if action == "handover":
            _add("system", f"(PC Agent would hand this off to a human here — reason: {reason})")
            with db.conn() as c:
                c.execute("UPDATE partner_simulations SET status = CASE WHEN status = 'new' THEN 'contacted' "
                          "ELSE status END, needs_handover = 1 WHERE id = ?", (sim_id,))
            _notify_pc_team(
                f"[SIMULATION] PC Agent needs you: {sim['business_name']}",
                f"[This is a Simulate-as-Partner test scenario, not a real business]\n\n"
                f"{label} needs a human.\n\nReason: {reason}\n\n--- Conversation ---\n"
                + "\n".join(f"{m['sender']}: {m['body']}" for m in conversation[-6:]) + "\n"
            )
            _notify_pc_team_whatsapp(f"[SIMULATION] PC Agent needs you: {label} — {reason}")
            _done(handover=True, reason=reason, message=message or None)
            return

        if action == "agreed":
            slot, contact = decision.get("call_slot") or "", decision.get("contact_name") or ""
            call_line = f"CALL BOOKED: {slot} (Sri Lanka time)" + (f", ask for {contact}" if contact else "")
            _add("system", f"(PC Agent booked a call with the Partner Central team: {slot}"
                           + (f", ask for {contact}" if contact else "") + ". A real chat is marked Agreed and "
                           "pushed to the team with this time.)")
            with db.conn() as c:
                c.execute("UPDATE partner_simulations SET status = 'agreed', not_agreed_reason = NULL, call_slot = ?, "
                          "call_contact_name = ?, call_booked_at = ? WHERE id = ?",
                          (slot, contact or None, db.now(), sim_id))
            _notify_pc_team(
                f"[SIMULATION] HOT LEAD — call booked: {sim['business_name']} — {slot}",
                f"[This is a Simulate-as-Partner test scenario, not a real business]\n\n"
                f"{label} agreed to join Partner Central ({reason or 'said yes'}).\n\n{call_line}\n\n"
                f"--- Conversation ---\n"
                + "\n".join(f"{m['sender']}: {m['body']}" for m in conversation) + f"\nkogul: {message}\n"
            )
            _notify_pc_team_whatsapp(f"[SIMULATION] HOT LEAD: {label} agreed to join Partner Central. {call_line}")
            _done(agreed=True, reason=reason, message=message)
            return

        if action == "not_agreed":
            with db.conn() as c:
                c.execute("UPDATE partner_simulations SET status = 'not_agreed', not_agreed_reason = ? WHERE id = ?",
                          (reason or "declined to give a reason", sim_id))
            _done(not_agreed=True, reason=reason, message=message)
            return

        with db.conn() as c:
            c.execute("UPDATE partner_simulations SET status = 'contacted' WHERE id = ? AND status IN ('new','unresponsive')",
                      (sim_id,))
        _done(message=message)
    except Exception as e:
        if new_msg_id is not None:
            with db.conn() as c:
                c.execute("DELETE FROM partner_simulation_messages WHERE id = ?", (new_msg_id,))
        with _JOBS_LOCK:
            _JOBS[job_id] = {"status": "error", "error": str(e)}


@app.route("/api/simulations/poll/<job_id>")
def api_simulation_poll(job_id):
    with _JOBS_LOCK:
        job = dict(_JOBS.get(job_id, {"status": "error", "error": "unknown job"}))
    return jsonify({"ok": True, **job})


@app.route("/api/simulations/<int:sim_id>/retry", methods=["POST"])
def api_simulation_retry(sim_id):
    """2026-10-14 (PC team testing): re-generate one PC Agent reply after a
    rule was added. Deletes that reply and everything after it, clears the
    handover/outcome it may have caused, then runs the normal reply job —
    which reads the rules fresh, so the tester sees the rule take effect."""
    body = request.get_json(silent=True) or {}
    msg_id = body.get("message_id")
    with db.conn() as c:
        msg = c.execute("SELECT id, sender FROM partner_simulation_messages WHERE id = ? AND simulation_id = ?",
                        (msg_id, sim_id)).fetchone()
        if not msg or msg["sender"] != "kogul":
            return jsonify({"ok": False, "error": "pick a PC Agent message from this scenario"}), 400
        c.execute("DELETE FROM partner_simulation_messages WHERE simulation_id = ? AND id >= ?", (sim_id, msg_id))
        left = c.execute("SELECT COUNT(*) AS n FROM partner_simulation_messages WHERE simulation_id = ? "
                         "AND sender = 'kogul'", (sim_id,)).fetchone()["n"]
        c.execute("UPDATE partner_simulations SET needs_handover = 0, not_agreed_reason = NULL, status = ? "
                  "WHERE id = ?", ("contacted" if left else "new", sim_id))
    job_id = uuid.uuid4().hex
    with _JOBS_LOCK:
        _JOBS[job_id] = {"status": "running", "error": None}
    threading.Thread(target=_run_simulation_reply, args=(job_id, sim_id, None), daemon=True).start()
    return jsonify({"ok": True, "job_id": job_id})


# ------------------------------------------------------------------ Team rules
# 2026-10-14, user: the Partner Central team tests PC Agent in Simulate as
# Partner and must be able to "add rules or define rules if any problem is
# detected that will be automatically included". Rules live in agent_rules
# and are appended to the brief in every prompt (brain.read_brief).

_RULE_DRAFT_PROMPT = """You help the Kapruka Partner Central team correct PC Agent, a WhatsApp agent that
invites Sri Lankan businesses to sell on Kapruka. A tester flagged one of its replies.

Conversation up to and including the flagged reply:
{convo}

Flagged PC Agent reply:
\"\"\"{flagged}\"\"\"

What the tester says went wrong:
\"\"\"{problem}\"\"\"

Rules PC Agent already follows (do not repeat one of these):
{existing}

Write ONE rule that would stop this mistake in future chats with ANY business. It must be a
general instruction, not about this test business by name. Plain English, imperative, under
40 words. If an existing rule already covers it, still write the rule, and say which in "note";
otherwise "note" is an empty string.

Output ONLY JSON: {{"rule": "<the rule>", "note": ""}}"""


@app.route("/api/rules")
def api_rules_list():
    with db.conn() as c:
        rows = c.execute("SELECT r.*, s.business_name AS simulation_name FROM agent_rules r "
                         "LEFT JOIN partner_simulations s ON s.id = r.simulation_id ORDER BY r.id DESC").fetchall()
    return jsonify({"ok": True, "rules": [dict(r) for r in rows]})


@app.route("/api/rules", methods=["POST"])
def api_rules_create():
    body = request.get_json(silent=True) or {}
    rule = (body.get("rule") or "").strip()
    if not rule:
        return jsonify({"ok": False, "error": "the rule text is empty"}), 400
    if len(rule) > 1000:
        return jsonify({"ok": False, "error": "keep a rule under 1000 characters"}), 400
    sim_id = body.get("simulation_id")
    with db.conn() as c:
        cur = c.execute(
            "INSERT INTO agent_rules (rule, problem, flagged_reply, simulation_id, added_by, active, created_at) "
            "VALUES (?,?,?,?,?,1,?)",
            (rule, (body.get("problem") or "").strip() or None, (body.get("flagged_reply") or "").strip() or None,
             int(sim_id) if sim_id else None, (body.get("added_by") or "").strip()[:80] or None, db.now()))
    return jsonify({"ok": True, "id": cur.lastrowid})


@app.route("/api/rules/<int:rule_id>", methods=["POST"])
def api_rules_update(rule_id):
    body = request.get_json(silent=True) or {}
    sets, vals = [], []
    if "rule" in body:
        rule = (body.get("rule") or "").strip()
        if not rule:
            return jsonify({"ok": False, "error": "the rule text is empty"}), 400
        sets.append("rule = ?"); vals.append(rule[:1000])
    if "active" in body:
        sets.append("active = ?"); vals.append(1 if body.get("active") else 0)
    if not sets:
        return jsonify({"ok": False, "error": "nothing to update"}), 400
    sets.append("updated_at = ?"); vals.append(db.now())
    with db.conn() as c:
        cur = c.execute(f"UPDATE agent_rules SET {', '.join(sets)} WHERE id = ?", (*vals, rule_id))
    if not cur.rowcount:
        return jsonify({"ok": False, "error": "unknown rule"}), 404
    return jsonify({"ok": True})


@app.route("/api/rules/<int:rule_id>", methods=["DELETE"])
def api_rules_delete(rule_id):
    with db.conn() as c:
        c.execute("DELETE FROM agent_rules WHERE id = ?", (rule_id,))
    return jsonify({"ok": True})


@app.route("/api/rules/draft", methods=["POST"])
def api_rules_draft():
    """Drafts a rule from a flagged simulation reply + the tester's complaint.
    Only a suggestion: the tester edits it before saving."""
    body = request.get_json(silent=True) or {}
    problem = (body.get("problem") or "").strip()
    sim_id, msg_id = body.get("simulation_id"), body.get("message_id")
    if not problem:
        return jsonify({"ok": False, "error": "describe what went wrong first"}), 400
    with db.conn() as c:
        sim = c.execute("SELECT business_name FROM partner_simulations WHERE id = ?", (sim_id,)).fetchone()
        msgs = c.execute("SELECT id, sender, body FROM partner_simulation_messages WHERE simulation_id = ? "
                         "AND id <= ? ORDER BY id", (sim_id, msg_id)).fetchall()
    if not sim or not msgs or msgs[-1]["id"] != msg_id or msgs[-1]["sender"] != "kogul":
        return jsonify({"ok": False, "error": "flagged message not found"}), 404
    who = {"kogul": "PC Agent", "partner": "Business", "system": "SYSTEM NOTE"}
    convo = "\n".join(f"{who.get(m['sender'], m['sender'])}: {m['body']}" for m in msgs[-12:])
    existing = "\n".join(f"- {r['rule']}" for r in brain.active_team_rules()) or "(none yet)"
    prompt = _RULE_DRAFT_PROMPT.format(convo=convo, flagged=msgs[-1]["body"], problem=problem, existing=existing)
    try:
        raw = run_openai_chat_text(prompt, model="gpt-4o", timeout=60, caller=f"kogul.rule_draft:{sim_id}")
        from claude_cli import extract_json
        data = extract_json(raw)
        if isinstance(data, list):
            data = data[0] if data else {}
        rule = (data.get("rule") or "").strip()
        if not rule:
            raise ValueError("empty draft")
    except Exception as e:
        return jsonify({"ok": False, "error": f"could not draft a rule ({e}) - write it yourself"}), 502
    note = (data.get("note") or "").strip()
    if note.lower().strip(".") in ("empty", "none", "n/a", "-"):
        note = ""
    return jsonify({"ok": True, "rule": rule, "note": note})


@app.route("/api/partners/<int:partner_id>/human_message", methods=["POST"])
def api_partner_human_message(partner_id):
    """A human sends a message directly (used when human_takeover is on, or
    to resolve a handover manually)."""
    if request.content_type and "multipart/form-data" in request.content_type:
        text = (request.form.get("text") or "").strip()
        files = request.files.getlist("files")
    else:
        body = request.get_json(silent=True) or {}
        text = (body.get("text") or "").strip()
        files = []
    if not text and not files:
        return jsonify({"ok": False, "error": "missing text"}), 400
    with db.conn() as c:
        p = c.execute("SELECT id FROM partners WHERE id = ?", (partner_id,)).fetchone()
        if not p:
            return jsonify({"ok": False, "error": "unknown partner"}), 404
    try:
        out_atts = attachments.save_uploads(files) if files else []
    except attachments.AttachmentError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    send = _send_and_log(partner_id, "human", text, atts=out_atts)
    if not send.get("ok"):
        # nothing reached the partner — keep the chat in the human queue
        return jsonify({"ok": False, "error": f"Not delivered: {send.get('error')}"}), 502
    # A human replying now owns the chat: takeover on (PC Agent stays quiet
    # until someone hands it back), and it leaves the "needs a human" queue.
    with db.conn() as c:
        c.execute("UPDATE partners SET needs_handover = 0, human_takeover = 1 WHERE id = ?", (partner_id,))
    return jsonify({"ok": True, "sent": send.get("sent", False), "simulated": send.get("simulated", False)})


@app.route("/api/partners/<int:partner_id>/takeover", methods=["POST"])
def api_partner_takeover(partner_id):
    """Toggles human_takeover per chat — when on, Kogul never auto-replies
    to this partner until it's toggled back off. Turning it back OFF also
    clears needs_handover (2026-10-06 fix: a human might toggle takeover off
    to hand it back to PC Agent WITHOUT having sent a message themselves —
    api_partner_human_message already clears needs_handover when a human
    actually replies, but that path was the only way to clear it, so a chat
    stuck on needs_handover with takeover re-disabled and no human message
    sent would otherwise stay permanently blocked by the needs_handover
    check in api_partner_reply_from_partner)."""
    body = request.get_json(silent=True) or {}
    on = bool(body.get("on"))
    with db.conn() as c:
        if on:
            c.execute("UPDATE partners SET human_takeover = 1 WHERE id = ?", (partner_id,))
        else:
            c.execute("UPDATE partners SET human_takeover = 0, needs_handover = 0 WHERE id = ?", (partner_id,))
    return jsonify({"ok": True, "human_takeover": on})


@app.route("/api/partners/<int:partner_id>/set_status", methods=["POST"])
def api_partner_set_status(partner_id):
    """Manually (or Kogul-programmatically, once outcome logic is wired)
    sets a partner's final status. not_agreed requires a reason — the
    dashboard's 'top reasons' breakdown depends on this never being blank
    (memory: kogul-partner-onboarding-agent, 'always log the real reason')."""
    body = request.get_json(silent=True) or {}
    status = (body.get("status") or "").strip()
    reason = (body.get("reason") or "").strip()
    valid = {"new", "contacted", "replied", "negotiating", "agreed", "not_agreed", "unresponsive"}
    if status not in valid:
        return jsonify({"ok": False, "error": f"status must be one of {sorted(valid)}"}), 400
    if status == "not_agreed" and not reason:
        return jsonify({"ok": False, "error": "not_agreed requires a reason"}), 400
    with db.conn() as c:
        if status == "agreed":
            c.execute("UPDATE partners SET status = ?, pushed_to_pc_at = ? WHERE id = ?",
                       (status, db.now(), partner_id))
        elif status == "not_agreed":
            c.execute("UPDATE partners SET status = ?, not_agreed_reason = ? WHERE id = ?",
                       (status, reason, partner_id))
        else:
            c.execute("UPDATE partners SET status = ? WHERE id = ?", (status, partner_id))
    # _log_message opens its own connection — kept outside the UPDATE's block
    # above (two write connections at once = "database is locked").
    if status == "agreed":
        _log_message(partner_id, "system",
                      "Marked AGREED — conversation pushed to the Partner Central team.",
                      simulated=True)
        _notify_partner_agreed(partner_id, "marked by a team member")
    return jsonify({"ok": True})


# ------------------------------------------------------------------ Settings

@app.route("/api/settings")
def api_settings():
    settings = db.get_settings()
    cfg = _project_env()
    effective = {
        "emails": _pc_team_emails(),
        "emails_are_fallback": _pc_team_emails() == _NOTIFY_RECIPIENTS_FALLBACK,
        "phones": _pc_team_phones(),
        "whatsapp_live": settings.get("whatsapp_live") == "1",
        "webhook_signature_ready": bool(cfg.get("META_APP_SECRET")),
        "webhook_verify_token_set": bool(cfg.get("WA_WEBHOOK_VERIFY_TOKEN")),
        "remote_login_set": bool(os.environ.get("KOGUL_ADMIN_PASSWORD") or cfg.get("KOGUL_ADMIN_PASSWORD")),
    }
    return jsonify({"ok": True, "settings": settings, "kogul_md": brain.read_kogul_md(), "effective": effective})


@app.route("/api/settings/update", methods=["POST"])
def api_settings_update():
    """Every field here writes straight to the DB (or KOGUL.md) and is read
    fresh on the next use — no in-memory caching anywhere in this app, per
    the standing instruction: 'make sure all changes are recorded and
    changed in the script'."""
    body = request.get_json(silent=True) or {}
    allowed = {"num_categories_per_week", "partners_per_category", "claude_model", "chat_model",
               "pc_team_email", "opening_message_template", "wa_template_cost_usd",
               "pc_team_emails", "pc_team_phones", "whatsapp_live", "wa_outreach_template",
               "wa_outreach_template_lang", "brief_questions", "wa_alert_template", "wa_alert_template_lang"}
    if "whatsapp_live" in body:
        body["whatsapp_live"] = "1" if str(body["whatsapp_live"]).lower() in ("1", "true", "on") else "0"
    updated = {}
    for k, v in body.items():
        if k not in allowed or v is None:
            continue
        if k == "brief_questions":
            items = [str(x).strip() for x in (v if isinstance(v, list) else str(v).splitlines()) if str(x).strip()]
            v = json.dumps(items[:12])
        elif k in ("pc_team_emails", "pc_team_phones"):
            # v arrives as a JSON-serializable list from the frontend (up to
            # 3 entries, user: "we can add upto 3 email and 3 numbers") —
            # trim blanks and cap at 3 server-side too, never trust the
            # client alone to enforce that.
            items = [str(x).strip() for x in (v if isinstance(v, list) else [v]) if str(x).strip()]
            v = json.dumps(items[:3])
        else:
            v = str(v)
        db.set_setting(k, v)
        updated[k] = v
    return jsonify({"ok": True, "updated": updated})


@app.route("/api/settings/kogul_md", methods=["POST"])
def api_settings_kogul_md():
    body = request.get_json(silent=True) or {}
    content = body.get("content")
    if content is None:
        return jsonify({"ok": False, "error": "missing content"}), 400
    brain.write_kogul_md(content)
    return jsonify({"ok": True})


@app.route("/api/settings/token_usage")
def api_settings_token_usage():
    days = request.args.get("days", 7, type=int)
    with db.conn() as c:
        rows = c.execute(
            "SELECT context, model, input_chars, output_chars, created_at FROM token_usage "
            "WHERE created_at >= datetime('now', ?) ORDER BY id DESC LIMIT 500",
            (f"-{days} days",)
        ).fetchall()
    total_in = sum(r["input_chars"] or 0 for r in rows)
    total_out = sum(r["output_chars"] or 0 for r in rows)
    return jsonify({"ok": True, "calls": len(rows), "total_input_chars": total_in,
                     "total_output_chars": total_out,
                     "approx_input_tokens": total_in // 4, "approx_output_tokens": total_out // 4,
                     "rows": [dict(r) for r in rows]})


# -------------------------------------------------------------------- costs
# 2026-10-06, user: "have a cost tab monitoring template cost - meta its a
# marketing and open ai token cost". Real $ figures, not char-count
# estimates (see api_settings_token_usage above, which predates real $
# tracking and only has chars/4 guesses) — every OpenAI call Kogul makes
# already routes through scripts/openai_cost_tracker.py's call_openai_chat
# (same shared tracker every other script in this project uses), which logs
# REAL cost from the API's own token usage response, per caller tag. Kogul's
# own caller tags all start with "kogul." (kogul.kogul_chat, kogul.simulation:<id>,
# kogul.partner_reply:<id>, kogul.brand_discovery.*, kogul.vision_*, etc.) —
# this endpoint filters the project-wide summary down to just those.
@app.route("/api/costs")
def api_costs():
    import openai_cost_tracker
    full_summary = openai_cost_tracker.summary()
    by_caller = {k: v for k, v in (full_summary.get("by_caller") or {}).items() if k.startswith("kogul.")}
    openai_total_cost = round(sum(v["cost_usd"] for v in by_caller.values()), 6)
    openai_total_calls = sum(v["calls"] for v in by_caller.values())
    openai_total_tokens = sum(v["tokens"] for v in by_caller.values())
    # 2026-10-06, user: "can cache the token to reduce the token costs" —
    # cached_tokens (OpenAI's own report of automatic prompt-cache hits,
    # billed at a 50% discount — see openai_cost_tracker.py) wasn't tracked
    # at all before; surfaced here so the cache is actually visible working,
    # not just assumed. A caller dict written before this fix has no
    # "cached_tokens" key, hence .get(..., 0).
    openai_total_cached = sum(v.get("cached_tokens", 0) for v in by_caller.values())
    # Collapse per-conversation tags (simulation:<id>, partner_reply:<id>) into
    # one feature-level bucket each — "simulation:1", "simulation:2", ... are
    # noise individually, the useful breakdown is by FEATURE.
    by_feature: dict[str, dict] = {}
    for caller, v in by_caller.items():
        feature = re.sub(r":\d+$", "", caller).removeprefix("kogul.")
        b = by_feature.setdefault(feature, {"calls": 0, "tokens": 0, "cached_tokens": 0, "cost_usd": 0.0})
        b["calls"] += v["calls"]
        b["tokens"] += v["tokens"]
        b["cached_tokens"] += v.get("cached_tokens", 0)
        b["cost_usd"] = round(b["cost_usd"] + v["cost_usd"], 6)
    feature_rows = sorted(
        [{"feature": k, **v} for k, v in by_feature.items()],
        key=lambda r: -r["cost_usd"]
    )

    settings = db.get_settings()
    wa_rate = float(settings.get("wa_template_cost_usd") or 0)
    with db.conn() as c:
        wa_sent_count = c.execute(
            "SELECT COUNT(*) AS n FROM whatsapp_test_sends WHERE status != 'failed'"
        ).fetchone()["n"]
        wa_recent = c.execute(
            "SELECT phone_number, template_name, status, created_at FROM whatsapp_test_sends "
            "ORDER BY id DESC LIMIT 20"
        ).fetchall()
    wa_total_cost = round(wa_sent_count * wa_rate, 6)

    return jsonify({
        "ok": True,
        "openai": {
            "total_cost_usd": openai_total_cost, "total_calls": openai_total_calls,
            "total_tokens": openai_total_tokens, "total_cached_tokens": openai_total_cached,
            "by_feature": feature_rows,
        },
        "whatsapp": {
            "rate_per_send_usd": wa_rate, "sends": wa_sent_count,
            "total_cost_usd": wa_total_cost, "recent": [dict(r) for r in wa_recent],
        },
        "grand_total_usd": round(openai_total_cost + wa_total_cost, 6),
    })


# ============================================================ WhatsApp test send
# 2026-10-05, user: "have a whatsapp starter, u have the waba api token and
# phone number id... have the button - send a test whatsapp message - next
# the phone number placeholder, and criteria is that we will open the
# conversation window by dropping the first message, all this is for
# testing purpose to monitor the status change as conversation builds" —
# the FIRST real (non-simulated) WhatsApp send path in Kogul. Everything
# else in this app (partner chats, simulations) logs what PC Agent "would"
# send (messages.simulated=1 / partner_simulation_messages) — this is a
# deliberately separate, minimal path: send one real template message to an
# arbitrary test number via scripts/notify_whatsapp.py's send_template(),
# log it, nothing else. Not wired into the partner/simulation pipelines —
# those stay simulated until a real outbound-sending decision is made for
# the whole app, not just this one test button.

@app.route("/api/whatsapp/send_test", methods=["POST"])
def api_whatsapp_send_test():
    """hello_world (Meta's free pre-approved sample template) is the
    starter until the user's real Kogul-branded template is created +
    approved in Meta Business Manager — per user: "when launching i will
    set the meta whatsapp template and give u the details to configure
    that". Swapping it later is a one-line change (TEMPLATE_NAME below),
    not a redesign."""
    body = request.get_json(silent=True) or {}
    raw_phone = (body.get("phone_number") or "").strip()
    phone = re.sub(r"[^\d]", "", raw_phone)  # E.164 digits only, no '+' — Meta Cloud API convention
    if not phone or len(phone) < 9:
        return jsonify({"ok": False, "error": "enter a valid phone number (with country code)"}), 400

    TEMPLATE_NAME = "hello_world"
    TEMPLATE_LANG = "en_US"

    from _env import env as _wa_env
    import notify_whatsapp

    cfg = _wa_env()
    if not cfg.get("WA_TOKEN") or not cfg.get("WA_PHONE_ID"):
        return jsonify({"ok": False, "error": "WA_TOKEN / WA_PHONE_ID not set in .env"}), 500

    result = notify_whatsapp.send_template(cfg, phone, TEMPLATE_NAME, TEMPLATE_LANG)

    if result.get("error"):
        err_msg = result.get("message") or "unknown error"
        with db.conn() as c:
            c.execute(
                "INSERT INTO whatsapp_test_sends (phone_number, template_name, status, error_message, created_at) "
                "VALUES (?,?,?,?,?)",
                (phone, TEMPLATE_NAME, "failed", err_msg, db.now())
            )
        return jsonify({"ok": False, "error": f"WhatsApp send failed: {err_msg}"}), 502

    wa_message_id = (result.get("messages") or [{}])[0].get("id")
    with db.conn() as c:
        cur = c.execute(
            "INSERT INTO whatsapp_test_sends (phone_number, template_name, wa_message_id, status, created_at) "
            "VALUES (?,?,?,?,?)",
            (phone, TEMPLATE_NAME, wa_message_id, "sent", db.now())
        )
        send_id = cur.lastrowid
    return jsonify({"ok": True, "id": send_id, "wa_message_id": wa_message_id, "template": TEMPLATE_NAME})


@app.route("/api/whatsapp/test_sends")
def api_whatsapp_test_sends():
    with db.conn() as c:
        rows = c.execute(
            "SELECT * FROM whatsapp_test_sends ORDER BY id DESC LIMIT 50"
        ).fetchall()
    return jsonify({"ok": True, "sends": [dict(r) for r in rows]})


# ------------------------------------------------------------- WhatsApp webhook
# Real inbound WhatsApp. Uses the same _record_partner_inbound +
# _do_partner_auto_reply path as the Partner Chats "partner replied" box, so
# every gate (takeover, handover, agreed) and status rule is identical. The
# reply goes out through _send_and_log, i.e. really sent only when the
# whatsapp_live setting is on.
def _normalize_wa_phone(raw: str) -> str:
    return re.sub(r"[^\d]", "", raw or "").lstrip("0") if raw else ""


def _find_partner_by_phone(phone_digits: str) -> int | None:
    """Matches an inbound sender to a partner on the last 9 digits of phone,
    phone_2 or phone_prev (the number we switched away from). If the same
    number belongs to several rows, the most recently active open chat wins."""
    if len(phone_digits) < 9:
        return None
    tail = phone_digits[-9:]
    with db.conn() as c:
        rows = c.execute(
            "SELECT id, phone, phone_2, phone_prev, status FROM partners WHERE status != 'removed' "
            "AND (phone IS NOT NULL OR phone_2 IS NOT NULL OR phone_prev IS NOT NULL) "
            "ORDER BY CASE WHEN status IN ('agreed','not_agreed') THEN 1 ELSE 0 END, "
            "COALESCE(last_message_at, created_at) DESC, id DESC"
        ).fetchall()
    for r in rows:
        for stored in (r["phone"], r["phone_2"], r["phone_prev"]):
            if stored and _normalize_wa_phone(stored)[-9:] == tail:
                return r["id"]
    return None


@app.route("/webhook/whatsapp", methods=["GET"])
def api_whatsapp_webhook_verify():
    """Meta's verification handshake when the callback URL is registered:
    echo hub.challenge only if hub.verify_token matches WA_WEBHOOK_VERIFY_TOKEN."""
    verify_token = _project_env().get("WA_WEBHOOK_VERIFY_TOKEN") or ""
    mode = request.args.get("hub.mode", "")
    token = request.args.get("hub.verify_token", "")
    challenge = request.args.get("hub.challenge", "")
    if mode == "subscribe" and verify_token and token == verify_token:
        return challenge, 200
    return "verification failed", 403


@app.route("/webhook/whatsapp", methods=["POST"])
def api_whatsapp_webhook_receive():
    """Real inbound WhatsApp traffic. Fails CLOSED: without META_APP_SECRET,
    or with a bad X-Hub-Signature-256, nothing is processed (403) — this URL
    is public, so an unsigned request could otherwise inject fake partner
    messages and trigger real replies.

    Returns 200 immediately; message handling runs in a background thread,
    because Meta re-delivers any webhook that is slow to answer. Each message
    id is recorded first, so a re-delivery is ignored."""
    import hashlib
    import hmac
    cfg = _project_env()
    app_secret = cfg.get("META_APP_SECRET") or ""
    if not app_secret:
        print("[whatsapp webhook] META_APP_SECRET not set — request refused", file=sys.stderr)
        return jsonify({"ok": False, "error": "webhook signature secret not configured"}), 403
    raw_body = request.get_data()
    signature = request.headers.get("X-Hub-Signature-256", "")
    expected = "sha256=" + hmac.new(app_secret.encode(), raw_body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        return jsonify({"ok": False, "error": "invalid signature"}), 403

    try:
        payload = json.loads(raw_body or b"{}")
    except ValueError:
        return jsonify({"ok": True}), 200
    try:
        for entry in payload.get("entry") or []:
            for change in entry.get("changes") or []:
                value = change.get("value") or {}
                for st in value.get("statuses") or []:
                    _apply_wa_status(st)
                for msg in value.get("messages") or []:
                    msg_id = msg.get("id")
                    if msg_id:
                        with db.conn() as c:
                            fresh = c.execute("INSERT OR IGNORE INTO wa_inbound (msg_id, received_at) VALUES (?,?)",
                                              (msg_id, db.now())).rowcount
                        if not fresh:
                            continue  # Meta re-delivery — already handled
                    threading.Thread(target=_handle_inbound_whatsapp_message, args=(msg, value), daemon=True).start()
    except Exception as e:
        print(f"[whatsapp webhook] error processing payload: {e}", file=sys.stderr)
    return jsonify({"ok": True}), 200


def _apply_wa_status(st: dict) -> None:
    """Delivery receipts: sent → delivered → read, or failed with Meta's reason."""
    wa_id, status = st.get("id"), st.get("status")
    if not wa_id or status not in ("sent", "delivered", "read", "failed"):
        return
    err = None
    if status == "failed":
        errs = st.get("errors") or [{}]
        err = f"{errs[0].get('code')}: {errs[0].get('title') or errs[0].get('message') or 'delivery failed'}"
    rank = {"sent": 1, "delivered": 2, "read": 3, "failed": 4}
    with db.conn() as c:
        row = c.execute("SELECT id, partner_id, send_status FROM messages WHERE wa_message_id = ?", (wa_id,)).fetchone()
        if not row or rank.get(row["send_status"] or "", 0) >= rank[status]:
            return
        c.execute("UPDATE messages SET send_status = ?, send_error = COALESCE(?, send_error) WHERE id = ?",
                  (status, err, row["id"]))
    if status == "failed":
        _log_message(row["partner_id"], "system", f"WhatsApp reported a message as NOT delivered — {err}",
                     simulated=True)


_INBOUND_DEBOUNCE_SECONDS = 4
_ANSWERED_UP_TO: dict[int, int] = {}  # partner_id -> highest partner message id already answered
_UNKNOWN_SENDER_ALERTED: dict[str, float] = {}

_MEDIA_TYPES = {"image", "document", "audio", "video", "sticker", "location", "contacts"}


def _inbound_text(msg: dict) -> str | None:
    """Readable text for one inbound message, or None if it should be ignored
    entirely (reactions, read-only system events, unsupported types)."""
    t = msg.get("type")
    if t == "text":
        return ((msg.get("text") or {}).get("body") or "").strip() or None
    if t == "button":
        return ((msg.get("button") or {}).get("text") or "").strip() or None
    if t == "interactive":
        inter = msg.get("interactive") or {}
        picked = inter.get("button_reply") or inter.get("list_reply") or {}
        return (picked.get("title") or "").strip() or None
    if t in _MEDIA_TYPES:
        caption = ((msg.get(t) or {}).get("caption") or "").strip()
        note = f"[Partner sent a {t} — PC Agent can't open it yet; ask them to describe it in text if needed]"
        return f"{caption}\n{note}" if caption else note
    return None  # reaction, system, unsupported, etc.


def _handle_inbound_whatsapp_message(msg: dict, value: dict) -> None:
    """One real inbound message: match → log → (after a short debounce, under
    the partner's lock) reply once for everything not yet answered."""
    try:
        text = _inbound_text(msg)
        if not text:
            return
        from_number = msg.get("from") or ""
        partner_id = _find_partner_by_phone(_normalize_wa_phone(from_number))
        if not partner_id:
            last = _UNKNOWN_SENDER_ALERTED.get(from_number, 0)
            if time.time() - last > 3600:
                _UNKNOWN_SENDER_ALERTED[from_number] = time.time()
                name = (((value.get("contacts") or [{}])[0].get("profile") or {}).get("name")) or "unknown"
                _notify_pc_team(f"PC Agent: WhatsApp from an unknown number ({from_number})",
                                f"A message arrived from +{from_number} (WhatsApp name: {name}), which doesn't "
                                f"match any partner, so PC Agent did not reply.\n\nMessage:\n{text}\n")
            print(f"[whatsapp webhook] inbound from unknown number {from_number!r} — no matching partner",
                  file=sys.stderr)
            return

        my_msg_id = _record_partner_inbound(partner_id, text, simulated=False, wa_message_id=msg.get("id"))
        time.sleep(_INBOUND_DEBOUNCE_SECONDS)
        with _partner_lock(partner_id):
            if _ANSWERED_UP_TO.get(partner_id, 0) >= my_msg_id:
                return  # an earlier thread already replied to this message as part of a burst
            with db.conn() as c:
                newest = c.execute("SELECT MAX(id) AS m FROM messages WHERE partner_id = ? AND sender = 'partner'",
                                   (partner_id,)).fetchone()["m"] or my_msg_id
            _ANSWERED_UP_TO[partner_id] = newest
            _do_partner_auto_reply_locked(partner_id, opening=False)
    except Exception as e:
        print(f"[whatsapp webhook] failed handling inbound message: {e}", file=sys.stderr)


# --------------------------------------------------------------- attachments
# 2026-10-06, user: "make sure the kogul has the ability to send images,
# files, read images if recieved, also in chat stimulation and partner
# chats". Serving back what was uploaded/sent, plus managing the "shareables"
# library PC Agent can attach FROM (see attachments.py's module docstring
# for the three jobs this covers).

@app.route("/api/attachments/<name>")
def api_attachment_get(name):
    """Serves one stored attachment back out — used by the chat UI to show
    an uploaded image inline or offer a file for download. `name` is always
    the uuid-based stored filename (never the original), and
    attachments.path_of strips any path component, so this can't be used to
    read an arbitrary file off disk."""
    from flask import send_file
    p = attachments.path_of({"name": name})
    if not p.exists():
        return jsonify({"ok": False, "error": "not found"}), 404
    return send_file(p, mimetype=attachments.ALLOWED.get(p.suffix.lower(), "application/octet-stream"))


@app.route("/api/shareables")
def api_shareables_list():
    return jsonify({"ok": True, "items": attachments.list_shareables()})


@app.route("/api/shareables/add", methods=["POST"])
def api_shareables_add():
    fs = request.files.get("file")
    if not fs or not fs.filename:
        return jsonify({"ok": False, "error": "no file given"}), 400
    try:
        result = attachments.add_shareable(fs, request.form.get("description", ""))
    except attachments.AttachmentError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    return jsonify({"ok": True, **result})


@app.route("/api/shareables/<name>", methods=["DELETE"])
def api_shareables_delete(name):
    ok = attachments.delete_shareable(name)
    return jsonify({"ok": ok})


@app.route("/api/shareables/<name>/file")
def api_shareables_get(name):
    """Serves a library file — used so the Settings tab's shareables list can
    preview/download what's already in the library."""
    from flask import send_file
    p = attachments.LIB_DIR / Path(name).name
    if not p.exists() or p.name == "index.json":
        return jsonify({"ok": False, "error": "not found"}), 404
    return send_file(p, mimetype=attachments.ALLOWED.get(p.suffix.lower(), "application/octet-stream"))


# --------------------------------------------------------------- server restart
# 2026-10-06, user: "what about a restart button password prottected : 1111" —
# a self-contained way to pick up a code/template change without needing SSH
# access (the VPS deploy script's own restart step has proven flaky over SSH —
# Start-Process not reliably surviving the session). os.execv re-execs this
# same process image in place (same PID, same working directory, same argv),
# which works identically whether launched by a local double-click or by the
# VPS's background Start-Process — no external supervisor/Task Scheduler
# needed. The password is intentionally a simple fixed PIN, not a real auth
# system — this restarts a process, it doesn't expose any data.
_RESTART_PIN_LOCAL_DEFAULT = "1111"  # only accepted from this machine itself
_RESTART_FAILURES: list[float] = []


@app.route("/api/server/restart", methods=["POST"])
def api_server_restart():
    import hmac
    body = request.get_json(silent=True) or {}
    now = time.time()
    _RESTART_FAILURES[:] = [t for t in _RESTART_FAILURES if now - t < 600]
    if len(_RESTART_FAILURES) >= 5:
        return jsonify({"ok": False, "error": "too many wrong attempts — wait 10 minutes"}), 429
    pin = os.environ.get("KOGUL_RESTART_PIN") or _project_env().get("KOGUL_RESTART_PIN") or ""
    if not pin:
        if not _is_local_request():
            return jsonify({"ok": False, "error": "remote restart is disabled until KOGUL_RESTART_PIN is set in .env"}), 403
        pin = _RESTART_PIN_LOCAL_DEFAULT
    if not hmac.compare_digest(str(body.get("password") or "").encode(), pin.encode()):
        _RESTART_FAILURES.append(now)
        return jsonify({"ok": False, "error": "wrong password"}), 403

    def _delayed_restart():
        # give the HTTP response time to actually flush to the browser first.
        time.sleep(1.5)
        # 2026-10-06 fix #1: os.execv() looked like the obvious approach
        # (re-exec the same process image in place) but proved unreliable on
        # Windows — confirmed live: the process survived but stopped
        # accepting connections entirely (the dev server's listening socket
        # doesn't carry cleanly through CPython's execv emulation on
        # Windows, which is really spawnv+exit under the hood, not a true
        # in-place exec).
        # 2026-10-06 fix #2: spawning the replacement BEFORE exiting (to
        # avoid a gap) doesn't work either on a single port — both processes
        # would briefly fight over the same listening port, and Flask's dev
        # server doesn't bind with conflict-tolerant SO_REUSEADDR semantics
        # on Windows, so the new one would fail to bind while the old one is
        # still up. Exit first, then spawn — same brief-gap tradeoff the
        # existing SSH-based deploy restart script already has, just
        # triggered from inside the app instead of over SSH.
        import subprocess
        exe, args = sys.executable, list(sys.argv)
        cwd = os.getcwd()

        # spawn the respawn-watcher as an OS-level detached process, not
        # a thread of THIS process — a thread would die the instant this
        # process calls os._exit, before it ever got to sleep or Popen.
        watcher_script = (
            "import os, subprocess, sys, time; "
            f"time.sleep(0.5); "
            f"subprocess.Popen({[exe] + args!r}, cwd={cwd!r})"
        )
        subprocess.Popen(
            [exe, "-c", watcher_script],
            creationflags=(subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP)
            if sys.platform == "win32" else 0,
            close_fds=True,
        )
        os._exit(0)  # hard exit, not sys.exit() — skips Flask's shutdown machinery entirely

    threading.Thread(target=_delayed_restart, daemon=True).start()
    return jsonify({"ok": True, "restarting": True})


# ---------------------------------------------------- follow-up scheduler
# 2026-10-05/06, user: "now once a meta conversation window is started within
# 24 hours i gues from last message to 24 within send a follow up meesage
# maybe on 20th hour" + "if more than one number is available send first
# number no response after 24 send the second number". Agreed design (via
# AskUserQuestion): ONE thread per business, two-stage escalation —
#   hour 20: one polite same-number nudge (followup_stage 'none' -> 'nudged',
#            or 'nudge_skipped' — 2026-10-14: only when stalled midway, see _nudge_decision)
#   hour 24+: if a second number exists, "switch" to it — resend the opening
#             there and restart the clock (-> 'switched_number').
# 2026-10-13, user: "in progress - if its still chatting and if it falls
# under 3 day window, if 3 days no reply push it unresponsive" — clarified
# via AskUserQuestion: "no 20h nudge stays same, even after nudge if thres
# no reply after 3 dasy - then unresponsive". The 20h nudge is UNCHANGED;
# only the final "mark unresponsive" mark moved from 24h to 72h (3 days) —
# on a single-number partner this is simply a longer wait after the nudge;
# on a two-number partner, 24h triggers the switch-to-second-number
# escalation, and 72h measured from the switch is the final unresponsive
# mark. All timers run from the start of the unanswered wait (see
# _wait_anchor) — a nudge does not restart them. Messages go out through
# _send_and_log (real only in live mode). A partner whose `human_takeover`
# is on, or who has `needs_handover`, is left alone.
_FOLLOWUP_NUDGE_HOURS = 20  # after the PARTNER's last message, only while the 24h window is open
_FOLLOWUP_SWITCH_HOURS = 24
_FOLLOWUP_UNRESPONSIVE_HOURS = 72  # 3 days — user: "if 3 days no reply push it unresponsive"
_FOLLOWUP_POLL_SECONDS = 600

def _hours_since(timestamp: str) -> float:
    import datetime
    then = datetime.datetime.strptime(timestamp, "%Y-%m-%dT%H:%M:%S")
    return (datetime.datetime.now() - then).total_seconds() / 3600.0


def _nudge_decision(partner_name: str, conversation: list[dict]) -> dict:
    """2026-10-14: whether this stalled chat gets a nudge, and its context-
    specific text (brain.build_nudge_prompt). No fixed fallback line any more:
    if the AI call fails or answers unreadably, NO nudge is sent — a generic
    or wrong nudge reaches a real business, a missed one only costs a
    follow-up. {ok, nudge, reason, message}."""
    try:
        raw = run_openai_chat_text(brain.build_nudge_prompt(partner_name, conversation), model="gpt-4o",
                                   timeout=45, caller="kogul.followup_nudge")
    except Exception as e:
        return {"ok": False, "nudge": False, "reason": f"AI call failed: {str(e)[:150]}", "message": ""}
    return brain.parse_nudge_output(raw)


def _wait_anchor(partner_id: int, stored_anchor: str | None) -> str | None:
    """When the current unanswered wait began: the first PC Agent message
    after the partner's (or a human's) last message. Nudges don't move it,
    so 20h / 24h / 72h are all measured from the same starting point. After a
    number switch the switch time is stored and used instead."""
    if stored_anchor:
        return stored_anchor
    with db.conn() as c:
        last_in = c.execute("SELECT MAX(id) AS m FROM messages WHERE partner_id = ? AND sender IN ('partner','human')",
                            (partner_id,)).fetchone()["m"] or 0
        row = c.execute("SELECT created_at FROM messages WHERE partner_id = ? AND sender = 'kogul' AND id > ? "
                        "ORDER BY id LIMIT 1", (partner_id, last_in)).fetchone()
    return row["created_at"] if row else None


def _run_followup_check() -> None:
    """One pass over every chat where PC Agent is waiting on the partner.
    Skips chats a human owns, chats flagged for a human, and chats whose last
    PC Agent message failed to send (the partner never got it, so there is
    nothing to time out)."""
    with db.conn() as c:
        candidates = c.execute(
            """SELECT p.id, p.name, p.phone, p.phone_2, p.followup_stage, p.followup_anchor_at
               FROM partners p
               WHERE p.status IN ('contacted','replied','negotiating')
                 AND p.human_takeover = 0 AND p.needs_handover = 0
                 AND p.followup_stage != 'done'"""
        ).fetchall()

    for row in candidates:
        pid = row["id"]
        with db.conn() as c:
            last_msg = c.execute(
                "SELECT sender, send_status FROM messages WHERE partner_id = ? AND sender != 'system' "
                "ORDER BY id DESC LIMIT 1", (pid,)
            ).fetchone()
        if not last_msg or last_msg["sender"] != "kogul":
            continue  # the partner (or a human) spoke last — not our wait to time out
        if last_msg["send_status"] == "failed":
            continue
        anchor = _wait_anchor(pid, row["followup_anchor_at"])
        if not anchor:
            continue
        try:
            elapsed = _hours_since(anchor)
        except ValueError:
            continue

        stage = row["followup_stage"]
        # 2026-10-14, user: "20 hours: a polite nudge - only if the
        # conversation window is about to end". Measured from the PARTNER's
        # last message (that's what opens WhatsApp's 24h window), not from
        # our wait anchor: no nudge on cold outreach they never answered,
        # and none once the window has closed (a missed tick never turns into
        # a paid cold template).
        since_partner = _hours_since_partner_wrote(pid)
        if (stage == "none" and since_partner is not None
                and _FOLLOWUP_NUDGE_HOURS <= since_partner < _WINDOW_SAFE_HOURS):
            with db.conn() as c:
                conv = [dict(m) for m in c.execute("SELECT * FROM messages WHERE partner_id = ? ORDER BY id",
                                                   (pid,)).fetchall()]
            # 2026-10-14: only if the chat stalled midway with no clear
            # direction or decision, and the text fits where it stopped.
            decision = _nudge_decision(row["name"], conv)
            if decision["nudge"]:
                with _partner_lock(pid):
                    _send_and_log(pid, "kogul", decision["message"])
                new_stage = "nudged"
            else:
                why = decision["reason"] or "no reason given"
                _log_message(pid, "system", f"No 20h nudge sent: {why}" if decision["ok"]
                             else f"No 20h nudge sent (could not decide safely): {why}", simulated=True)
                new_stage = "nudge_skipped"  # decided once per quiet spell; a partner reply resets to 'none'
            with db.conn() as c:
                c.execute("UPDATE partners SET followup_stage = ? WHERE id = ?", (new_stage, pid))
            continue
        if stage in ("none", "nudged", "nudge_skipped") and elapsed >= _FOLLOWUP_SWITCH_HOURS and row["phone_2"]:
            _log_message(
                pid, "system",
                f"No response after {_FOLLOWUP_SWITCH_HOURS}h on {row['phone'] or '(no number)'} — "
                f"switching outreach to second number {row['phone_2']}.",
                simulated=True,
            )
            with db.conn() as c:
                c.execute(
                    "UPDATE partners SET phone_prev = phone, phone = ?, phone_2 = NULL, "
                    "followup_stage = 'switched_number', followup_anchor_at = ? WHERE id = ?",
                    (row["phone_2"], db.now(), pid)
                )
            result, status = _do_partner_auto_reply(pid, opening=True)
            if not (status == 200 and result.get("auto_replied")):
                _log_message(pid, "system",
                             f"Opening message to second number failed: {result.get('error') or result.get('reason')}",
                             simulated=True)
            continue
        if elapsed >= _FOLLOWUP_UNRESPONSIVE_HOURS:
            with db.conn() as c:
                c.execute(
                    "UPDATE partners SET status = 'unresponsive', followup_stage = 'done' WHERE id = ?", (pid,)
                )
            _log_message(pid, "system", f"No reply for {_FOLLOWUP_UNRESPONSIVE_HOURS}h — marked unresponsive.",
                         simulated=True)


def _followup_scheduler_loop() -> None:
    while True:
        try:
            _run_followup_check()
        except Exception as e:
            print(f"[followup scheduler] error: {e}", file=sys.stderr)
        time.sleep(_FOLLOWUP_POLL_SECONDS)


if __name__ == "__main__":
    # 2026-10-06, user: "can u push the pc agent tot the vps" — the VPS runs
    # Kogul on port 5011 (5010 stays local-only, so both can run
    # independently without a port clash if ever on the same network).
    # Defaults to 5010 unchanged for every existing local launch.
    port = int(os.environ.get("KOGUL_PORT", "5010"))
    print(f"Kogul running at http://127.0.0.1:{port}")
    threading.Thread(target=_followup_scheduler_loop, daemon=True).start()
    # 2026-10-14: every pending business card gets its report before anyone reviews it.
    with db.conn() as _c:
        _queue_report_builds([r["id"] for r in _c.execute(
            "SELECT wc.id FROM weekly_categories wc LEFT JOIN card_reports r ON r.category_id = wc.id "
            "WHERE wc.source = 'brand_discovery' AND wc.review_status = 'pending_review' "
            "AND (r.category_id IS NULL OR r.profile_json IS NULL OR r.research_json IS NULL) ORDER BY wc.id").fetchall()])
    threading.Thread(target=_upgrade_existing_reports, daemon=True).start()
    # threaded=True is required, not optional — weekly/generate and
    # category/approve do live HTTP calls (kapruka.com, Business Scout) in
    # a background thread that can take minutes; without this flag Flask's
    # dev server serves ONE request at a time, so every other tab/poll
    # request queues up behind whatever's in flight and the whole app looks
    # hung (confirmed live 2026-09-30 — a generate job left one connection
    # ESTABLISHED with no other request completing until it finished).
    # KOGUL_HOST=127.0.0.1 when a reverse proxy (HTTPS) sits in front; the
    # default 0.0.0.0 keeps the current VPS setup reachable, and the access
    # guard above requires KOGUL_ADMIN_PASSWORD for any non-local request.
    app.run(host=os.environ.get("KOGUL_HOST", "0.0.0.0"), port=port, debug=False, threaded=True)
