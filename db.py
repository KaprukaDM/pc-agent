"""Kogul's own SQLite store — pipeline/chat state that nothing else in this
project tracks today (see memory: kogul-partner-onboarding-agent, "the real
gap"). Everything here is Kogul-owned; category/partner SOURCE data is read
read-only from the main project's data/ files and from app_ship.py's
/api/find_partners — never duplicated into this DB, only referenced by id.
"""
from __future__ import annotations
import json
import sqlite3
import time
from pathlib import Path
from contextlib import contextmanager

DB_PATH = Path(__file__).resolve().parent / "data" / "kogul.sqlite"
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS weekly_batches (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    week_start   TEXT NOT NULL,           -- YYYY-MM-DD, Monday of the week
    created_at   TEXT NOT NULL,
    num_categories INTEGER NOT NULL,
    partners_per_category INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS weekly_categories (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id      INTEGER NOT NULL REFERENCES weekly_batches(id),
    department    TEXT NOT NULL,
    concept       TEXT NOT NULL,
    concept_volume INTEGER,
    source        TEXT,                  -- daraz_new_category_gap | daraz_thin_catalog |
                                          -- gsc_thin_catalog | keyword_bank — see kogul/sourcing.py
    worth_pursuing_reason TEXT,          -- why the semantic gate passed this category — see kogul/worth_pursuing.py
    priority_score REAL,                 -- verdict_weight + demand_bonus + core-dept bonus — see _score_candidate
    opportunity_type TEXT,               -- 'new' | 'real' | NULL, from the bank verdict
    action_text   TEXT,
    source_ref    TEXT,                   -- e.g. "data/category_opportunities_bank/electronics.json#recaptcha"
    business_url  TEXT,                   -- brand_discovery only: the candidate's own homepage URL
    products_to_bring_in TEXT,            -- brand_discovery only: what we'd onboard from this business
    already_partner_flag TEXT,            -- brand_discovery only: uncertain partner-sitemap match slug, for human review
    found_via_web_search INTEGER DEFAULT 0, -- brand_discovery only: 1 if sourced via the web-search fallback
                                             -- (not yet human-vetted, unlike a BRAND_SEEDS.txt pick) — shown
                                             -- as a review flag on the card, see brand_discovery.py
    brand_images  TEXT,                   -- brand_discovery only: JSON array of image URLs from the site (og + product shots)
    social_links  TEXT,                   -- brand_discovery only: JSON array of IG/FB/TikTok profile links found on the site
    brand_phones  TEXT,                   -- brand_discovery only: JSON array of phone numbers (tel:, wa.me/WhatsApp links, visible text)
    brand_emails  TEXT,                   -- brand_discovery only: JSON array of emails (mailto: + visible text)
    brand_whatsapp TEXT,                  -- brand_discovery only: digits of a wa.me-proven WhatsApp number, if one was found
    ecommerce_platform TEXT,              -- brand_discovery only: 'shopify' | 'woocommerce' | NULL — see _detect_platform
    review_status TEXT NOT NULL DEFAULT 'pending_review',
        -- pending_review | approved | rejected — see memory: kogul-partner-onboarding-agent,
        -- "human review step before partners are sourced" (the bank's own verdict data has
        -- confirmed noise — cricket-match/brand-name rows ranking as top 'opportunity' by
        -- volume — so partners are never auto-sourced for a category a human hasn't approved)
    reviewed_at   TEXT,
    reject_reason TEXT,                   -- 2026-10-13, user: "when rejecting weekly outreach i want
                                           -- why it is getting rejected... so agent should learn from
                                           -- that when doing weekly outreach" — real human reason, fed
                                           -- back into brand_discovery.judge_brand_fit's prompt as
                                           -- learned rejection patterns (see JUDGE_FIT prompt + the
                                           -- recent-rejections helper near judge_brand_fit)
    -- Weekly Outreach metrics (computed once at generation time via kogul/metrics.py) --
    metrics_json  TEXT,               -- JSON blob: ads/gsc/daraz revenue proxy/live product count/verdict
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS partners (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    weekly_category_id INTEGER NOT NULL REFERENCES weekly_categories(id),
    name            TEXT NOT NULL,
    phone            TEXT,
    source          TEXT,                 -- Maps/Google Search/Facebook/Instagram
    address         TEXT,
    url             TEXT,
    wa_link         TEXT,
    is_landline     INTEGER DEFAULT 0,
    product_verified INTEGER DEFAULT 0,
    is_specialist   INTEGER DEFAULT 0,
    specialization_reason TEXT,
    status          TEXT NOT NULL DEFAULT 'new',
        -- new | contacted | replied | negotiating | agreed | not_agreed | unresponsive
    not_agreed_reason TEXT,
    human_takeover  INTEGER DEFAULT 0,
    needs_handover  INTEGER DEFAULT 0,
    pushed_to_pc_at TEXT,
    last_message_at TEXT,
    phone_2         TEXT,    -- second contact number, if the business gave/has more than one
                             -- (brand_discovery's brand_phones[1]) — used for the 24h follow-up
                             -- escalation: hour 24+ switches to this number if hour-20's nudge
                             -- on `phone` got no reply. NULL if only one number is known.
    followup_stage  TEXT NOT NULL DEFAULT 'none',
        -- none | nudged | switched_number | done — tracks where a stalled (no-reply) chat is in
        -- the 24h follow-up cycle so the scheduler never nudges or switches twice. Reset to
        -- 'none' whenever the partner actually replies (see api_partner_reply_from_partner).
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    partner_id  INTEGER NOT NULL REFERENCES partners(id),
    sender      TEXT NOT NULL,     -- 'kogul' | 'partner' | 'human' | 'system'
    body        TEXT NOT NULL,
    simulated   INTEGER DEFAULT 1,  -- 1 = not actually sent over WhatsApp yet
    created_at  TEXT NOT NULL,
    attachments TEXT                -- JSON list of {name, orig, mime, size, kind}; see attachments.py
);

CREATE TABLE IF NOT EXISTS kogul_chat (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    role       TEXT NOT NULL,   -- 'user' | 'kogul'
    body       TEXT NOT NULL,
    created_at TEXT NOT NULL,
    attachments TEXT
);

-- 2026-10-04, user: "and alos i need to be ble to chat with kogil like im
-- the partner to see how kogul responds" — a role-play sandbox, entirely
-- separate from the `partners`/`messages` tables (which represent REAL
-- sourced businesses only). This exists purely so a human can type as a
-- fictional prospective partner and see exactly how PC Agent would reply
-- (same brain.build_partner_reply_prompt logic, same brief, same OpenAI
-- call real outreach uses), with no risk of a test scenario ever being
-- mistaken for a real partner anywhere else in the app.
CREATE TABLE IF NOT EXISTS partner_simulations (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    business_name TEXT NOT NULL,
    category     TEXT NOT NULL,
    department   TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'new',
        -- new | contacted | agreed | not_agreed | unresponsive — 2026-10-05, user:
        -- "Make sure the chats status falls into one of these buckets" (the 5 real
        -- Dashboard tiles: Reached/Agreed & pushed to PC/Not agreed/Unresponsive/
        -- In progress). Deliberately reuses partners.status's EXACT vocabulary
        -- (see that column) instead of a parallel one invented for simulations
        -- only — "contacted" is the one bucket covering an ordinary still-going
        -- reply (dashboard's "In progress" groups contacted/replied/negotiating
        -- together, see api_dashboard). See app_kogul.py's _run_simulation_reply.
    needs_handover INTEGER DEFAULT 0,  -- same flag/meaning as partners.needs_handover — an
                                        -- overlay on top of status, not a status value itself
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS partner_simulation_messages (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    simulation_id  INTEGER NOT NULL REFERENCES partner_simulations(id),
    sender         TEXT NOT NULL,   -- 'kogul' | 'partner' (the human, role-playing)
    body           TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    attachments    TEXT
);

-- 2026-10-14, user: PC team tests via Simulate as Partner and "add rules or
-- define rules if any problem is detected that will be automatically
-- included". Active rows are appended to PC Agent's brief in every prompt
-- (brain.team_rules_block), read fresh each message like KOGUL.md.
CREATE TABLE IF NOT EXISTS agent_rules (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    rule           TEXT NOT NULL,       -- the instruction PC Agent must follow
    problem        TEXT,                -- what the tester saw go wrong
    flagged_reply  TEXT,                -- the PC Agent reply that was flagged, verbatim
    simulation_id  INTEGER,             -- the test scenario it came from (NULL = added directly)
    added_by       TEXT,
    active         INTEGER NOT NULL DEFAULT 1,
    created_at     TEXT NOT NULL,
    updated_at     TEXT
);

-- 2026-10-14: Partner report (kogul/partner_report.py). One cached row per
-- BUSINESS CARD (weekly_categories row) — user: "i want the partner report
-- before reaching out", so it exists from discovery on, before any partner
-- row/chat. Rebuilt when the chat has new messages. Each *_json is that
-- piece's own result so one failing piece never blanks the others.
-- (Replaced same-day partner-keyed tables partner_reports/partner_questions;
-- _migrate copies their rows across.)
CREATE TABLE IF NOT EXISTS card_reports (
    category_id    INTEGER PRIMARY KEY REFERENCES weekly_categories(id),
    catalog_json   TEXT,      -- store_catalog(): real SKU count + titles from their own store
    site_json      TEXT,      -- site_location_text(): address snippets from their own pages
    profile_json   TEXT,      -- extract_profile(): about, product types, location, answers, analysis
    kapruka_json   TEXT,      -- kapruka_check(): live Kapruka search per product type
    kapruka_terms  TEXT,      -- the product types kapruka_json was run for (re-run only when they change)
    error          TEXT,      -- last build error, shown on the report
    msg_count      INTEGER,   -- messages in the chat when profile_json was built (staleness check)
    built_at       TEXT
);

-- 2026-10-14, user chose "Per-partner questions": AI suggests questions specific
-- to one business, a human approves/rejects (before or after outreach); humans
-- can add their own (approved on creation). Only 'approved' rows are given to
-- PC Agent to ask.
CREATE TABLE IF NOT EXISTS card_questions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    category_id INTEGER NOT NULL REFERENCES weekly_categories(id),
    question    TEXT NOT NULL,
    why         TEXT,
    source      TEXT NOT NULL,                     -- 'ai' | 'human'
    status      TEXT NOT NULL DEFAULT 'suggested', -- suggested | approved | rejected
    added_by    TEXT,
    created_at  TEXT NOT NULL,
    decided_at  TEXT
);

CREATE TABLE IF NOT EXISTS whatsapp_test_sends (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    phone_number    TEXT NOT NULL,       -- E.164 digits, no '+' (Meta Cloud API convention)
    template_name   TEXT NOT NULL,       -- 'hello_world' until a real Kogul template is approved
    wa_message_id   TEXT,                -- Meta's own message id, for status webhook correlation later
    status          TEXT NOT NULL DEFAULT 'sent',
        -- sent | delivered | read | failed — 2026-10-05, user: "have the button - send a
        -- test whatsapp message... criteria is that we will open the conversation window
        -- by dropping the first message, all this is for testing purpose to monitor the
        -- status change as conversation builds". Starts 'sent' the moment the Cloud API
        -- accepts it; 'delivered'/'read'/'failed' need a status-webhook receiver to ever
        -- update automatically (not built yet — this table exists so that piece has
        -- somewhere real to write to, rather than inventing the schema again later).
    error_message   TEXT,                -- set when status='failed' (e.g. template not approved,
                                          -- number not opted in, 24h window rules)
    created_at      TEXT NOT NULL
);

-- Potential Product Opportunities tab (2026-10-07) — real, human-confirmed
-- "Kapruka has little/no stock for this" items. Originally a read-only pull
-- from the user's "Kapruka - New Opportunity" Google Sheet
-- (data/missing_items_seed.json, one-time import below in _migrate); this
-- table is now the editable source of truth so items can be added/removed
-- directly in Kogul, not just via the sheet. Feeds brand_discovery.py's
-- weekly search hints AND the tab's own on-demand "Find partner now".
CREATE TABLE IF NOT EXISTS missing_items (
    id            TEXT PRIMARY KEY,     -- sheet rows keep their original hex id; new rows get uuid4().hex
    missing_item  TEXT NOT NULL,
    example       TEXT,
    search_term   TEXT,                 -- used for the real Google Ads demand lookup, see ads_volume
    demand        TEXT,                 -- low | medium | high — the sheet's own human judgment call
    department    TEXT,
    summary       TEXT,
    url           TEXT,
    status        TEXT NOT NULL DEFAULT 'Pending',
    ads_volume    INTEGER,              -- real avg_monthly_searches from Google Ads Keyword Planner,
                                         -- NULL until first looked up (see api_missing_items_refresh_ads)
    ads_checked_at TEXT,                -- so a stale lookup is visible, never silently reused forever
    source        TEXT NOT NULL DEFAULT 'manual',  -- 'sheet' (original import) | 'manual' (added in-app)
    created_at    TEXT NOT NULL
);

-- Meta re-delivers a webhook it thinks failed; every inbound message id is
-- recorded here first (INSERT OR IGNORE) so a redelivery never gets a second reply.
CREATE TABLE IF NOT EXISTS wa_inbound (
    msg_id      TEXT PRIMARY KEY,
    received_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS token_usage (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    context     TEXT NOT NULL,   -- 'kogul_chat' | 'partner_reply:<partner_id>' | 'weekly_pick'
    model       TEXT NOT NULL,
    input_chars  INTEGER,
    output_chars INTEGER,
    created_at  TEXT NOT NULL
);
"""

DEFAULT_SETTINGS = {
    "num_categories_per_week": "10",
    "partners_per_category": "4",
    "claude_model": "claude-sonnet-5",
    # 2026-10-06, user: "have chat model setting drop down with ai models" —
    # originally applied to all 3 conversational call sites via OpenAI, then
    # same day, user: "no no chat with pc agent will be claude cli based,
    # stimulate as partner only openai, chat replies and outreach messages
    # use OpenAI (gpt-4o) directly, not this" — split back apart. This
    # setting now controls ONLY the Chat with PC Agent tab, which runs on
    # the Claude CLI (see claude_cli.run_claude_cli); real partner replies
    # and Simulate as Partner stay hardcoded to OpenAI gpt-4o, unaffected by
    # this setting.
    "chat_model": "claude-sonnet-5",
    # 2026-10-06, user: "have a cost tab monitoring template cost - meta its
    # a marketing and open ai token cost". Meta's WhatsApp marketing-template
    # conversation rate varies by country/time — user-provided real rate,
    # not a guessed/hardcoded figure (never invent a $ number not given).
    "wa_template_cost_usd": "0.08784",
    "follow_up_days": "3",
    "pc_team_email": "",
    # 2026-10-14, user: "i want in settings to Partner Central team email and
    # number to fill so notification will go ino theirs we can add upto 3
    # email and 3 numbers" — the real notification recipients going forward
    # (human-intervention + partner-interested alerts). Stored as JSON arrays
    # (up to 3 entries each), read fresh by _notify_pc_team /
    # _notify_pc_team_whatsapp on every send rather than the old hardcoded
    # _NOTIFY_RECIPIENTS constant. pc_team_email (singular, above) is now
    # legacy/unused by the notification path — kept only so an existing
    # saved value isn't silently lost; the UI no longer shows it as its own
    # field, superseded by pc_team_emails.
    "pc_team_emails": "[]",
    "pc_team_phones": "[]",
    # "1" = partner-facing messages really go out over the WhatsApp Cloud API.
    # "0" (default) = they are only logged and shown as "simulated — not sent".
    "whatsapp_live": "0",
    # Approved Meta template used whenever we message a partner outside the
    # 24h customer-service window (cold opener, nudge, second-number opener) —
    # free-form text is rejected there (error 131047). Empty = such sends fail
    # visibly instead of pretending to succeed.
    "wa_outreach_template": "",
    # 2026-10-14: team alerts (hot lead / needs a human) to the PC team's own
    # WhatsApp. Free-form text only reaches a number that messaged the business
    # in the last 24h (a template does NOT open that window — the old
    # "hello_world then text" path delivered only hello_world). An approved
    # UTILITY template with one body variable, e.g. "PC Agent alert: {{1}}",
    # carries the whole alert. Empty = text only (works inside the 24h window).
    "wa_alert_template": "",
    "wa_alert_template_lang": "en_US",
    # 2026-10-14, user: once a partner is interested, PC Agent "requests a brief
    # Business information like whats the products they sell, How many products
    # they have, Location of their buisness". One question per message, in order;
    # per-partner approved questions (partner_questions) are asked after these.
    "brief_questions": json.dumps([
        "What products do you sell?",
        "Roughly how many individual products (SKUs) do you have? Count each item, not categories.",
        "What is your business location?",
        "Only if they are outside Colombo: are you OK with consignment? (You keep some stock at Kapruka's warehouse in Mirihana and we pack and deliver when an order comes.)",
    ]),
    "wa_outreach_template_lang": "en_US",
    "wa_outreach_template_name": "",  # "1" = template has one variable, filled with the business name
    # 2026-10-04, user-authored opening message — went through 5 rounds:
    # (1) "Increase your sales by increasing the visibility among
    # international audience..." (2-para, too long), (2) shortened but
    # self-intro-first, (3) user's literal wording ("Hi, Im Kogul from
    # Kapruka - Partner Central / are you interested in joining our
    # platform...") plus a tacked-on "We think X could be a great fit for
    # our Y category" sentence, (4) user: "i feel like tis lenghty can u
    # shorten and make not look like an ai is chatting" — the tacked-on
    # sentence was exactly the kind of templated-AI phrasing that gave it
    # away ("We think X could be a great fit for Y" is not how a real
    # person opens a WhatsApp message). Cut to one line, name dropped in
    # naturally instead of in its own sentence, category mentioned only
    # once and in passing rather than as a formal category-fit claim.
    # (5) user: "remove em dash" — the em dash was itself another AI tell;
    # a real person texting on WhatsApp uses a period or comma, not —.
    # (6) 2026-10-06, user-provided replacement wording (pasted verbatim,
    # "kogul intro") — supersedes the above iteration; kept as a multi-line
    # WhatsApp-style message (greeting, context, soft CTA) rather than the
    # single-line version.
    # (7) 2026-10-14, user-provided replacement ("kogul new intro message"), pasted verbatim;
    # a second, shorter version followed the same day and is the current one.
    "opening_message_template": (
        "Hi {business_name} \U0001F44B I'm Kogul from Kapruka Partner Central.\n\n"
        "Would you like to become a Kapruka Partner and boost your sales and earnings?"
    ),
}


@contextmanager
def conn():
    c = sqlite3.connect(DB_PATH, timeout=15)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    c.execute("PRAGMA busy_timeout = 15000")
    try:
        yield c
        c.commit()
    finally:
        c.close()


def _migrate(c: sqlite3.Connection) -> None:
    """CREATE TABLE IF NOT EXISTS never retroactively adds a column to an
    existing table — a live kogul.sqlite created before the 'source'
    column existed would otherwise silently keep missing it forever, no
    error, just an absent field (confirmed live 2026-09-30: exactly this
    happened — a real weekly batch generated cleanly with sourcing.py's
    new multi-source data, but 'source' was nowhere in the row because
    only the .py schema string had been updated, not the already-created
    file on disk)."""
    cols = {r["name"] for r in c.execute("PRAGMA table_info(weekly_categories)")}
    if "source" not in cols:
        c.execute("ALTER TABLE weekly_categories ADD COLUMN source TEXT")
    if "worth_pursuing_reason" not in cols:
        c.execute("ALTER TABLE weekly_categories ADD COLUMN worth_pursuing_reason TEXT")
    if "priority_score" not in cols:
        c.execute("ALTER TABLE weekly_categories ADD COLUMN priority_score REAL")
    if "business_url" not in cols:
        c.execute("ALTER TABLE weekly_categories ADD COLUMN business_url TEXT")
    if "products_to_bring_in" not in cols:
        c.execute("ALTER TABLE weekly_categories ADD COLUMN products_to_bring_in TEXT")
    if "already_partner_flag" not in cols:
        c.execute("ALTER TABLE weekly_categories ADD COLUMN already_partner_flag TEXT")
    if "found_via_web_search" not in cols:
        c.execute("ALTER TABLE weekly_categories ADD COLUMN found_via_web_search INTEGER DEFAULT 0")
    if "brand_images" not in cols:
        c.execute("ALTER TABLE weekly_categories ADD COLUMN brand_images TEXT")
    if "social_links" not in cols:
        c.execute("ALTER TABLE weekly_categories ADD COLUMN social_links TEXT")
    if "brand_phones" not in cols:
        c.execute("ALTER TABLE weekly_categories ADD COLUMN brand_phones TEXT")
    if "brand_emails" not in cols:
        c.execute("ALTER TABLE weekly_categories ADD COLUMN brand_emails TEXT")
    if "brand_whatsapp" not in cols:
        c.execute("ALTER TABLE weekly_categories ADD COLUMN brand_whatsapp TEXT")
    if "discovery_source" not in cols:
        # 2026-10-03, user: "i dont see brand discovery brands based on daraz
        # kapruka verdict data - opportunity tool" — found_via_web_search
        # alone can't answer that (it's True for EITHER search stream);
        # this records WHICH stream actually found the business: "open"
        # (unconstrained self-discovery), "gap:<department>" (the
        # per-department search sharpened by real Daraz/Kapruka verdict
        # gaps), or "seed" (BRAND_SEEDS.txt). See brand_discovery.py.
        c.execute("ALTER TABLE weekly_categories ADD COLUMN discovery_source TEXT")
    if "ecommerce_platform" not in cols:
        # 2026-10-05, user: "check if its shopify or woo commer... so if its
        # shoppy integration will very easy within few clicks u can get ur
        # products listen on kapruka - add that". "shopify" / "woocommerce" /
        # NULL (unknown/neither) — see brand_discovery.py's _detect_platform.
        c.execute("ALTER TABLE weekly_categories ADD COLUMN ecommerce_platform TEXT")
    if "reject_reason" not in cols:
        # 2026-10-13, user: "when rejecting weekly outreach i want why it is
        # getting rjected... so agent should learn from that when doing
        # weekly outreach" — see the column comment in CREATE TABLE above.
        c.execute("ALTER TABLE weekly_categories ADD COLUMN reject_reason TEXT")
    if "followup_id" not in cols:
        # 2026-10-17, user: "Push to human follow-up" sends the business to the PC Follow-up tracker
        c.execute("ALTER TABLE weekly_categories ADD COLUMN followup_id INTEGER")
        c.execute("ALTER TABLE weekly_categories ADD COLUMN followup_pushed_at TEXT")
    if "rejected_by" not in cols:
        # 2026-10-15, user: team rejections and Kogul's own pre-screen rejections both show on the
        # Dashboard's By-department table. NULL on old rows = the team (only humans could reject before).
        c.execute("ALTER TABLE weekly_categories ADD COLUMN rejected_by TEXT")

    pcols = {r["name"] for r in c.execute("PRAGMA table_info(partners)")}
    if "is_specialist" not in pcols:
        c.execute("ALTER TABLE partners ADD COLUMN is_specialist INTEGER DEFAULT 0")
    if "specialization_reason" not in pcols:
        c.execute("ALTER TABLE partners ADD COLUMN specialization_reason TEXT")
    if "phone_2" not in pcols:
        # 2026-10-06, user: "if more than one number is available send first
        # number no response after 24 send the second number" — see the
        # 24h follow-up scheduler in app_kogul.py.
        c.execute("ALTER TABLE partners ADD COLUMN phone_2 TEXT")
    if "followup_stage" not in pcols:
        c.execute("ALTER TABLE partners ADD COLUMN followup_stage TEXT NOT NULL DEFAULT 'none'")
    if "phone_prev" not in pcols:
        # the number we switched AWAY from — still matched for inbound replies
        c.execute("ALTER TABLE partners ADD COLUMN phone_prev TEXT")
    if "followup_anchor_at" not in pcols:
        # when the current unanswered wait started; nudges don't reset it
        c.execute("ALTER TABLE partners ADD COLUMN followup_anchor_at TEXT")

    # 2026-10-14: the report moved from per-partner to per-card (user wants it
    # before outreach). Copy any rows from the same-day partner-keyed tables.
    old = {r["name"] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "partner_questions" in old:
        c.execute("""INSERT INTO card_questions (category_id, question, why, source, status, added_by, created_at, decided_at)
                     SELECT p.weekly_category_id, q.question, q.why, q.source, q.status, q.added_by, q.created_at, q.decided_at
                     FROM partner_questions q JOIN partners p ON p.id = q.partner_id""")
        c.execute("DROP TABLE partner_questions")
    if "partner_reports" in old:
        c.execute("""INSERT OR IGNORE INTO card_reports (category_id, catalog_json, profile_json, kapruka_json,
                       kapruka_terms, error, msg_count, built_at)
                     SELECT p.weekly_category_id, r.catalog_json, r.profile_json, r.kapruka_json, r.kapruka_terms,
                            r.error, r.msg_count, r.built_at
                     FROM partner_reports r JOIN partners p ON p.id = r.partner_id""")
        c.execute("DROP TABLE partner_reports")

    # 2026-10-14: web research (Google location fallback + brand value), and the
    # call-booking CTA ("arrange a call with the team", 3 slots, 11-4).
    rcols = {r["name"] for r in c.execute("PRAGMA table_info(card_reports)")}
    if "research_json" not in rcols:
        c.execute("ALTER TABLE card_reports ADD COLUMN research_json TEXT")
    for table in ("partners", "partner_simulations"):
        tcols = {r["name"] for r in c.execute(f"PRAGMA table_info({table})")}
        for col in ("call_slot", "call_contact_name", "call_booked_at"):
            if col not in tcols:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {col} TEXT")

    mcols = {r["name"] for r in c.execute("PRAGMA table_info(messages)")}
    if "send_status" not in mcols:
        # simulated | sent | delivered | read | failed
        c.execute("ALTER TABLE messages ADD COLUMN send_status TEXT")
        c.execute("UPDATE messages SET send_status = CASE WHEN simulated = 1 THEN 'simulated' ELSE 'sent' END")
    if "wa_message_id" not in mcols:
        c.execute("ALTER TABLE messages ADD COLUMN wa_message_id TEXT")
    if "send_error" not in mcols:
        c.execute("ALTER TABLE messages ADD COLUMN send_error TEXT")

    # 2026-10-06, user: "make sure the kogul has the ability to send images, files, read images
    # if received, also in chat simulation and partner chats" — one JSON column per chat table.
    for table in ("messages", "kogul_chat", "partner_simulation_messages"):
        tcols = {r["name"] for r in c.execute(f"PRAGMA table_info({table})")}
        if "attachments" not in tcols:
            c.execute(f"ALTER TABLE {table} ADD COLUMN attachments TEXT")

    simcols = {r["name"] for r in c.execute("PRAGMA table_info(partner_simulations)")}
    if "status" not in simcols:
        # 2026-10-05, user: "when partner stimulate also i need the status on
        # which status the each conversation is in" — scenarios created
        # before this column existed default to 'new' here, then get
        # corrected to a real value the next time a reply runs in them.
        c.execute("ALTER TABLE partner_simulations ADD COLUMN status TEXT NOT NULL DEFAULT 'new'")
    if "needs_handover" not in simcols:
        c.execute("ALTER TABLE partner_simulations ADD COLUMN needs_handover INTEGER DEFAULT 0")
    if "not_agreed_reason" not in simcols:
        c.execute("ALTER TABLE partner_simulations ADD COLUMN not_agreed_reason TEXT")
    # 2026-10-05, user: "Make sure the chats status falls into one of these
    # buckets" — the status column briefly used its own vocabulary
    # (not_started/in_progress/handover/error) before being aligned to
    # partners.status's real vocabulary (new/contacted/agreed/not_agreed/
    # unresponsive); this one-time rename corrects any rows already written
    # under the old names so old test data doesn't silently fall outside
    # every dashboard bucket.
    c.execute("UPDATE partner_simulations SET status = 'new' WHERE status = 'not_started'")
    c.execute("UPDATE partner_simulations SET status = 'contacted' WHERE status = 'in_progress'")
    c.execute("UPDATE partner_simulations SET needs_handover = 1, status = 'contacted' WHERE status = 'handover'")
    c.execute("UPDATE partner_simulations SET status = 'contacted' WHERE status = 'error'")

    # One-time import of the original Google Sheet pull (data/missing_items_seed.json)
    # into the new missing_items table — only runs while the table is still
    # empty, so it never re-adds a row a human has since removed, and never
    # duplicates on every app start.
    row_count = c.execute("SELECT COUNT(*) AS n FROM missing_items").fetchone()["n"]
    if row_count == 0:
        seed_path = Path(__file__).resolve().parent / "data" / "missing_items_seed.json"
        if seed_path.exists():
            try:
                seed = json.loads(seed_path.read_text(encoding="utf-8"))
                for r in seed.get("rows", []):
                    c.execute(
                        "INSERT OR IGNORE INTO missing_items (id, missing_item, example, search_term, "
                        "demand, department, summary, url, status, source, created_at) "
                        "VALUES (?,?,?,?,?,?,?,?,?,'sheet',?)",
                        (r.get("id"), r.get("missing_item"), r.get("example"), r.get("search_term"),
                         r.get("demand"), r.get("department"), r.get("summary"), r.get("url"),
                         r.get("status") or "Pending", now())
                    )
            except Exception:
                pass  # fail-open: a corrupt/missing seed file just means an empty table, not a crash


def init_db() -> None:
    with conn() as c:
        # WAL lets the scheduler, webhook worker and request threads read while one writes.
        c.execute("PRAGMA journal_mode = WAL")
        c.executescript(SCHEMA)
        _migrate(c)
        for k, v in DEFAULT_SETTINGS.items():
            c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (k, v))


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def get_settings() -> dict:
    with conn() as c:
        rows = c.execute("SELECT key, value FROM settings").fetchall()
    return {r["key"]: r["value"] for r in rows}


def set_setting(key: str, value: str) -> None:
    with conn() as c:
        c.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
