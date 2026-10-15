# Kogul — Change & Suggestion Checklist

Every change made and every suggestion raised (by the user or found during
work), tracked here so nothing gets lost across sessions. Check an item off
`[x]` only once it's actually done and verified working — not just coded.

## Build 1 — core app (2026-09-30)

- [x] `kogul/` standalone Flask app scaffolded (own DB, own port 5010)
- [x] KOGUL.md editable brief, live-reloaded on every Kogul message
- [x] 6 tabs: Dashboard, Weekly Outreach, Chat with Kogul, Partner Chats, Kogul Settings, Partner Central Settings
- [x] Kogul runs on the Claude CLI (`claude -p --model`), never a direct API call
- [x] Categories + partners sourced from existing Opportunity Tool data (not independent research)
- [x] WhatsApp simulated (logged, not sent) — real send/webhook explicitly deferred
- [x] Settings changes write straight to DB/file, read fresh on next use — no in-memory caching
- [x] Human approve/reject gate before partners are sourced for a category (found: bank data has real noise — cricket matches, brand names)
- [x] App launched locally at http://127.0.0.1:5010

## Build 2 — Weekly Outreach metrics + partner dropdown (2026-09-30)

- [x] Per-category metrics: Google Ads demand, GSC impressions, Daraz revenue proxy (overseas-excluded), live Kapruka product count
- [x] Daraz revenue proxy joined via best-effort keyword match (bank concept has no direct key into daraz_categories)
- [x] Live Kapruka check seeded from Daraz's own product names, auto-run at category-generation time
- [x] Fixed: live check falsely reported "covered" (11 unrelated party-decor products) for wallpaper — root cause was smart_live_search's last-word fallback on a noisy Daraz title; fixed by anchoring on the concept and re-filtering any secondary-probe hit against the concept's own words
- [x] Partner dropdown per category: remove a partner, auto-refill from a fresh Find Partners call excluding all previously-seen names/phones

## Fixes (2026-09-30)

- [x] Fixed: tabs stopped responding entirely — a double-escaped `\\'` in two JS strings threw a SyntaxError that halted the whole script before tab-click listeners attached. Fixed both strings, restarted the app (Flask caches compiled templates in memory, a refresh alone wasn't enough).
- [x] Fixed: app appeared to hang / not respond — Flask dev server defaulted to single-threaded, so a slow background job (live category generation) blocked every other request. Added `threaded=True` to `app.run()`.
- [x] **Switching tabs cancels an in-flight Chat with Kogul request** — root cause: the "Kogul is thinking..." placeholder was a DOM node captured locally inside `sendKoguChat()`; switching to the Chat with Kogul tab again re-rendered `#koguChatMsgs` from scratch (`loadKoguChat`), orphaning that node — the backend job kept running fine, but the answer had nowhere valid to land once it resolved, so it looked cancelled even though it wasn't. Fixed by making `/api/kogul_chat/history` (already persisted server-side) the single source of truth: a pending request is tracked page-level (`koguChatPendingJob`, not tab-scoped), rendered as a synthetic last row on every re-render, and cleared once the real turn is confirmed in history. Verified live: started a chat job, deliberately never polled it from the client, waited for it to finish server-side, and confirmed the Q&A turn appeared correctly in `/api/kogul_chat/history` with no client-side polling involved — proves the answer survives regardless of tab state.

## Sourcing rework (2026-09-30, in progress)

Per user: "start with source GSC, Daraz and from expand catalog and new
category opportunities" — replacing the keyword-bank-only category source
(confirmed noisy: cricket matches, brand names) with the same real sources
the main Opportunity Tool's own tabs use.

- [x] `kogul/sourcing.py` rewritten: 3 new real sources added —
      `source_gsc_thin_catalog` (Expand Catalog's GSC sub-tab logic, via
      `category_health.py`), `source_daraz_thin_catalog` (Expand Catalog's
      Daraz sub-tab — page exists, thin vs Daraz demand),
      `source_daraz_new_category_gap` (New Category Opportunities' Daraz
      sub-tab — `status='category_gap'`, real named category + real
      revenue proxy, no fuzzy join needed). Keyword-bank kept as 4th,
      lowest-priority fallback only when the other three don't fill quota.
- [x] Verified live: `source_daraz_new_category_gap` and
      `source_daraz_thin_catalog` return real, clean, named categories
      (Mens Digital Watches, Coffee Grinders, Ladders & Workbenches — not
      noise) with real revenue proxy figures.
- [x] Verified live: `source_gsc_thin_catalog` returns real GSC-demand rows,
      but surfaced its OWN data-quality issue — top results include
      "beer price in sri lanka", "kfc sri lanka", "liquor price in sri
      lanka" (informational/non-onboardable queries, same noise class as
      the keyword-bank cricket-match problem, just from GSC instead of
      Ads). Flagged, not yet fixed — the existing human approve/reject
      gate in Weekly Outreach still catches these before any partner
      outreach happens, so this isn't unsafe, just not yet filtered
      upstream.
- [x] Wire the rewritten `sourcing.top_category_opportunities()` into
      `app_kogul.py`'s `_run_weekly_generate` — call-site signature was
      already compatible (unchanged `(limit, exclude_keys)`), no app_kogul.py
      change needed. Found and fixed a real bug before it could crash a
      real run: `source_gsc_thin_catalog` returned `department: None`
      (GSC rows don't carry a bank-style department slug), but
      `weekly_categories.department` is `NOT NULL` in the schema — would
      have thrown a SQLite IntegrityError on every GSC-sourced row's
      insert. Fixed by deriving the department from the row's own `path`
      field (e.g. "Cakes > Caravan Fresh" → "Cakes"). Also found the same
      risk was real, not just theoretical, for the Daraz sources: checked
      `daraz_categories` directly and found 1 row in each of the
      category_gap and thin-catalog-eligible sets with a NULL
      `kapruka_department` — added an `or "other"` fallback to both.
- [x] Decide whether to add a GSC-side noise filter — user chose to add
      one now (not rely on the review gate alone). Implementing:
      `_looks_onboardable_gsc()` in `sourcing.py`, reusing the project's
      existing informational-query vocabulary (price/near-me/brand-chain
      patterns already identified in `kapruka_live_verify.py`'s
      `_GENERIC_SEARCH_MODIFIERS` and known competitor/restaurant-chain
      noise from `keyword-bank-off-topic-gate` memory) rather than
      inventing a new list from scratch.
- [x] Full end-to-end test: real `/api/weekly/generate` call using the new
      combined sourcing — completed successfully, no crash (confirms the
      department-NULL fix holds under real load). Result: 10/10 categories
      came from `daraz_new_category_gap` (the strongest, cleanest source —
      correct, since it's tried first and had enough supply to fill the
      week's quota) — real named categories (Serum & Essence, Coffee
      Grinders, Ladders & Workbenches, Portable Audio Microphones), ZERO
      cricket-match/brand-name noise, a clear improvement over the old
      bank-only source.
- [x] GSC noise filter (`_looks_onboardable_gsc`) implemented, reusing
      real vocabulary from `build_keyword_bank_big.py`'s `_FOOD_CHAINS`/
      `_FOOD_SERVICE` (memory: keyword-bank-off-topic-gate) and
      `kapruka_live_verify.py`'s price/near-me modifier phrases. Verified
      live: "beer price in sri lanka", "kfc sri lanka" no longer appear in
      `source_gsc_thin_catalog`'s output. Honest caveat found during
      testing and not silently glossed over: some remaining rows are still
      questionable by a different noise class this filter doesn't target
      (brand/competitor names like "vogue jewellers", service queries like
      "translate english to sinhala") — filter does what it claims, isn't
      a complete solution, human review gate still carries the rest.
- [x] **Found + fixed: `weekly_categories` had no `source` column** — every
      row from the new multi-source sourcing.py carries a `source` field
      but the DB schema and insert statement never captured it, silently
      dropped before reaching the UI. Fixed: added the column to the
      schema string AND a real migration (`_migrate()` in db.py, `ALTER
      TABLE ... ADD COLUMN` guarded by `PRAGMA table_info` check) — a bare
      schema-string edit alone would NOT have reached the already-created
      kogul.sqlite file on disk (confirmed this is exactly what would have
      happened: checked the live file right after the schema-only edit and
      the column was genuinely still absent). Wired through the insert
      statement in `_run_weekly_generate` and added a source badge
      (`SOURCE_LABEL` map) to the Weekly Outreach category card. Verified
      live end-to-end: re-ran a full generate post-fix, all 10 rows
      correctly carry `source: "daraz_new_category_gap"` in
      `/api/weekly/current`'s response.

## UI consistency pass (2026-09-30, in progress)

Per user: "use this color" (screenshot of the main Opportunity Tool's
purple/pink/blue palette) + "fix all the UI gaps and have a consistently
style CSS file".

- [x] Extracted Kogul's inline `<style>` block into `kogul/static/kogul.css`
      using the exact same CSS custom properties as `templates_ship/ship.html`
      (--purple-dark, --purple, --pink, --blue, --cyan, --yellow, etc.)
- [x] Rebuilt header (logo badge, pill, tagline), nav tabs, cards, stat
      cards, tables, badges, buttons, forms, chat UI, toggle switch to
      match the main tool's component vocabulary
- [x] Added a responsive breakpoint (chat layout stacks under 720px,
      header/tagline adjusts) — gap that didn't exist in the original inline styles
- [x] Verified CSS file serves correctly (`/static/kogul.css` → 200) and
      page JS still parses cleanly after the swap
- [ ] Visual QA in an actual browser (only verified via curl/node --check
      so far — a person should look at it, not just confirm it doesn't
      404 or throw)
- [ ] Logo: user shared a green/navy handshake mark image inline in chat.
      No tool available to save a pasted image directly to disk — asked
      user to save it to `kogul/static/logo.png` (or tell me the actual
      path). Header/favicon already wired to try loading it (graceful
      fallback to a text "K" badge if the file isn't there yet) —
      swap-in is automatic once the file exists, no further code change
      needed.

## Verification links on Weekly Outreach cards (2026-09-30)

- [ ] Add a Google search link and a Daraz category link to each Weekly
      Outreach category card, so a category can be sanity-checked (e.g.
      "Drums Percussion" landed under department "other" — a weak
      department fit worth eyeballing) before approving it.

## Product-count filter (2026-09-30)

- [x] Confirmed with user: applies to the LIVE Kapruka product count
      computed in `metrics.py` (not Daraz's own count). If a category's
      live check finds 10+ relevant Kapruka products already, it should
      not surface as a Weekly Outreach pick at all — the category is
      already well-stocked.
- [ ] Implementing: this can't be a pre-metrics sourcing filter (the live
      count isn't known until `metrics.build_category_metrics` actually
      runs the live Kapruka check, which happens per-pick, not per-source-
      row) — needs to drop a pick AFTER its metrics come back >= 10 and
      backfill with the next candidate, inside `_run_weekly_generate`'s
      loop in `app_kogul.py`, not in `sourcing.py` itself.

## Batch of fixes/requests (2026-09-30, from screenshot review)

- [x] **Root cause confirmed for serum bug, and a second live-confirmed
      instance found (user: "we have portable micro phoen", pointing at a
      real Kapruka search result for Portable Microphone):** NOT a stale
      Daraz status, and NOT a bug in `find_existing_profile` itself.
      `find_existing_profile("Serum & Essence")` correctly returns False —
      by deliberate, documented design (2026-09-09/24 false-positive
      history in kapruka_live_verify.py) a BROADER candidate term is never
      matched to a NARROWER existing page ("Serum & Essence" ⊃ "serum" —
      treating the real "serum" page as covering the broader label would
      reintroduce the exact bug that guard exists to prevent). The real
      defect is in `metrics.py`: `live_kapruka_check` passes the RAW Daraz
      category label straight into both the search and the profile check,
      but a Daraz category name is a taxonomy label, not real product
      vocabulary — same documented failure class as
      `category-search-term-derivation-2026-09-22` ("On-Dash Video" ≠
      "Dash Cam"), just hitting this new call site instead of the ones
      already fixed elsewhere in the project. There's already a real,
      hardened function for exactly this
      (`kapruka_live_verify.derive_category_search_term`) that `metrics.py`
      should be using instead of the raw label — fixed.
      `live_kapruka_check` now derives a real product term from Daraz's own
      product titles (co-occurrence-based, never a single stitched word —
      same hardened logic already proven elsewhere in this project) and
      uses it as the PRIMARY search + profile-check term; falls back to
      the raw concept only when no real term can be derived (a genuine
      Daraz product-title-less category, or a true umbrella category).
      Verified live on all 3 known-bad cases: Serum & Essence now derives
      "serum vitamin rashel", finds 16 real products, verdicts `covered`
      (was falsely `new_opportunity`). On-Dash Video (the exact case
      already flagged in [[category-search-term-derivation-2026-09-22]])
      now derives "cam dash dvr", finds 8 real dash-cam products, verdicts
      `covered` (was falsely surfacing as a gap — user confirmed live via
      Kapruka's own search page for "dash cam"). Wallpaper re-checked, still
      correctly `expand` (1 real product) — the earlier fix's guarantee
      held through this second change.
- [ ] **Duplicate categories in the same batch** — screenshot shows "Drums
      Percussion" (department: other) and "Folk World Percussion"
      (department: electronics) BOTH matched to the same Daraz category
      "Folk World Percussion" (identical revenue proxy LKR 301,594,617) —
      these are two different `daraz_categories` rows both pointing at
      the same real-world category, or a near-duplicate slug, surfacing
      twice. Need a dedupe pass — "have only one".
- [ ] **Add a manual "add category" option** — a way to add a category to
      the Weekly Outreach batch by hand, not just from the automated
      sourcing pool (e.g. for a category a person already knows about, or
      to backfill after removing a bad/duplicate one).
- [x] Confirmed: "Rename Kogul → PC agent" is DISPLAY NAME ONLY — app
      title, header, tab title, user-facing text. File names (kogul.py,
      kogul.css, KOGUL.md), folder name, DB, table/column names stay
      as-is internally. Also confirmed with user: the agent's own
      self-identity in prompts (brain.py, KOGUL.md's body text) should
      rename too, not just UI chrome — otherwise the dashboard would say
      "PC Agent" while the agent introduces itself as "Kogul" mid-chat.
      Renamed: page title, header, all 3 tab labels, all user-facing copy
      in kogul.html, brain.py's system prompts (both chat and partner-
      reply), and every "Kogul" persona reference in KOGUL.md's body text
      (kept the filename KOGUL.md itself, per display-only scope).
      Verified live: title tag now shows "PC Agent", app restarted and
      confirmed alive.
      **Known side-effect, not fully resolved:** asked PC Agent "What is
      your name?" post-rename and got "My name is Kogul — that's PC
      Agent's name" — the model is blending the new system-prompt identity
      with old chat HISTORY (kogul_chat table) that still has "Kogul"
      self-references from before the rename, and tried to reconcile both
      rather than just answering as PC Agent. This will likely self-heal
      as new history accumulates, but flagging honestly rather than
      claiming the rename is 100% clean — a determined test of an old
      thread could still surface "Kogul" from persisted history.
- [x] Confirmed: "agent sends the first message" means AUTOMATIC on
      category approve — when partners are sourced for an approved
      category, PC Agent sends the opening message to each one right
      away, not wait for a manual "Start Outreach" click per partner.
      Built: `_run_category_approve` now calls `_do_partner_auto_reply`
      for every newly-sourced partner right after insert; a per-partner
      failure is recorded (not silently dropped) and the job result
      reports opened-count + failures so the UI can tell the user exactly
      which partners still need a manual retry. Required a real refactor
      first: `_partner_auto_reply` returned Flask `jsonify()` response
      objects, which only work inside an active request context —
      calling it from a background thread (no request context there)
      would have crashed. Split into a pure `_do_partner_auto_reply(...)
      -> (dict, status)` plus thin route wrappers that jsonify() the
      result themselves. Verified live end-to-end (via
      `/api/partners/<id>/start_outreach`, same underlying function the
      auto-open path uses): real HTTP 200, real Claude CLI round-trip
      completed — and confirmed PC Agent correctly REFUSED to message a
      synthetic test partner not sourced from the real Find Partners
      pipeline ("this is placeholder/test data, so no outreach message can
      be sent to a fabricated contact"), honoring KOGUL.md's
      never-fabricate-a-partner rule exactly as specified. Full
      approve→auto-open flow not yet tested end-to-end because
      `app_ship.py` (needed for `_find_partners`) isn't running locally
      right now — the underlying send mechanism is proven, just not the
      full chain from a real approve click.

## Department-wise category list for manual/auto candidate selection (2026-09-30)

- [x] Confirmed with user: this IS the same feature as "add an option to
      add the category" from the earlier duplicate-screenshot request.
      Build: a browsable, department-grouped list of ALL sourced
      candidates (not just this week's random top-10) with an Add button
      per row, so a Partner Central person can manually pick specific
      categories into a batch instead of only ever getting the automatic
      top-N picker's choices.
- [ ] Not yet built.

## Google Ads demand metric may include non-Sri-Lanka volume (2026-09-30)

- [x] Confirmed the user is correct, root-caused it: `concept_volume` in
      `data/category_opportunities_bank/<dept>.json` is
      `sum(avg_monthly_searches)` across EVERY geo a concept was matched
      in (`build_keyword_bank_big.py`'s `save_merged`/grouping logic,
      confirmed by reading it directly), not Sri-Lanka-only — this is a
      deliberate design for a DIFFERENT purpose (diaspora demand
      detection, see memory: keyword-bank-big-design) that Kogul's Weekly
      Outreach display is unintentionally inheriting. Live-confirmed real
      rows mixing geos: "seiko 5" 900/mo summed across
      Australia/Canada/Sri Lanka/UAE/UK/US; this is common, not rare — a
      quick scan of electronics/home_lifestyle/jewellery found many
      multi-geo rows among real `opportunity` candidates.
      `geos_seen` (comma-separated country list) IS present on every row
      and can label this honestly, but a true Sri-Lanka-only split isn't
      recoverable from `category_opportunities_bank`'s output rows — the
      per-geo breakdown exists earlier in the pipeline (`variants` field
      in `build_keyword_bank_big.py`) but was never carried through into
      the file Kogul reads, only the pre-summed total was. Two real
      options: (a) label the existing figure honestly as "Ads demand (all
      geos)" and show `geos_seen` alongside it, or (b) get a true
      Sri-Lanka-only number by re-deriving it from the underlying
      `keyword_bank_big/<dept>.json` per-keyword rows (has `geo` per row,
      confirmed) instead of the pre-summed bank output.
- [x] User decision: "break google ads in sri lanka demand and
      international demand" — show BOTH numbers side by side (Sri Lanka
      demand, international demand), not just relabel or replace.
- [x] Implemented `split_ads_demand()` in metrics.py: re-groups
      `data/keyword_bank_big/<dept>.json`'s raw per-keyword rows using the
      bank pipeline's own `concept_key()` (so results align exactly with
      how the bank itself groups concepts), summing `avg_monthly_searches`
      separately for `geo == "Sri Lanka"` vs everything else. Per-department
      in-process cache so the expensive load+group only happens once per
      department per process lifetime, not once per concept.
- [ ] **Real problem found, NOT yet resolved: cold-cache cost is 10-35s
      PER DEPARTMENT** (measured live: electronics 35s, home_lifestyle
      10.7s — likely `concept_key()`'s per-row stemming/sorting work
      across 400K+ rows, not just file I/O). Weekly Outreach's generate
      loop can touch up to 16 different departments across up to
      `num_cats*6` candidates in one run — at this cost, a cold run could
      add several extra MINUTES just for the geo split, unacceptable on
      top of the already-slow live Kapruka checks. Caching only helps
      once a department's already been loaded in that same process; every
      NEW department in a run still pays the full cost once.
- [x] Investigated "coffee grinders" returning null — NOT a bug in
      split_ads_demand. Confirmed live: zero rows in
      `keyword_bank_big/home_lifestyle.json` stem to "coffee grinder" at
      all — this Daraz-native category genuinely has no Ads pricing data,
      an honest null, not a matching defect.
- [ ] **Bigger finding surfaced by this investigation: the "Google Ads
      Demand" column is already mislabeled for most of today's Weekly
      Outreach picks.** `sourcing.py`'s `source_daraz_thin_catalog` and
      `source_daraz_new_category_gap` both set `concept_volume` to
      `units_sold_sample` (Daraz's OWN sales-volume proxy), not real
      Google Ads search-volume data — confirmed by reading the source
      directly. Since the last full generate run had 10/10 picks from
      `daraz_new_category_gap`, the "Google Ads Demand" figure shown on
      every current Weekly Outreach card is actually a Daraz sales number
      mislabeled as Ads demand. This needs its own fix (rename the field/
      column for Daraz-sourced rows, e.g. "Daraz units sold" instead of
      "Google Ads Demand", and only show a real Ads-demand column — with
      the new SL/international split — for rows that actually came from
      the keyword bank). User decision: separate, honestly-labeled
      columns — "Daraz units sold" for Daraz-sourced rows, "Google Ads
      demand: Sri Lanka / International" (real split) only for
      keyword-bank-sourced rows, blank/dash where data doesn't exist for
      that row's source. Also confirmed: keep the SL/international split
      auto-running during weekly generate (not on-demand), accepting the
      per-new-department cold-load cost.
- [x] Wired into both backend and UI: `build_category_metrics` now takes
      `source` and only computes/shows real Ads figures
      (`ads_sri_lanka`/`ads_international`) for `keyword_bank`-sourced
      rows, and `daraz_units_sold` (honestly labeled, no "Ads" claim) for
      Daraz-sourced rows — all fields None/dash where the data genuinely
      doesn't apply to that row's source, rather than showing a wrong
      number under a misleading label. Weekly Outreach table split into
      3 separate columns: "Ads demand: Sri Lanka", "Ads demand:
      International", "Daraz units sold". Verified live end-to-end:
      Daraz-sourced test row correctly shows Ads fields as None + real
      daraz_units_sold; keyword-bank-sourced test row ("seiko 5") correctly
      shows real ads_sri_lanka=590/ads_international=0 (matches the
      manually-verified raw-data figure from earlier investigation) with
      daraz_units_sold correctly None. The expensive department-file load
      is now gated to ONLY run for keyword_bank-sourced rows (which are
      rare given Daraz sources are tried first) — confirmed the Daraz-path
      test above incurred no meaningful delay, only the keyword-bank path
      pays the ~10-35s cost, and only once per department per process
      (cached).

## Live Kapruka check: multiple search-term sources + visible links (2026-09-30)

- [x] Investigated "Diapering & Potty" — NOT a verdict/data bug. Checked
      the actual stored record (category id 37, this week's batch) AND
      the live `/api/weekly/current` response directly: both correctly
      show `verdict: "covered"`, 7 real matching products (Baby Potty
      Chair, Baby Toddler Potty, etc.). Asked the user to confirm what
      they actually saw — it WAS the dashboard/card showing a different
      verdict than "covered", meaning this was a stale browser view (the
      Flask server was restarted multiple times this session for other
      fixes, and the frontend has no cache-busting on `/api/weekly/current`
      — a tab left open across those restarts could show old rendered
      state until refreshed). Not a code defect to fix, but worth a
      user-facing "click refresh if a card looks wrong after I restart the
      app" note; not adding cache-busting machinery for this alone unless
      it recurs after a normal (non-restart) session.
- [ ] **User's concrete design request for the live check:** generate
      MULTIPLE search terms — from Daraz's own product names in that
      category, from the category's URL/slug, and from the category
      name itself — try EACH on Kapruka's live search, and build the
      product count as the UNION/total across all of them (not just the
      single best-scoring term, which is roughly what happens today via
      `search_terms_tried` + best-count-wins). Also: **show the actual
      search links used** (both the Kapruka search URL and presumably the
      Daraz category URL) so a human can click through and verify
      themselves — extends the "Google search + Daraz link" feature
      already added to category cards to the search-terms-tried list
      specifically.
- [x] Implemented in `metrics.py`'s `live_kapruka_check`: generates a term
      from 3 sources (derived-from-Daraz-product-titles, Daraz slug words,
      raw concept), tries every one, UNIONS relevant products across all
      of them (deduped by product URL, so the same product matched by two
      terms counts once) instead of keeping only the single best-scoring
      term. Each term's real Kapruka search URL is returned in
      `search_terms_tried` (now a list of `{term, search_url, ok, error,
      count}` objects — a shape change, confirmed nothing else in the
      codebase reads the old plain-string-list shape). Added a "Search
      terms tried" link row under each category's metrics table with a
      clickable Kapruka search link + per-term count for every term.
      Verified live on "Diapering & Potty": 3 terms tried ("cuddl velona"
      derived-but-wrong, "baby diapers" from the wrong Daraz slug match,
      "Diapering & Potty" the raw concept which correctly found all 7 real
      products), union count 7, verdict `covered` — matches the earlier
      confirmed-correct result, now with full transparency into what was
      actually searched.
- [ ] **Real bug surfaced by this test, not yet fixed:**
      `match_daraz_category("Diapering & Potty")` matched the WRONG Daraz
      row — "Baby Diapers" (slug `baby-diapers`) instead of the actual
      "Diapering & Potty" row that exists in `daraz_categories` (slug
      `baby-toddler-diapers-potties`, confirmed present via direct SQL
      query). Didn't corrupt this particular verdict only because the raw
      concept term (tried as a fallback source) still found the real
      products independently — a case with a worse Daraz mismatch and no
      working fallback term could still get a wrong verdict. Needs
      investigation: `match_daraz_category`'s word-overlap scoring
      (`kogul/metrics.py`) picked "Baby Diapers" over the exact-name
      "Diapering & Potty" row — likely a scoring-threshold or tie-break
      issue given "Diapering & Potty" shares FEWER words with itself
      post-stopword-filtering than expected.

## Curation filter: "worth pursuing" gate before surfacing (2026-09-30)

- [x] User: "before giving me make sure is it worthy pursing coz we dont
      want our website to be another daraz we are like an invite only
      very specific" — clarified immediately: "not luxury premium", "we
      have a class" (pointing at the EXISTING tier/classification system
      already built elsewhere in this project, not asking to invent a new
      one from scratch) — see [[dynamic-category-tier-strategy]]:
      `scripts/tier_scoring.py`'s T1-T5 tiers (Winning/Expand/Selective/
      Hold/Add-to-win), `business-context.md`'s LINE classification
      (Gifting vs Ecom/self-purchase vs "Hard" commodity/margin-risk),
      and the Moats framework (`scripts/moats.py` — Freshness,
      Discretion, Heritage, Occasion Timing, Diaspora, Presentation,
      Repeat-Buy Trust, Regulated). T5 "Add-to-win" is specifically the
      already-gated true-gap bucket (excludes anything mapping onto an
      existing winning category, margin-risk-flagged for Hard-line
      commodity) — this is the real "worth pursuing" filter to reuse, not
      a new invention.
- [x] User confirmed business context directly: "it is gifting mostly and
      self purchase" (matches business-context.md's Line A
      Gifting/diaspora-moat + Line C Ecom self-purchase strategic push
      verbatim) — NOT a raw commodity-marketplace-parity strategy.
- [x] User gave 4 concrete positive examples to calibrate against: "car
      interior accessories are good, guitars are good, 3d printers are
      good, drums percussion are good" — notably 2 of these (Drums
      Percussion, and by extension likely 3D Printers/Guitars) landed in
      the weak `department: "other"` bucket earlier this session, meaning
      department-fit alone is NOT the filter user wants — something else
      about these categories (specialty/hobbyist/considered-purchase
      nature?) makes them worth pursuing despite a poor department match.
      Checked live against the real `line_fit()` function
      (`scripts/daraz_addwin.py`): it DISAGREES with the user on all 4 —
      "Car Interior Accessories" -> Hard (commodity, via the `car`
      HARD_TOKEN), "3D Printers" -> Hard (via `printer` HARD_TOKEN),
      "Guitars" -> Skip (no department/taxonomy match at all), "Drums
      Percussion" -> Skip. Confirms the suspicion: the existing LINE/Hard
      system was built for a DIFFERENT question (margin-risk/commodity
      screening for the main Daraz-gap dashboard) and doesn't capture what
      "worth pursuing" means for Weekly Outreach's partner-onboarding
      purpose — these are specialty/hobbyist, considered-purchase
      categories (musical instruments, car customization, maker/hobby
      tech) that a blunt commodity-token filter (HARD_TOKENS includes
      bare "car" and "printer") wrongly flags as generic commodity. NOT
      reusing the existing system as-is — will ask the user what the real
      pattern is across their 4 examples before building a new filter,
      rather than guess at "specialty/hobbyist" as the criterion myself.
      User: "see what are the patterns it falls into" — investigated the
      real data behind all 4 examples (genuine_count, total_products,
      avg_price, units_sold_sample, revenue_proxy from `daraz_categories`)
      against 4 deliberately "bad"/commodity comparison categories (Baby
      Diapers, Coffee Grinders, Sports Nutrition, Ladders & Workbenches).
      Finding: the raw NUMBERS are structurally identical across both
      groups (similar revenue proxy, avg price, near-zero genuine_count,
      substantial Daraz total_products) — no numeric threshold separates
      "good" from "bad" here. Reported this back to the user rather than
      forcing a fake numeric pattern.
      **User's real answer (multi-select):** (1) Gifting angle — could
      this be bought AS a gift (matches business-context.md's Gifting-
      first strategy exactly); (2) Self-expression/passion/hobby category,
      not household utility (guitars/3D printers/car mods = identity and
      creative pursuit; diapers/workbenches/coffee grinders = pure
      functional commodity — the "become another Daraz" feeling to avoid);
      (3) it may also just mean "add Automobile-specialty/Electronics-
      specialty/Musical-Instruments as real departments," not a
      universal per-category rule; PLUS a late addendum "some premium
      uniqueness is there" (distinctive/non-interchangeable, not
      mass-commodity — echoing back to the earlier "premium" language,
      now clearly meant as a QUALITY signal, not literal Kapruka-facing
      copy, consistent with the earlier "not luxury premium" wording
      clarification).
      **Conclusion: this is a SEMANTIC/qualitative judgment (giftability +
      passion-category-ness + distinctiveness), not something derivable
      from Daraz's numeric fields.** No numeric filter can build this — it
      needs actual reasoning about what a category IS. Natural fit for an
      LLM-based gate (one Claude CLI call per candidate category asking
      exactly this 3-part question), similar in spirit to the "on-demand
      LLM judge" option already offered-and-declined earlier for the
      unrelated noise-filtering problem, but here it's the RIGHT tool
      because the question is genuinely semantic, not detectable from
      structured data.
      **User decisions, confirmed:** (1) gate runs BEFORE the expensive
      metrics step, right after sourcing — reject fast on just the
      Ads/Daraz row + one Claude call, so categories that would be
      rejected anyway never pay for a live Kapruka check or Daraz match;
      (2) gate works ALONGSIDE human review, not instead of it — a
      rejected candidate never reaches the Weekly Outreach list at all,
      but anything that passes STILL needs a manual Approve click before
      partners are sourced, same as today's existing gate.
- [x] Built `kogul/worth_pursuing.py` (one batched Claude CLI call per
      generate run, not one-per-category — up to 15 candidates/call,
      judged against the 3-part criterion: giftable, passion/hobby/
      self-expression, distinctive/non-commodity). Fail-open design: any
      CLI error or malformed JSON response keeps the whole batch
      unfiltered rather than silently dropping real candidates — a false
      reject here permanently hides an opportunity, a false keep just
      costs one manual Reject click (same asymmetry principle already
      used by every other filter in this pipeline). Wired into
      `_run_weekly_generate` right after sourcing, before the expensive
      metrics loop. Added `worth_pursuing_reason` as a new DB column (with
      migration, same pattern as `source`) and surfaced on the category
      card for transparency.
      **Verified live with a real mixed batch** (not a guess): ran all 4
      of the user's confirmed "good" examples plus 3 of the "bad"
      comparison categories through the real function. Result matched the
      user's own judgment exactly — Guitars, 3D Printers, Drums
      Percussion all PASSED with sensible reasons ("musical instrument —
      passion/hobby category and commonly gifted", "maker/hobby tech...
      not plain utility"); Baby Diapers, Coffee Grinders, Ladders &
      Workbenches were all correctly REJECTED. This is real evidence the
      gate works as intended, not just that it runs without error.
      Full-pipeline end-to-end test (real `/api/weekly/generate` call
      exercising worth-pursuing + product-count cutoff + metrics +
      source labeling all together) started, running in background —
      pipeline is now meaningfully slower with the added LLM gate on top
      of live Kapruka checks; will confirm final picks once it completes.

## Portable Audio Microphones spot-check (2026-10-01)

- [x] User flagged `srilanka_online_search.jsp?d=Portable%20Audio%20Microphones`
      + "we have a range" — checked both the live Kapruka search directly
      AND the stored Weekly Outreach record (category id 42). Both agree:
      3 real matching products (Portable Speaker w/ Wireless Mic, WS 858
      Portable Karaoke Mic, Singer Portable Party Speaker), verdict
      `expand` (the 1-4-products bucket, correctly NOT `new_opportunity`
      or `covered`) — this one is NOT a false-positive like the earlier
      wallpaper/serum/dash-cam/diapering cases.
      **User's answer: "depending on the category up, let these category
      have low score"** — graduated scoring, not a hard include/exclude.
      True 0-product gaps should rank highest; `expand` (1-4 real
      products, "we have a range") should rank LOWER in what gets picked
      for the week, but stay eligible, not be excluded outright. This
      means the live product-count verdict needs to become a real
      priority input on top of (or alongside) `concept_volume`/revenue
      ranking, which currently has no awareness of the live-check result
      at all — `top_category_opportunities()` ranks purely on
      volume/revenue, computed BEFORE any live check runs. Needs a
      two-pass design: source+worth-pursuing-gate first (as today), run
      metrics (which includes the live check) on the survivors, THEN
      re-rank by a combined score (verdict strength + demand) before
      picking the final num_cats, rather than cutting off at num_cats
      before the live verdict is even known.
      Implemented: `_run_weekly_generate` now computes metrics for the
      FULL candidate pool (not stopping at num_cats as before), scores
      each by `verdict_weight` (new_opportunity=4 > real_opportunity=3 >
      expand=2 > seo_profile_opportunity=1 > covered/check_failed=0) with
      a tiny log-scaled demand bonus that only breaks ties WITHIN the same
      verdict tier (never lets raw demand override the verdict tier
      itself), sorts, and inserts only the top num_cats.
      **Real cost tradeoff, flagged not hidden:** this makes generate
      noticeably SLOWER — it now runs the live check on the full pool
      (up to num_cats*6 candidates) instead of stopping as soon as
      num_cats survivors were found. Accepted as the correct tradeoff
      since the whole point is comparing verdicts across the pool before
      picking, which requires knowing all of them first.
      **Verified live end-to-end (2026-10-01, batch id=7, 10
      inserted/14 skipped well-stocked):** picking worked correctly —
      but found and fixed a SECOND bug: `/api/weekly/current` was still
      serving the category list `ORDER BY concept_volume DESC` in raw
      SQL, which completely undid the new scoring at DISPLAY time (a
      73K-volume `expand` row showed above a 10K-volume true 0-product
      `real_opportunity` row). Fixed by re-sorting the API's output list
      in Python using the exact same verdict_weight + log-scaled demand
      formula used to pick the batch. Confirmed correct order on the
      real batch 7 data after the fix + Flask restart: 3D Printing
      and Folk World Percussion (`real_opportunity`, 0 live products)
      now rank 1st/2nd; False Eyelashes/Portable Audio Microphones/
      Optics Accessories/Drums Percussion (`expand`, "we have a range")
      rank in the middle, highest-demand-first within that tier; Car
      Video/Guitars/In-Dash DVD/Orchestral Strings (`covered`) rank
      last. Matches the user's instruction exactly: true gaps first,
      expand categories lower but still included, never hard-excluded.

## Partner specialization gate (2026-10-01)

- [x] User request: "make sure now u pick candidates where they specialize
      in that category they have that category as a whole business, now
      when it comes to eyelashes see if theres any business that
      specializes in that" — Find Partners previously only checked
      `product_verified` (does the domain have ANY page mentioning the
      product term — e.g. a general salon that happens to offer one lash
      service would pass that check). That is a different, weaker signal
      than "this business's core offering IS this category" (a dedicated
      lash studio/supplier).
      Built `kogul/specialization.py` — same semantic-gate pattern as
      `worth_pursuing.py` (batched Claude CLI call, fail-open on error —
      keep unjudged rather than silently drop a real partner), reasoning
      over each candidate's name/source/url/address text to judge
      `is_specialist` + `specialization_reason`. Wired into `_find_partners()`
      in app_kogul.py (the one shared choke point used by both
      category-approve sourcing and the remove/refill path) — results are
      re-sorted specialists-first (then product_verified, then name), NOT
      hard-filtered, so a niche category with zero dedicated specialists
      in Sri Lanka still gets its best available candidates rather than
      an empty list. Added `is_specialist`/`specialization_reason` columns
      to the `partners` table (migration-safe, same PRAGMA table_info
      pattern as the earlier `source` column fix) and surfaced a
      "specialist" badge + reason on the Partner Chats/category partner
      table in kogul.html.
      **Verified live with the user's own example** — ran a real
      `/api/find_partners?business=eyelashes` call against app_ship.py
      (56 raw Business Scout results, `product_verified=True` on nearly
      all of them — confirming that flag does NOT discriminate
      specialists at all) then ran the real `specialization.py` judgment
      over the actual response: correctly flagged 19/56 as genuine lash
      specialists (Cinderella Lashes, Matsugelk "Lash - Brow - Skin
      studio", The Lash Lounge & Academy, Korean False Lashes Sri Lanka,
      Lash Up, etc. — names/URLs that directly name the lash category as
      the core business) while correctly excluding generic multi-service
      salons that merely also do lashes (Doctor salon, Beauty by Reena,
      Black Beauty Secret Salon, Chanu Bridal & Beauty, DL beauty studio)
      even though Business Scout had verified the product on all of them.
      This is real evidence the gate discriminates correctly, not just
      that it runs without error.

## "3D Printing" category false positive (2026-10-01)

- [x] User flagged `daraz.lk/3d-printing/` with a screenshot — the Daraz
      category page is overwhelmingly 3D-PRINTER ACCESSORIES/CONSUMABLES
      (filament spools, nozzles, build plates, resin, enclosures), not
      actual 3D printers as a product. "this is not a good category to
      pursue coz its not technically 3d printers".
      Root cause confirmed via `data_store.sqlite`: Daraz's own taxonomy
      nests a PARENT "3D Printing" row (breadcrumb ends at "...>
      3D Printing", 4080 total products, revenue_proxy 144.6M) above 3
      separate LEAF children, each its own `category_gap` row with its
      own breadcrumb: "3D Printers" (actual hardware, 477 products,
      revenue_proxy 123.9M), "3D Printer Parts", "3D Printing Materials".
      `source_daraz_new_category_gap` sorted purely by `revenue_proxy`
      across ALL category_gap rows with no parent/child awareness, so
      the vaguer parent umbrella outranked the real hardware leaf by a
      small revenue margin. Confirmed APP-WIDE, not a one-off (per
      feedback-app-wide-fixes memory): 90 of 3185 category_gap rows are
      a parent of another category_gap row in the live dataset
      (Diapering & Potty, Formula Milk, Laundry And Home Care, Media
      Players, Men's Care, Storage & Organisation, Drawing Tools, etc. —
      every one of these was eligible to get picked instead of its own
      more specific, more pitchable leaf child).
      Fixed in `source_daraz_new_category_gap`: now excludes any row
      whose breadcrumb is a strict prefix of another category_gap row's
      breadcrumb (i.e. it has a sibling leaf already in the same gap
      set) before ranking by revenue — the more specific child sources
      instead of the vaguer parent. A parent with no leaf children
      present in category_gap (the vast majority) is unaffected.
      **Verified live:** re-ran `source_daraz_new_category_gap(limit=20)`
      fresh after the fix — "3D Printing" (parent) no longer appears in
      the sourced list at all; "3D Printers" (`daraz.lk/3d-printers/`,
      the real hardware leaf) now appears in its place. Not yet re-run
      through a full `/api/weekly/generate` end-to-end (next weekly
      batch will pick up the fix automatically — no separate action
      needed).

## Department-aware product-count cutoff + visible category score (2026-10-01)

- [x] User: "depending on the category if we have more than 5 then
      especially automobile" + listed 8 core departments: electronics,
      clothing, fashion, grocery, home & lifestyle, sport fitness, toys
      and kids, beauty and fragrance. Clarified via AskUserQuestion:
      (1) Automobile specifically gets a LOWER live-product cutoff (5,
      not the current flat 10) — the other 7 core departments keep the
      existing 10. (2) These 8 departments are now the canonical CORE
      department list; anything outside it (automobile, jewellery,
      books_stationery_cards, gift_sets_combos, pet, services, other) is
      non-core and gets the same stricter bar as automobile.
      Real `kapruka_department` slugs confirmed live from
      `data_store.sqlite`: automobile, beauty_fragrance,
      books_stationery_cards, clothing_fashion, electronics,
      gift_sets_combos, grocery_fresh, home_lifestyle, jewellery, other,
      pet, services, sports_fitness, toys_kids. Core set =
      {electronics, clothing_fashion, grocery_fresh, home_lifestyle,
      sports_fitness, toys_kids, beauty_fragrance} (7 slugs — "fashion"
      and "clothing" are one department on the real data,
      clothing_fashion).
      Implemented `CORE_DEPARTMENTS` + `_max_live_product_count()` +
      shared `_score_candidate()` in app_kogul.py (core dept bonus folded
      into the existing verdict_weight + demand_bonus score as a small
      tie-break, never enough to let a non-core 'expand' row outrank a
      core 'real_opportunity' row). Also: new `priority_score` DB column
      (migration-safe), stored at generate time and reused (not
      recomputed) for the `/api/weekly/current` display sort.
      ALSO same-turn request: "assign a score to each category so itll
      be easy to prioritize which cat to expand" — surfaced as a visible
      "Priority: X.XX" badge on each category card in kogul.html.
      **Verified live:** isolated pipeline run against real data
      confirmed the dept-aware cutoff fires (18 candidates skipped as
      well-stocked in one real test run, vs the old flat-10 behavior)
      and the stored/displayed score correctly orders real_opportunity
      (3.0+) > expand (2.0+) > covered (0.0+).

## No near-duplicate categories + avoid accessory categories (2026-10-01)

- [x] User: "make sure theres no overlap between suggested category coz
      one partner can carry more than one category products on the same
      business line" — confirmed live from data_store.sqlite: Musical
      Instruments alone has Drums Percussion / Folk World Percussion /
      Marching Percussion / Concert Percussion as separate category_gap
      rows, every one of which a real percussion shop would carry at
      once. Clarified via AskUserQuestion when I asked "fix this but
      lets change the model a bit": NOT a plain word-overlap heuristic —
      built `kogul/dedupe.py`, a SEMANTIC LLM judgment (one batched
      Claude CLI call grouping the candidate pool into real-world "same
      business line" clusters, keeping only the top-priority_score
      survivor per cluster), same pattern as worth_pursuing.py /
      specialization.py. Wired into `_run_weekly_generate` AFTER scoring
      (so it knows which survivor to keep) but BEFORE picking the final
      num_cats (so a merged-away duplicate's slot backfills from the
      next genuinely different candidate, not left empty).
      **Verified live** with a direct test (real Claude CLI call, 10
      candidates incl. 4 percussion variants + Guitars/Guitar Amplifiers
      + Car Video/On-Dash Video + 2 unrelated categories): correctly
      collapsed all 4 percussion rows to 1, Guitars+Guitar Amplifiers to
      1, Car Video+On-Dash Video to 1, left 3D Printers and Coffee
      Grinders untouched as their own clusters. Also verified on a real
      end-to-end pipeline run: 6 survivors deduped to 4 unique.
- [x] User (same turn): "Avoid accessories also" — in context, right
      after the 3D Printing/accessories false-positive fix: categories
      that are themselves an ACCESSORIES/PARTS sub-category (e.g.
      "3D Printer Parts", "Interior Accessories", "Camera Accessories",
      "Microwave Accessories") are not good standalone partner-outreach
      targets. Added `_is_accessory_category()` (word-match on
      accessories/accessory/parts/spares/components) to sourcing.py,
      applied in `top_category_opportunities`'s combine step across all
      4 sources. **Verified live:** 0 accessory-category leaks in a real
      40-60 row sourced pool; "Microwave Accessories", "Interior
      Accessories", "Optics Accessories", "Bedding Accessories" all
      correctly excluded while "3D Printers" (real product) passed
      through untouched.

## Brand discovery (2026-10-01)

Per user: "kogul shouldnt stick only to the daraz data" / "kogul should be
able to see new interesting brands website to onboard" — a 5th Weekly
Outreach source alongside sourcing.py's 4, keyed on a real BUSINESS (name,
products we could bring in, why it fits, reach-out button) rather than an
abstract Daraz/GSC category gap. User's own "good examples": sokkrepublic.com,
getmazeted.com, skrumptious.lk, caravanfresh.lk (2 of these turned out to
already be Partner Central partners — see below). User explicitly confirmed:
run alongside the existing 4 sources, not instead of (own example: "herb
category we had only 2" — brand discovery should top up a thin category's
week, not replace Daraz/GSC sourcing); tag each brand candidate with its
closest department for filtering/dedupe; judge fit on real business context
(independent/niche/giftable, not mass-commodity), same reasoning
worth_pursuing.py already applies to bare category labels.

- [x] `kogul/partner_sitemap.py` — fetches the LIVE Partner Central roster
      (`kapruka.com/scripts/sitemap_partners.jsp`, confirmed real XML,
      ~420-700 `<loc>` entries as `kapruka.com/partner/<slug>`), cached
      24h, with a fuzzy word-overlap match (exact / likely / uncertain)
      against a candidate business name — exact/likely auto-excludes a
      candidate, uncertain keeps it but flags it on the card for human
      review rather than silently hiding a possibly-new brand (same
      fail-open asymmetry as every other filter in this pipeline).
      **Verified live** against the real sitemap: "Sokk Republic" and
      "Skrumptious" both correctly matched `exact` (they ARE already
      partners — `sokk-republic`, `skrumptious`); "MAZE Socks" and a
      made-up name both correctly returned no match; "Caravan Fresh"
      returned a weak `uncertain` match on an unrelated slug
      ("nature-fresh", shared word "fresh") — correctly NOT auto-excluded,
      shown as a review flag instead. Note: Caravan Fresh's own products
      DO appear elsewhere on kapruka.com, but it has no `/partner/` slug
      — this module answers "is it a Partner Central partner specifically",
      nothing broader.
- [x] `kogul/BRAND_SEEDS.txt` — plain user-editable seed list (one URL per
      line, same convention as KOGUL.md), pre-loaded with the user's 4
      examples (the 2 already-onboarded ones commented out for reference).
- [x] `kogul/brand_discovery.py` — `source_brand_discovery()`: reads unseen
      BRAND_SEEDS.txt URLs first, real web-search fallback only once the
      seed list runs dry (see claude_cli.run_claude_cli_with_search below);
      fetches each site for real via `scripts/fetch_page.py` (Playwright);
      excludes/flags already-onboarded brands via partner_sitemap; judges
      business-context fit (`judge_brand_fit`, one batched Claude CLI call
      per batch, same fail-open pattern as worth_pursuing.py/
      specialization.py) directly against each site's real fetched title/
      meta/headings/body — not a bare category label. Every attempted URL
      recorded in `kogul/data/brand_discovery_seen.json` so a dead/rejected
      site isn't retried forever.
      **Verified live end-to-end**: real fetch of getmazeted.com succeeded
      (title/meta/headings/body all extracted correctly); real Claude CLI
      fit judgment on MAZE Socks + Caravan Fresh both returned `fits: true`
      with a correct department and a specific, non-templated
      `products_to_bring_in` sentence grounded in the actual fetched page
      content (not generic filler); full `source_brand_discovery(limit=2)`
      run against the real seed file produced 2 correctly-shaped candidate
      rows end-to-end.
- [x] `kogul/claude_cli.py`: added `run_claude_cli_with_search()` — grants
      `--allowedTools "WebSearch"`, the ONE deliberate exception to "Kogul
      gets no tool access" (user explicitly confirmed this approach over
      a Bright Data/Business-Scout-reuse alternative). Used ONLY by
      brand_discovery's web-search fallback step; every other gate
      (worth_pursuing, specialization, dedupe, judge_brand_fit) stays on
      the original tool-free `run_claude_cli`.
- [x] `weekly_categories` schema: added `business_url`, `products_to_bring_in`,
      `already_partner_flag` columns via `_migrate()`. **Verified live**
      against the real on-disk `kogul.sqlite` — migration applied cleanly,
      all pre-existing rows/columns intact.
- [x] `app_kogul.py` wiring: `_run_weekly_generate` sources brand candidates
      alongside the 4 existing sources (brand rows skip the Daraz-only
      live-Kapruka-check metrics step — there's no Daraz category to check
      a live count against, the business itself already IS the fetched/
      judged evidence) and scores them at a fixed 3.5 (between
      real_opportunity and new_opportunity) so they compete for weekly
      slots without being crowded out by category rows, addressing the
      user's own "herb category only had 2" top-up case.
      `_run_category_approve` branches on `source == 'brand_discovery'`:
      builds ONE partner row directly from the business's own
      name/URL (no Business Scout call — the candidate already IS the
      partner), `is_specialist`/`product_verified` set true by
      construction. `api_partner_remove` also branches: removing a
      brand-discovery partner reports no-refill-possible (adding a
      Business-Scout search for a specific brand name would return
      unrelated junk, not a real alternative) instead of silently
      searching for a "replacement" that doesn't make sense.
      **Verified live**: app boots clean, `/api/weekly/current` serves the
      new columns correctly (null on old pre-existing rows, no crash) on
      a real existing batch.
- [x] `templates/kogul.html`: Weekly Outreach card renders a brand-discovery
      row distinctly — business name, department badge, "Visit website"
      link (instead of a Google-search link), "📦 What we'd bring in"
      line, fit reason reframed as "Why this is a good onboarding fit",
      an uncertain-partner-match ⚠ flag, and "Approve → reach out" instead
      of "Approve → source partners"; the Daraz/GSC metrics table is
      hidden for brand rows (not applicable). **Verified**: inline script
      passes a Node syntax check (`new Function(...)`) with no errors.

- [x] Full end-to-end `_run_weekly_generate` verified live against a
      THROWAWAY COPY of kogul.sqlite (never touched the real DB — confirmed
      real batches unchanged at id=7 after the test, copy deleted after):
      inserted 6 real brand_discovery rows into a fresh batch, each a real
      business discovered via the web-search fallback (seed list was
      already exhausted by earlier isolated tests — Nuts on Top, Miss
      Nutri, Hansa Ceylon Coffee, Oona Collections, Sweet Dreams Star,
      Cecrafto — none of which were in BRAND_SEEDS.txt or seen before),
      each with a real fetched URL and the correct priority_score 3.5.
      Notably, the Daraz/GSC/bank category sources returned ZERO survivors
      this particular run (all skipped as well-stocked) — brand discovery
      was the ONLY source that filled the week, a live real-world
      demonstration of the user's own "herb category only had 2" top-up
      case working as intended.

## Brand discovery gap-fix round (2026-10-01)

User asked "can u fix it fully" + "check for any UI gaps also" after a
self-critique raised 3 concerns, followed by an independent code-review
agent audit of brand_discovery.py/partner_sitemap.py/claude_cli.py/
app_kogul.py/kogul.html (11 findings, 2 rated HIGH). All HIGH/MEDIUM
findings fixed; LOW findings fixed where cheap, otherwise left open below.

- [x] **User's 3 requested fixes, all confirmed as recommended:**
  - Web-search-found candidates now carry `found_via_web_search` (new DB
    column) and show a "🔍 found via web search — spot-check before
    approving" badge distinct from a seed-list pick, which shows no badge.
  - `judge_brand_fit`'s prompt now instructs the model to use concrete
    evidence already in hand (don't guess fit from a thin/empty fetch —
    see HIGH finding #3 below, same code change covers both).
  - Web search capped at `max(1, limit // 2)` of the week's brand quota —
    a thin/empty BRAND_SEEDS.txt means brand_discovery contributes LESS
    that week, never becomes fully search-driven.
- [x] **[HIGH, audit finding #1] Outreach message prompt was incoherent for
      EVERY brand_discovery partner** — `category` and `partner_name` both
      equal the business name for a brand row, so the old hardcoded
      "Kapruka wants to expand its '{category}' category... and this
      business ({partner_name})" context line read as nonsense ("expand
      its 'Maze Socks' category... and this business (Maze Socks)"), and
      never mentioned the real researched product line at all. Fixed:
      `brain.build_partner_reply_prompt` takes `category_source` +
      `products_to_bring_in`, branches to a distinct brand-grounded context
      line naming the specific products from judge_brand_fit's own
      research. `_do_partner_auto_reply`'s query extended to pull both
      fields. **Verified**: real PC Agent WhatsApp opening messages to
      brand candidates will now reference actual products, not a
      self-referential category label.
- [x] **[HIGH, audit finding #2] dedupe_same_business_line mixed business
      names with category labels** — the dedupe prompt's "same retailer,
      same product line" clustering reasoning was never designed to
      compare a business name ("Maze Socks") against a Daraz category
      label ("Drums Percussion") in the same candidate block, risking a
      brand candidate silently merged away under reasoning that doesn't
      apply to it. Fixed: brand_discovery rows are now excluded from the
      dedupe pass entirely (split into category_rows/brand_rows before
      dedupe, recombined and re-sorted after) — they're already
      deduplicated by business identity via partner_sitemap + source_brand_
      discovery's own exclude_business_names, so a second semantic pass
      added risk with no benefit.
- [x] **[HIGH, audit finding #3] No prompt-injection defense in
      judge_brand_fit** — fetched site content (title/meta/headings/body)
      went into the CLI prompt completely unescaped/undelimited; a
      malicious or SEO-spam-stuffed site's own text could plausibly
      hijack the fit verdict a human reviewer trusts before approving.
      Fixed: every candidate's scraped fields wrapped in explicit
      `<<<PAGE_CONTENT>>>...<<<END_PAGE_CONTENT>>>` delimiters, with an
      up-front instruction to treat everything inside as DATA about the
      business, never as instructions, regardless of what it says. Same
      prompt change also added the thin/empty-content guard (audit finding
      #6): near-empty scraped content must not auto-pass OR auto-fail —
      flagged as insufficient evidence for manual review instead.
- [x] **[MEDIUM, audit finding #4] No atomic writes on
      brand_discovery_seen.json / partner_sitemap_cache.json** — a crash
      mid-write (same risk class as this project's own documented
      daraz-mall-concurrent-run-race incident) could leave either file
      truncated/corrupt, and partner_sitemap's own corrupt-cache fallback
      path would then fail identically forever. Fixed: both now write via
      a temp file + atomic `Path.replace()` instead of a direct
      `write_text()`.
- [x] **[MEDIUM, audit finding #5] `import requests` failure was
      indistinguishable from a network failure** in
      `partner_sitemap.fetch_partner_slugs` — a missing/broken `requests`
      install would silently look exactly like "sitemap unreachable,"
      permanently returning `[]` and defeating the ENTIRE already-
      onboarded check with zero distinguishing signal anywhere. Fixed:
      `import requests` moved outside the network try/except so a genuine
      ImportError now raises loudly instead of being swallowed.
- [x] **[MEDIUM, audit finding #7, 2 iterations] `_guess_business_name`'s
      title-splitting was unreliable** — first fix (prefer the longest
      non-generic title segment) was ITSELF live-tested and found wrong in
      practice: "MAZE Socks Sri Lanka | Fun Happy Gifts For Men & Women"
      picked the long marketing tagline over the real 10-char brand name;
      "Welcome to MAZE Socks | Online Store" kept "Welcome to MAZE Socks"
      since that exact phrase wasn't in the generic-word blocklist.
      Second, correct fix: prefer the DOMAIN NAME itself as the primary
      signal (immune to tagline-length noise, always present), falling
      back to the SHORTEST non-generic title segment only if the domain
      looks too short/generic to use. **Verified live** against 6 real
      title patterns including both failure cases above — all correct.
      This fix also surfaced and required fixing a NEW regression it
      introduced: a concatenated domain-derived name ("Sokkrepublic", no
      internal spaces) could never word-overlap-match a hyphenated sitemap
      slug ("sokk-republic") in partner_sitemap.py, since that comparison
      needs real word boundaries on BOTH sides. Fixed with an added
      concatenated-form exact-match check (strip all spaces/hyphens from
      both sides, compare as one token) in `match_existing_partner`,
      checked before word-overlap scoring. **Verified live**: full
      regression matrix (8 cases incl. concatenated + spaced forms of
      known onboarded/not-onboarded brands) all matched expectation.
- [x] **[LOW, audit finding #10] Silent zero-candidates from brand_discovery
      was indistinguishable between "genuinely erred" and "ran fine, found
      nothing"** — `_run_weekly_generate`'s `except Exception: brand_pool =
      []` swallowed the real error with no visible marker anywhere, unlike
      worth_pursuing.py/specialization.py's per-row "(check failed)"
      reason strings. Fixed: the exception is now recorded as
      `brand_discovery_error` on the job result and surfaced in the
      Weekly Outreach generate-status line in the UI
      ("⚠ brand discovery failed this run (...), category sourcing was
      unaffected").
- [x] **[LOW, audit finding #9] Inconsistent shortfall*2/limit*2 multiplier
      math** in the web-search-fallback sizing — replaced with one
      consistent `room_for_search` calculation (bounded by both the actual
      shortfall AND the new web-search cap), asked for `room_for_search*3`
      candidates to give the fit/sitemap filters real margin.
- [x] **UI gaps found in a dedicated pass** (user: "check for any UI gaps
      also"), beyond what the code-review audit's finding #11 already
      caught:
      - Weekly Outreach's generate-status line never surfaced
        `brand_discovery_error` — fixed (see finding #10 above).
      - The approve-button's in-flight label was hardcoded to "Sourcing
        partners + sending opening messages..." for every category,
        including brand rows (which source nothing) — now reads "Sending
        opening message..." for a brand row via a new
        `data-is-brand` attribute on the button.
      - The approved-state partner-count text read "N partner(s) sourced"
        for brand rows too (nothing was "sourced," it's the business
        itself) — now reads "Opening message sent" for brand rows.
      - Partner Chats' per-chat header unconditionally showed "no phone"
        for a brand_discovery partner (phone is never populated for this
        partner type — it's not a missing-data case) — now shows the
        business's own website link instead when `p.source ===
        'brand_discovery'`.
      - [audit finding #11] `live_kapruka_search_terms_tried` block wasn't
        wrapped in the `isBrand` guard like every other metrics-table block
        (harmless today only because `Array.isArray(undefined)` is falsy)
        — now explicitly gated for consistency with the rest of the card.
- [x] **Full live re-verification after all fixes**: Python compiles
      clean; DB migration applies cleanly to the real on-disk kogul.sqlite
      (`found_via_web_search` column present); app boots with no errors;
      `/api/weekly/current` (10 real categories) and `/api/partners` (4
      real partners) both serve correctly with no crash from the modified
      queries/joins; re-ran `judge_brand_fit` end-to-end on a real fetched
      site (getmazeted.com) after the injection-delimiter + thin-content
      prompt changes — still produces the same correct, concrete,
      non-templated verdict as before the fix (no quality regression from
      the added instructions); inline JS passes a Node syntax check.

## Business-first + Material redesign (2026-10-01)

User: "now we agreed put business name at first over categories / so weekly
it should generate businesss as i mentioned with the criterias / and it
should be visually attractive / for each buisness there should be images
fetched from there website, ig, facebook, square images etc / Fix the Font -
it should be google font / make it like google designed it".

- [x] Businesses rank FIRST: _BRAND_DISCOVERY_SCORE raised 3.5 -> 4.5
      (above new_opportunity's 4) so brand candidates win weekly slots
      ahead of every category verdict tier, AND /api/weekly/current pins
      brand cards above category cards at display time regardless of
      stored score (covers pre-raise rows stored at 3.5). Category supply
      survives because brand sourcing stays capped (max(num_cats,3), web
      search half of that) — businesses lead, categories fill the rest.
- [x] Per-business images: scripts/fetch_page.py gained an OPT-IN --images
      flag (og:image/twitter:image first, then largest rendered <img>s by
      naturalWidth*naturalHeight >= 200x200, svg/data: filtered, deduped;
      plus instagram/facebook/tiktok profile links) — opt-in so the daily
      SEO runs' existing extracts are byte-identical. brand_discovery
      passes --images, parses the new sections (and the BODY regex was
      tightened to stop at the next section header so image URLs can't
      leak into the body_excerpt the fit judgment reads), carries
      brand_images/social_links through judge_brand_fit's **f spread into
      the output row -> new DB columns -> card. **Verified live** on
      getmazeted.com: 8 real product images (Shopify CDN, mostly square)
      + both real FB/IG profile links flowed end-to-end into a correctly
      shaped candidate row. NOTE: actual IG/FB photo scraping is
      login-walled — profiles are LINKED (social chips), site's own
      product shots carry the visual load.
- [x] Card redesign: brand cards get .brand-card elevation, 20px
      .brand-headline, square image gallery (aspect-ratio 1/1, object-fit
      cover, rounded 12px, loading=lazy, referrerpolicy=no-referrer for
      hotlink-protection CDNs, onerror=remove so broken images vanish
      instead of showing broken-image icons), labeled "What we'd bring
      onto Kapruka" / "Why this is a good onboarding fit" sections, and
      Instagram/Facebook/TikTok social chips.
- [x] Material/Google redesign of kogul.css: Roboto + Roboto Mono from
      Google Fonts (preconnect + css2 link in kogul.html head),
      Google-blue #1a73e8 primary, Google gray palette, outlined 16px
      cards with hover elevation, pill (fully-rounded) Material buttons,
      tonal chips, Material tabs with blue active underline, sticky white
      app bar, restyled chat bubbles/toggle/stat cards. All class names
      unchanged so the inline JS needed zero changes. SUPERSEDES the old
      "same palette as the Opportunity Tool" rule by explicit user
      direction this session — do not revert to the purple tokens.
- [x] Verified live: --images output on a real site; full
      source_brand_discovery(limit=1) run (images+socials in the row); the
      exact brand INSERT replayed against a throwaway DB copy (JSON
      columns write+read back correctly; real DB untouched); migration
      applied to the real kogul.sqlite; app boot; /api/weekly/current
      serves brand_images/social_links as [] (not null) for old rows;
      GET / serves the Google Fonts link; JS syntax passes node check
      (one real bug caught by it: a template ternary I wrote without its
      else-branch).
- [x] Operational catch #1: THREE stale kogul servers were still listening
      on :5010 from earlier boot tests — Git Bash `kill $!` does NOT
      reliably kill a backgrounded Windows python process, so my API
      "verification" curl was hitting a stale server running OLD code
      (symptom: brand_images came back null instead of []). Killed via
      `taskkill //F //PID`, re-verified against the fresh process, port
      confirmed clear after. Lesson recorded in memory: after restarting
      the app on Windows, confirm via netstat WHICH pid owns :5010 before
      trusting any live verification against it.
- [x] Operational catch #2: brand_discovery_seen.json had been polluted by
      this session's tests — it contained the 2 seed URLs + 6 web-search
      finds whose candidate rows only ever landed in throwaway test DBs,
      so the REAL first weekly run would have silently skipped all of
      them. Ledger reset to empty so the real run can use everything.

## Contact discovery + department mix (2026-10-02)

- [x] **Facebook search was dead code** — `deep_whatsapp_search`'s FB step
      used plain `requests`, which facebook.com answers with HTTP 400 and a
      1.5 KB stub, so it never once contributed a number. Now rendered via
      Playwright (`_render_page_text`), which works even logged out:
      tested on facebook.com/mazeted it returned BOTH "+94 77 066 6293" and
      "colourme@getmazeted.com" from the public intro block. Also added the
      FB `/about` tab. **Measured effect on the live batch**: MAZE 1 -> 3
      emails, D'Las 1 -> 3, Mimosa 1 -> 2.
- [x] Instagram: attempted but **confirmed to yield nothing** (renders the
      profile title, everything else behind the login wall). Kept as a
      1-fetch attempt on candidates that have no number anyway; do NOT
      claim it as a working source.
- [x] **Stage 4: vision pass** (`scan_images_for_contacts`) — reads numbers
      printed INSIDE banner/contact graphics via OpenAI vision, through the
      project's shared `openai_cost_tracker`. Only runs when every text
      stage found no phone, capped at 4 images/business. Three real bugs
      found and fixed while building it, each of which produced a SILENT
      wrong answer:
        1. Passing CDN URLs -> `LOAD_FAILED` (Shopify hotlink protection),
           indistinguishable from "no number in this image". Now downloads
           the bytes locally and inlines base64.
        2. RGBA PNGs rejected by the API -> flattened to RGB JPEG.
        3. **gpt-4o-mini CORRUPTED DIGITS** on smaller text (fed
           "+94 77 123 4567", got back "+477 123 4567"). Pinned to gpt-4o,
           which transcribed it exactly at both detail levels. A wrong
           number means outreach to a stranger, so this step pays for the
           better model. Cost ~$0.002-0.01/business.
- [x] **Department mix** (user: "make sure weekly found is mix of
      categories / now it 4 - jewellery, 3 - clothing etc"). Root cause:
      ONE open-ended web search with no category constraint — a single call
      has no reason to spread itself, so whichever vertical surfaced first
      dominated. Two layered fixes:
        1. `_web_search_for_brands` now searches **per department** (one
           call each, `per_dept` cap, department-specific vocabulary in
           `_DEPT_SEARCH_HINTS`, shuffled order so the same ones don't lead
           every week) instead of one open call.
        2. `source_brand_discovery` **round-robin interleaves** the final
           pick by the department judge_brand_fit actually assigned — no
           department takes a 2nd slot until every other has had a 1st.
           Needed as a second layer because seed URLs carry no department
           and the judged department can differ from the searched one.
           Verified against the user's exact reported skew: 4/3/1/1 ->
           3/3/1/1 with every department represented in the top 8.

## Daraz gap data feeds the search + speed fix (2026-10-02)

- [x] **Per-department web search was sequential, not parallel** — a live
      test with the new department-split search (previous entry) timed out
      past 10 minutes on just 3 departments. Fixed: `_web_search_for_brands`
      now runs all department searches concurrently via `ThreadPoolExecutor`
      (each `run_claude_cli_for_search` call is a blocking subprocess, which
      releases the GIL, so threads genuinely overlap). Re-measured live:
      3 departments, 272s total (vs. not finishing one department in 600s
      sequential) — found 2/3 departments returned a real candidate
      (stone-n-string.com, thirteen31.com), the 3rd came back empty, which
      is an acceptable fail-open outcome, not a bug.
- [x] **Daraz/Kapruka verdict data now feeds the web search** (user: "how
      about to daraz kapruka verdict data to find?"). Investigated first
      whether it could name actual BRANDS to approach — confirmed it can't:
      daraz_categories carries category-level demand/gap data with no
      seller name anywhere, and even product_examples (scraped product
      titles) carry no seller identity either. What it CAN do: tell the
      search exactly WHAT has proven demand and zero Kapruka presence,
      turning a generic ask ("find a jewellery brand") into a concrete one
      ("find a brand selling men's digital watches — Kapruka has zero").
      `_daraz_gap_hints_for_department()` pulls the top category_gap rows
      per department (umbrella-row and accessories/parts exclusion reused
      from sourcing.py's same fixes), and feeds up to 6 real category names
      into each department's search prompt as a "Kapruka specifically has
      NO products in these proven-demand categories" block.
      Deliberately does NOT run the full worth_pursuing LLM gate on these
      hints (would add a second Claude CLI call per department on top of
      the search call, worsening the timeout risk just fixed) — confirmed
      live the raw top-by-revenue gaps are still commodity-heavy (Coffee
      Grinders, Ladders & Workbenches survive for home_lifestyle), but this
      is just PROMPT VOCABULARY, not a selection filter: judge_brand_fit
      still does the real giftable/niche/not-commodity judgment on whatever
      business the search actually finds, so a commodity-sounding hint
      slipping through here costs nothing but a slightly less sharp search,
      never a bad card reaching the human reviewer. Pure local SQLite query,
      cached per-process, adds no extra CLI call or runtime.

## Multi-category "golden target" clustering (2026-10-02)

User, with a real screenshot of automobile Daraz gaps (Lighting & Electrical,
Cooling & Water Pump Tools, Exterior Accessories, Wheel Accessories Parts,
Glass Care, Cleaning Supplies, Brake System, etc.): "see one company website
might be selling many of these sub categories so those are golden targets" —
landing ONE real multi-line retailer fills several Kapruka gaps in a single
onboarding, which is a much stronger target than chasing each gap as its own
separate single-category search.

- [x] `cluster_daraz_gaps()` — groups a department's real Daraz gaps into
      "one real retailer could plausibly stock all of these together"
      clusters, same semantic-judgment pattern as dedupe.py's
      dedupe_same_business_line (real-retail-business question, not a
      word-match), but returns the clusters themselves (dedupe.py only
      returns one deduped survivor per cluster, which isn't what's needed
      here). Ordered biggest-cluster-first. Fail-open: any error degrades
      to one cluster per category, same as the old flat-list behavior.
      **Verified live** against the REAL automobile gap data from the
      user's own screenshot: correctly grouped a 9-category diagnostic/
      mechanical-tools cluster, a 6-category car-care/garage cluster, a
      4-category car-video/infotainment cluster, and a 3-category
      motorcycle-gear cluster — 54s for the clustering call.
- [x] `_web_search_for_brands` now searches for the BIGGEST cluster per
      department, not a flat list of independent gap names — the prompt
      explicitly asks for "ONE business that covers several of these over
      several businesses that each cover one."
- [x] Found and fixed a real contradiction this surfaced: the search
      prompt's "GOOD examples" block (Sokk Republic, MAZE Socks — niche/
      giftable lifestyle brands) was being sent ALONGSIDE a request to find
      a 5-category auto-parts/diagnostics specialist, which is a
      functional/practical-purchase business, not a giftable one — sending
      contradictory signals in the same prompt. Fixed: when the best
      cluster has 2+ categories, the example block swaps to explicit
      multi-line-specialist framing instead of the lifestyle-brand
      examples. Same fix applied to judge_brand_fit's fit-judgment prompt
      (added a line explicitly covering a multi-category specialist under
      Kapruka's "ECOM SELF-PURCHASE, practical considered purchases" side,
      not just the gifting side) so a real find doesn't get rejected later
      in the pipeline for failing a "giftable" bar it was never meant to
      clear.

## Source priority redesign (2026-10-02)

User, after seeing the yield problem in the real fresh-generate test (15
candidates found, only 2 survived to the final card list — under
investigation, root cause not yet confirmed when this landed): "no no what
i want is business to surface the tool but sources should be discovered
self, second from opportunity tool verdicts, and from seed brands got it" —
then clarified directly: self-discovery and Opportunity-Tool-verdict-driven
search are EQUAL top priority (not one feeding the other), seeds are "an
idea i gave u", i.e. supplementary, not the primary quota-filling source.
This reverses the OLD priority entirely (seeds filled the quota first, web
search only ran for whatever was left).

- [x] **`_open_web_search_for_brands()`** — new, Stream A: a genuinely
      OPEN search with NO department/category steering at all ("ANY product
      category, no restriction... do not confine yourself to any one
      department"). Deliberately the opposite of the structured
      per-department search — lets the model surface whatever it finds
      interesting. Explicitly allows non-giftable specialty retailers too
      (not just lifestyle/gifting brands), matching the earlier
      multi-category "golden target" fix.
- [x] **`source_brand_discovery()` rebuilt**: Streams 1 (open) and 2
      (`_web_search_for_brands`, the existing Opportunity-Tool-verdict/
      gap-clustered search) now run CONCURRENTLY in a 2-worker thread pool,
      each targeting half the week's quota — equal priority, equal weight,
      neither blocks the other. Seeds (`BRAND_SEEDS.txt`) moved to LAST:
      read only to top up whatever room remains under `limit` after both
      searches, never displacing a search result, never gating whether
      search runs (old behavior: seeds filled the quota FIRST, search was
      the fallback only for the shortfall — exactly backwards from what the
      user wants).
- [x] `web_search_share`'s default flipped 0.5 -> 1.0 everywhere it's read
      (api_weekly_generate's request param, `_run_weekly_generate`,
      `source_brand_discovery`) — its meaning is now "how much of the
      SEARCH-discovered pool can be used" rather than "how much of the
      total quota search is ALLOWED to fill before seeds take over", since
      seeds no longer compete with search for priority at all.
- [x] **Caught a real new timeout the two-stream restructure introduced**:
      first live test of `source_brand_discovery(limit=4)` timed out at
      320s with the seen-ledger unchanged, meaning it never even reached
      `_mark_seen()` — the search streams themselves didn't finish. Root
      cause: `_search_one` (inside Stream 2) called `cluster_daraz_gaps`
      (a separate ~54s Claude CLI call) THEN waited for it to finish before
      even starting its own up-to-240s search call, per department,
      SEQUENTIALLY within each department's own thread — worst case ~294s
      per department thread, which the outer department-level parallelism
      doesn't fix since every thread pays this cost independently.
      Fixed: ALL departments' clustering now runs in its own parallel batch
      FIRST (a `cluster_cache` dict, `ThreadPoolExecutor` sized to
      `len(depts)`), fully before any search call starts — clustering and
      searching are independent work, so there's no reason to serialize
      them per department. `_search_one` now just reads the pre-computed
      cluster_cache instead of calling cluster_daraz_gaps itself.
      **Confirmed live**: old sequential version timed out past 400s even
      on a small n=2 test; flattened version completed the same test in
      188s with 2 real candidates (Huskies Global, Edge Casual). Full
      `source_brand_discovery(limit=4)` end-to-end (both streams + fetch +
      sitemap check + fit judgment + interleave) completed in 360s with 2
      final candidates (CarTek, Vishmitha Car Audio, both automobile via
      Stream 2) — real businesses, correct shape, acceptable runtime for a
      `limit=4` test. Both survivors landing in the same department at this
      sample size is NOT re-confirmed as a mix bug — only 2 brand-new URLs
      were actually found this run (everything else already in
      brand_discovery_seen.json from prior test runs), so this is a
      small-sample artifact, not evidence the interleave fix broke; a
      proper mix check needs a realistic limit=10 run, not yet done.

## Project-wide JSON-parsing bug found and fixed (2026-10-02)

Investigating why Stream 1 (`_open_web_search_for_brands`) returned 0
candidates on a live test uncovered a bug affecting EVERY Claude-CLI
JSON-parsing call site in kogul/, not just the new code.

- [x] **Root cause**: every JSON-parsing call site used
      `re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.MULTILINE)`
      before `json.loads()` — this only strips a fence at the ABSOLUTE
      start/end of the string. Any explanatory prose before the fence (a
      real captured response began "All three check out as genuine
      independent operations... Here's the result: \`\`\`json...") defeats it
      completely, `json.loads` raises, and every call site's fail-open
      except-block silently returned an empty/unfiltered result with NO
      visible error anywhere — indistinguishable from "the model genuinely
      found nothing."
      **Confirmed this exact failure live**: captured the raw CLI output
      for a Stream 1 open search that LOOKED like 0 results but actually
      contained 3 genuine, well-formed candidates (Bubbles & Beyond,
      Pet Shopper, Hansa Ceylon) sitting right after one sentence of prose.
- [x] This ONE bug affected **7 call sites across the whole Kogul
      pipeline**, not just brand_discovery.py — `worth_pursuing.py`,
      `specialization.py`, and `dedupe.py` (the category pipeline's own
      core semantic gates) all copied the identical fragile pattern. Any
      of their fail-open "kept unfiltered" results going forward may have
      actually been silent parse failures rather than genuine CLI errors —
      impossible to distinguish after the fact from past runs, but the
      fix applies going forward.
- [x] Fixed with one shared `claude_cli.extract_json(raw)` — tries the old
      exact-anchor behavior first (cheap, handles the common case), then a
      non-anchored search for a \`\`\`json fence ANYWHERE in the string, then
      falls back to the outermost `[...]`/`{...}` span in the raw text.
      Raises the same way `json.loads` would on genuine failure, so every
      existing fail-open except-block keeps working unchanged — a drop-in
      replacement for `json.loads(cleaned)`, not a new error path.
      Applied to all 7 call sites (brand_discovery.py x4, dedupe.py,
      specialization.py, worth_pursuing.py). **Verified live**: the exact
      captured failing response now parses to all 3 real candidates; the
      simple no-preamble-fenced case and plain unfenced JSON both still
      parse correctly (no regression on the common case).

## Category gaps are no longer their own weekly card (2026-10-03)

- [x] User flagged a live screenshot (from the Opportunity Tool / Kogul
      weekly feed) showing bare category-gap rows (Folk Wood Percussion,
      Climbing, Fishing, Outdoor Lighting, Waist Packs) still rendered as
      standalone cards with their own "Approve → source partners" button —
      "daraz kapruka verdict should be the source to find new brands to
      expand or add that category got it", i.e. a category gap must ONLY
      ever be search fuel, never an actionable card of its own.
- [x] Root cause: `_run_weekly_generate` still pulled a SECOND pool
      (`sourcing.top_category_opportunities` + `worth_pursuing` gate +
      `dedupe.dedupe_same_business_line`) alongside `brand_pool`, merged and
      ranked both together, and took the top `num_cats`. Brand rows
      (score 4.5) normally win every slot, but whenever brand_discovery
      didn't find enough candidates for the week, the leftover slots
      silently fell back to raw category rows — exactly the old
      Daraz-gap-as-partner-source behavior the brand-discovery redesign was
      supposed to have replaced.
- [x] Asked the user what should happen to a slot brand discovery can't
      fill: chose "keep searching with that gap as a hint, don't fall back
      to a card" over leaving the slot empty or showing a read-only note.
- [x] Fixed in two places:
      1. `brand_discovery.source_brand_discovery` now retries internally
         (up to 3 rounds, default `max_rounds=3`) via a new
         `_source_brand_discovery_round` helper — each round searches both
         equal-priority streams again (seen-URL exclusion naturally pushes
         toward new ground), widening the gap-driven stream's per-department
         ask (2 → 3 → 4) each retry, until `limit` businesses are found or
         rounds run out. Called with `limit=num_cats` directly now (was
         `max(num_cats, 3)` when it had a category-row fallback to lean on).
      2. `app_kogul.py`'s `_run_weekly_generate` no longer pulls, scores, or
         dedupes a category pool at all — `brand_pool` is the entire
         candidate set; nothing but a `source='brand_discovery'` row is ever
         inserted into `weekly_categories` going forward.
      Dead code removed as a result: `sourcing`/`worth_pursuing`/`dedupe`/
      `metrics` imports, `_max_live_product_count`, `CORE_DEPARTMENTS`,
      `_NON_CORE_MAX_LIVE_PRODUCT_COUNT`, `_score_candidate`, `math` import
      — all were only reachable from the now-removed category-pool path.
      The old "Approve → source partners" template branch in kogul.html is
      left in place (dead for new rows, but still renders any OLDER batch
      still in the DB that has real non-brand rows from before this fix —
      batch 11 confirmed still renders correctly after the change).
- [x] **Verified live**: restarted the server (confirmed no in-progress job
      first, after last session's mid-job-kill mistake), confirmed
      `/api/weekly/current` still serves batch 11 (the pre-existing 18-card
      batch) with no crash — the removed functions/imports were genuinely
      unreferenced by anything needed to render historical data.
- [ ] NOT yet live-tested: a full fresh `/api/weekly/generate` run exercising
      the new multi-round retry path end-to-end (i.e. a run where round 1
      comes up short and round 2/3 actually fire) — the verification above
      only confirms the code loads and old data still renders, not that the
      retry logic itself behaves correctly under a real shortfall.

## Weekly generate tops up instead of blocking/replacing (2026-10-03)

- [x] User: "i dont want u to loose currently whats found keep that in the
      list until it gets rejected" — before this, running Generate a second
      time for a week that already had a batch just errored out
      ("batch already exists"), with no way to add more businesses to an
      existing week's list.
- [x] Asked what should happen to a slot brand discovery can't fill (empty /
      retry harder / read-only note); chose "keep searching with that gap as
      a hint, don't fall back to a card" for the earlier category-row fix,
      and for THIS fix chose "Generate adds more, never replaces."
- [x] Fixed: `api_weekly_generate` now detects an existing batch for the
      week and, instead of erroring, computes how many MORE businesses are
      needed (`num_cats` minus the count of non-rejected rows already in
      that batch) and runs a "top-up" job — `_run_weekly_generate` gained an
      `existing_batch_id` param that skips creating a new `weekly_batches`
      row and inserts straight into the existing one. No existing row is
      ever touched (no delete, no status change, no re-insert); a
      previously-rejected business can't silently reappear because
      `prior_keys` (passed to `source_brand_discovery` as
      `exclude_business_names`) already spans every concept ever stored for
      ANY batch regardless of status.
- [x] **Verified live, full round-trip**: batch 11 started at 18 live
      businesses (num_cats=20) → called Generate again → correctly routed to
      top-up (`top_up:true`, `requested:2`, same `batch_id`) instead of
      erroring → job completed, inserted exactly 2 → confirmed via
      `/api/weekly/current` all 20 original+new rows present, all
      `pending_review`. Then rejected the 5 leftover pre-fix category rows
      (see the entry above) and ran Generate a third time → correctly
      requested 5 → completed → final state 25 total rows (20 live
      `brand_discovery` + 5 preserved `rejected` `daraz_new_category_gap`),
      exactly as designed.
- [x] Frontend status message reworded ("Done — N business(es) added/found")
      to distinguish a top-up from a fresh generate rather than reusing the
      old "N categories picked" wording left over from the dual-pool era.

## Which search stream found each business is now visible (2026-10-03)

- [x] User, looking at the week's businesses: "majority of the brands are
      from brand discovery right i dont see brand discovery brands based on
      daraz kapruka verdict data - opportunity tool" — i.e. no way to tell,
      from the card or the data, whether the gap-driven stream (sharpened by
      real Daraz/Kapruka category-gap data from the Opportunity Tool) was
      contributing anything, versus the open self-discovery stream quietly
      finding everything.
- [x] Root cause: `found_via_web_search` is set True for candidates from
      EITHER search stream — it only ever distinguished "came from search"
      vs. "came from BRAND_SEEDS.txt", never WHICH stream. The actual
      per-stream origin (`search_department` on gap-stream results) was
      computed inside `_web_search_for_brands` but discarded immediately —
      `source_brand_discovery`'s candidate-building loop only ever pulled
      `f["url"]` out of the search results, dropping every other field.
- [x] Fixed: `_source_brand_discovery_round` now builds a
      `url -> "open" | "gap:<department>" | "seed"` map right after both
      streams return, and stamps it onto each fetched site dict as
      `discovery_source`. That rides through `judge_brand_fit` untouched
      (it builds its output via `{**f, ...}`, so any extra key on the input
      dict survives) all the way to the stored row. New DB column
      `discovery_source` added via the standard `_migrate()` pattern. UI
      shows a distinct badge: "Daraz/Kapruka gap search (department)" (blue)
      vs. "open self-discovery" (amber) vs., for rows stored before this fix
      with no recorded source, an honest "found via web search" rather than
      guessing which stream it was.
- [x] **Verified the plumbing statically end-to-end** (search →
      discovery_source map → fetched site dict → survives judge_brand_fit's
      dict spread → final row dict → DB insert → DB column → API response →
      UI badge) — grepped every call site to confirm the key is never
      dropped or renamed along the way.
- [ ] **NOT yet verified with a real live search** — the current batch (11)
      is already full at its 20-business quota, so no top-up ran after this
      fix landed. The next time Generate actually finds a NEW business via
      the gap-driven stream, confirm its card shows the blue "Daraz/Kapruka
      gap search" badge (not the amber "open self-discovery" or gray
      "found via web search" one) — that's the real proof this works, not
      just that the code is wired correctly.

## UI redesign — "this aint google enough" (2026-10-03)

- [x] Full Material 3 / Google product pass across every screen (not just
      the brand cards): Google Sans (falling back to Segoe UI/Roboto, since
      Google Sans/Product Sans aren't web-licensable) for all headings and
      the business-name headline, Roboto kept for body copy — matches
      Google's own pairing instead of one font doing both jobs.
- [x] Real Material elevation scale (`--e1`..`--e4`, Google's own shadow
      values) replacing the old flat 1px-border-only card style — cards and
      the brand-card specifically now lift on hover with a genuine shadow,
      not just a border-color change.
- [x] App bar now lifts with a shadow only once the page actually scrolls
      (Gmail/Drive do exactly this — flush with the page at rest), via a
      `.scrolled` class toggled by a `window scroll` listener, instead of a
      permanent static border.
- [x] Status badges redesigned to lead with a small dot (Gmail label /
      Drive sharing-state convention) instead of filling the whole chip with
      saturated color — reads calmer with several badges side by side.
      Purely-informational tags (department, source, priority score) split
      into a separate `.badge.tag` style (outlined, no dot) so they don't
      visually compete with real pipeline STATUS badges.
- [x] Brand cards restructured from one dense run-on block (name, 6+ badges,
      links, and paragraphs all interleaved in a single toolbar div) into
      clear sections: `.brand-meta-row` (status/tags on their own line) →
      `.brand-headline` (the name, now genuinely the visual lead) →
      `.brand-sub` (links) → `.brand-section` label/value blocks → contact
      chips → a separated `.brand-actions` bar at the card's bottom edge
      (tonal background, border-top) instead of Approve/Reject crammed into
      the same row as the business name.
- [x] Dashboard, Settings, and Partner Central Settings pages given a real
      `.page-heading` (title + subtitle) instead of either no heading
      (Categories) or a heading buried inside an otherwise-empty bordered
      card (Dashboard's old "This week" card-around-a-title).
- [x] Settings screens rebuilt from bare stacked `<label>+<input>` pairs
      into `.setting-row` blocks (title + description on the left, control
      on the right, divider between rows) — the same layout pattern Google's
      own Account/Workspace admin settings pages use, and it also means
      every setting now carries a one-line explanation of what it does
      (several had none before).
- [x] Chat screens (`Chat with PC Agent`, `Partner Chats`) given a proper
      `.chat-card`/consistent structure instead of ad hoc inline
      `style="height:70vh;display:flex..."` attributes repeated at the call
      site.
- [x] **Verified live**: restarted the server after each change batch
      (checking for in-progress jobs first both times), confirmed
      `/api/weekly/current` keeps serving the existing 25-row batch with no
      crash, confirmed JS syntax via a Node parse check (no headless browser
      available in this environment).
- [ ] **NOT yet visually reviewed in an actual browser** — all verification
      so far is "the code is syntactically valid and the API still returns
      data," not "a human looked at the rendered page." The user should open
      the app and confirm the redesign actually reads as intended before
      this is marked fully done.

## Open items / not yet built (carried from earlier, still true)

- [ ] Real WhatsApp send (Meta Cloud API via `notify_whatsapp.py`) — swap-in point documented in `_log_message`'s docstring
- [ ] Real WhatsApp inbound webhook — none exists anywhere in this project yet, explicitly deferred by user
- [ ] Automatic "unresponsive after N days" — `follow_up_days` setting exists and is editable but nothing reads it yet; all status changes are currently manual
- [ ] VPS deployment — built/tested locally only; `OPPORTUNITY_TOOL_URL` hardcoded to `127.0.0.1:5002`
- [ ] `already_partner_flag`'s "uncertain" threshold (0.4 word-overlap) is fairly loose — live-tested showed one weak false-lead (Caravan Fresh -> nature-fresh); acceptable under the fail-open philosophy (flag costs a glance, silent drop costs a real opportunity) but worth revisiting if false-flags prove noisy in real use
- [ ] Brand discovery has not yet been clicked through in an actual browser (Approve -> reach out button, card layout) — verified via direct DB/route/pipeline calls only so far

## 2026-10-13 — full-app audit fix batch (user: "can u fix everyhing")

Security
- [x] A1 Auth gate: non-loopback requests need HTTP basic auth (KOGUL_ADMIN_PASSWORD in .env); webhook exempt; no password = remote blocked
- [x] A2 CSRF: reject cross-origin POST/DELETE (Origin/Referer mismatch)
- [x] A3 Webhook fails closed when META_APP_SECRET empty
- [x] A4 Restart PIN from env, not hardcoded "1111"
- [x] A5 Claude CLI hardened (no tools, empty cwd)
- [x] A6 MAX_CONTENT_LENGTH
WhatsApp send path
- [x] W1 needs_handover + terminal status gate inside _do_partner_auto_reply (webhook path)
- [x] W2 One _send_and_log(): live mode really sends (template for cold opener/nudge), records simulated=0 + wa id or failure
- [x] W3 Human "Send" really sends in live mode; needs_handover only cleared on success
- [x] W4 Webhook dedupe on Meta message id; return 200 fast, process in worker; per-partner lock
- [x] W5 Scheduler timers measured from first unanswered outbound (not reset by nudge); keep old number after switch
- [x] W6 Reply-generation failure / unknown number -> PC team alert
- [x] W7 Non-text inbound (reactions/status) don't trigger a bot reply
Agent logic
- [x] L1 {category} in opening = products/department, not business name; safe template fill
- [x] L2 Structured JSON output (reply/handover/agreed/not_agreed), shared parser for real + sim; unparseable -> hold for human
- [x] L3 AGREED action + hot-lead alert
- [x] L4 Status revival (unresponsive->replied on reply; not_agreed reopens on positive)
- [x] L5 Prompt: pricing/payments contradiction, partner text treated as data, recent-turn cap
- [x] L6 Nudge in partner's language
- [x] L7 Simulation uses brand-discovery context
Sourcing
- [x] S1 Mark seen only after judging, overflow fits kept
- [x] S2 Domain/name normalization in dedupe + phone check vs partners
- [x] S3 Dept prioritization: recent window, no permanent starvation
- [x] S4 Reject reason required; Cancel aborts; ORDER BY fixed
Settings/Dashboard
- [x] D1 Show real alert recipients + warn when none
- [x] D2 Confirm dialogs on Approve/Remove/Mark*
- [x] D3 Partner chats auto-refresh
- [x] D4 jget checks r.ok; per-tab try/catch; null guards
- [x] D5 safeUrl() for hrefs
- [x] D6 Sim export re-fetches; labels fixed; dead CSS/comments removed
- [x] D7 Live/simulation mode banner

Verification (2026-10-14)
- [x] Offline harness: 56/56 behaviour checks pass twice in a row, against a COPY of
      kogul.sqlite with OpenAI/email/WhatsApp replaced by fakes (no real service touched):
      access guard, CSRF, webhook signature/dedupe/burst/receipts/reactions/unknown
      number, handover + agreed gates, human send success/failure, cold-window
      template rule, unreadable-output hold, AI-down hold, status revival,
      20h/24h/72h timers (nudge no longer resets the clock), simulation parity.
- [x] Every tab loaded headless: 0 JS errors, no tab-error boxes, no horizontal
      scroll at 390px width.
- [x] Real gpt-4o, 2 turns in a throwaway simulation (deleted after): JSON contract
      followed, opener says "Home & Lifestyle sellers" (was the business name),
      Singlish question answered in Singlish, first step only.
- [x] Claude CLI: proved the OLD flags could read a file in its cwd (real hole);
      with --tools "" + empty strict MCP config it cannot, and still answers;
      the search variant still searches.
- [x] Found + fixed during verification: `[hidden]` was overridden by .tab-badge,
      so Partner Chats showed an empty red badge at count 0.

Still needs the user (cannot be done from here)
- [ ] Set META_APP_SECRET (Meta App Dashboard → Settings → Basic). Until then the
      webhook refuses every inbound message (fails closed, by design).
- [ ] Set KOGUL_ADMIN_PASSWORD in the VPS .env, or the VPS copy is unreachable
      from outside that machine (also by design). Optionally KOGUL_RESTART_PIN.
- [ ] Register an approved WhatsApp utility template for cold outreach and put
      its name in PC Agent settings → WhatsApp sending; then turn Live sending on.
- [ ] Deploy to the VPS (nothing from this batch is deployed yet).
- [ ] Not changed on purpose: Flask dev server kept (waitress isn't installed);
      jobs still live in memory (now pruned after 1h) — a restart still loses a
      job that is mid-run.

## 2026-10-14 — Expand Catalog (thin categories) as a brand-search hint
User: "yes add that" — after confirming brand search only used New Categories
(status='category_gap') and never Expand Catalog.
- [x] Per-department thin-catalog hint (Kapruka page exists, <10 products, Daraz
      demand) added to the gap-driven search prompt as a non-blocking bonus,
      same pattern as the missing-items / GA4 blocks
      (brand_discovery._thin_catalog_hints_for_department + expand_block)
- [x] Same rows shown as demand evidence on business cards (source 'daraz_thin',
      new "Thin Kapruka page, Daraz demand" group in renderDemandEvidence, links
      to our thin page)
- [x] Found while building: the Daraz->Kapruka page join behind Expand Catalog
      crosses departments wrongly (automobile Mirrors/Sun Protection -> cosmetics,
      toys Monitors -> computer monitors, Cheese -> cheesecakes). Filtered here by
      URL-section -> department map; the join itself (Opportunity Tool's Expand
      Catalog tab shows the same wrong pairs) is NOT fixed.
- [x] Verified against real data_store.sqlite: 108 raw rows -> 39 kept across 11
      depts (automobile 0, gift_sets 0); real electronics prompt rendered with
      Claude stubbed shows the block; attach_demand_evidence returns 2 daraz_thin
      rows; page JS passes node --check; server restarted (was idle), serves new page.
- [ ] Existing cards keep their old evidence (backfill only fills EMPTY evidence);
      new rows only appear on businesses found from the next generate onward.
- [ ] Not yet seen rendered in a browser with a real card.

## 2026-10-14 — PC team testing: flag a reply in Simulate -> rule -> auto-applied
User: "give this to partner central team to test ... simulate as a partner - they
should be able to test it and add rules or define rules if any problem is detected
that will be automatically included the chatfile or anyfile"
- [x] agent_rules table (rule, problem, flagged reply, sim id, added_by, active)
- [x] Active rules injected into EVERY prompt: real partner replies, Simulate,
      nudges, Chat with PC Agent — brain.read_brief() = KOGUL.md + team_rules_block(),
      read fresh each message. KOGUL.md file itself never modified.
- [x] Simulate: "Flag this reply" under each PC Agent reply -> what went wrong +
      rule ("Draft a rule for me" = gpt-4o suggestion, editable) -> save. Opening
      message shows "edit it in Settings -> Opening message" instead (fixed template).
- [x] "Save rule & retry this reply" -> /api/simulations/<id>/retry deletes that
      reply + everything after, clears handover/outcome, regenerates with rules
- [x] Rules list on the Simulate tab: on/off, edit, delete, who/when/from which
      test/problem/flagged reply. Tester name kept in localStorage.
- [x] Verified: 12/12 HTTP-level checks with real gpt-4o on a throwaway scenario
      (before: no sign-off; rule saved; after retry: reply ends with it; rule in
      partner/nudge/chat prompts; paused rule leaves prompts; bad retry refused;
      cleaned up). Headless browser desktop + 390px: flag form, opening note,
      rules list render, 0 JS errors, no horizontal overflow. Fixed: AI draft
      note came back as the word "empty".
- [ ] Not on the VPS yet — PC team can't reach it until deployed (+ KOGUL_ADMIN_PASSWORD).

## 2026-10-14 — 20h nudge only when the WhatsApp window is about to close
User: "20 hours: a polite nudge - only if the conversation window is about to end"
- [x] Nudge fires only if the partner has messaged us and their last message is
      20h-23.5h old (window still open, closes at 24h). No nudge on cold outreach
      the partner never answered; none once the window has already closed.
- [x] 24h second-number switch + 72h unresponsive unchanged
- [x] UI/brief text describing the nudge updated (Settings follow-up note + KOGUL.md step 5)
- [x] Verified: 6/6 scheduler cases on a DB copy, sends/AI stubbed (cold 21h no nudge; partner
      wrote 21h ago nudged; wrote 25h ago no nudge; wrote 10h ago no nudge; 2nd-number switch
      and 72h unresponsive unchanged). Real DB untouched; server restarted.

## 2026-10-14 — nudge only when the chat stalled midway with no direction/decision
User: "only nudge if the conversation is halted in midway, not for every" + "when theres
no clear direction or decision" (chose: "Only when we're waiting on them").
- [x] At the 20h point one gpt-4o call judges the transcript: nudge only if it stalled
      midway with no decision and no stated next step/timeline; same call writes the
      nudge (picks up where it stopped, partner's language, team rules applied)
- [x] No nudge -> system note with the reason, stage 'nudge_skipped' (no re-check every
      10 min); AI failure -> no nudge (a wrong nudge reaches a real business)
- [x] 24h switch / 72h unresponsive behaviour unchanged
- [x] Verified: real gpt-4o, 6 transcripts x 3 runs + 2 scheduler cases on a DB copy (sends stubbed):
      stalled-on-question / Singlish-stalled / stalled-mid-steps -> nudged; 'get back next week' /
      clear no / 'I'll register myself' -> skipped with reason note. Fixed during testing: nudge
      repeated our last message verbatim; then copied the prompt's example; Singlish chat got an
      English nudge -> example removed, strict partner-language rule. Final 2 runs 8/8 each.
      User also asked "depending on the context change the nudge message" -> covered (no fixed text).

## 2026-10-14 — "Log a partner reply" only in simulation mode
User: "during simulation have that option log a partner reply, and during live chat with
actual partner i dont want"
- [x] Partner Chats: dropdown hidden when Live sending is on; shown (as now) when off
- [x] Server: /api/partners/<id>/reply_from_partner refuses (403) in live mode; the real
      WhatsApp webhook path is unaffected
- [x] Verified 7/7: browser sim shows / live hides the dropdown, no JS errors; server 403 + nothing recorded in live, still works in sim (DB copy). Human takeover toggle untouched, present in live mode (user: important).

## 2026-10-14 — Conversation brief flow + Partner report
User: opening -> if interested, request a brief (products, how many products, location)
-> Kapruka search of their product types -> auto-reject if only <=5 SKUs (actual SKUs, not
categories). Plus a Partner report per partner: live pipeline status, about, SKU count,
does Kapruka sell it (live search, words from their website), location (Colombo? if not,
consignment?), lucrative-partner verdict, per-partner questions (AI-suggested, Approve /
Reject — user chose "Per-partner questions"), add own questions, AI analysis of the chat,
click to open the chat. Visually appealing.
- [x] Brief questions: default list (Settings-editable) + approved per-partner questions,
      injected into the reply prompt; asked one per message after interest is shown
- [x] Auto-reject: <=5 individual products -> not_agreed with reason, polite close
- [x] KOGUL.md conversation-flow section updated to match
- [x] Store SKU count from the site itself where possible (Shopify products.json,
      WooCommerce Store API) — real count, never guessed
- [x] Live Kapruka search per product type (smart_live_search, one at a time, paced)
- [x] Profile extraction (gpt-4o): about, product types, stated SKUs, location, Colombo,
      consignment, answers to every question, chat analysis
- [x] Lucrative verdict (deterministic from gap + location + SKUs, reasons shown)
- [x] Per-partner questions: AI suggests up to 3, Approve/Reject, add own, delete
- [ ] Report UI: button on approved business cards + Partner Chats; modal; status stepper;
      auto-refresh while open; open-chat button; phone width OK
- [ ] Verified end to end
- [ ] Card evidence (Weekly Outreach): GA4/Daraz rows now product-type matched (relevant_store_evidence)
      for NEW cards. Rewriting the 69 existing cards was prepared but the user declined running it
      (backup data/kogul.sqlite.bak-before-evidence-fix-20261014 made, nothing written).
- [x] FIX: report sections invisible — global `section {display:none}` (tab switcher) hid them; use div
- [x] Report BEFORE outreach (user): keyed to the business card, not the chat partner; on pending
      cards too; Approve/Reject from inside the report
- [x] Location read from their website (footer/contact address), Colombo District check
- [x] Kapruka coverage spelled out per product type, not "3/4"
- [ ] Partner brief section: every brief item + where it came from (website / store / chat)
- [x] Verified: brief order + auto-reject (4 soaps) + "3 categories" not rejected, 2 runs x 3 chats real gpt-4o.
      Found: "3 categories" WAS auto-rejected first -> prompt fix + code guard (brain.guard_auto_reject).
- [x] Kapruka check judges THEIR KIND (user: "we have soap but not this kinda soap"), ranges from their own
      shop collections; Mineral Earth -> Lucrative (soap: 3 of their kind; foot soak/lip balm/hair wax: none).
- [x] Report queue builds every pending card's report in the background; approve refused (409) until built.

## 2026-10-14 — Brief first, call booking CTA, brand value
User: "Brief collected ... this should be the first step and then approve and opening message sent";
"call to action is to arrange a call with the team ... 3 slots ... 11 to 4 ... else tomorrow or next week
... along with the time and name send email and whatsapp notification"; "check for brand value ...
followers they have - socials, traffic they get on their website"; Nuts on Top location blank.
- [x] Pipeline: Found -> Brief collected (research) -> Approved -> Opening sent -> Replied -> Call booked -> Decision
- [x] Reply prompt gets facts already known from the report; PC Agent doesn't re-ask them
- [x] Call slots: 3, Sri Lanka time, 11:00-15:00 starts, working days; today if before 4 PM else next
      working day (Mon after Fri 4 PM / weekend). "agreed" requires a chosen slot (+ contact name)
- [x] Booking stored on partner/simulation; email + WhatsApp alert carry slot + name
- [x] Brand value + location via web search (followers, traffic if public, press/stockists, location), sourced
- [x] Verified: call slots across Wed 09:30/11:20/13:40/14:30/17:00, Fri 15:10/18:00, Sun; CTA chats real
      gpt-4o 2 runs x 4 = 8/8 after fixes. Found + fixed: model booked "Today 2:00 PM" on a plain "yes I want
      to join" (code guard: slot must be named in the partner's message); Nugegoda treated as outside Colombo
      (Colombo District list in prompt); "don't ask" note ignored (answered questions now dropped in code).
      Alerts carry slot + name (stubbed send on DB copy). Web research: Nuts on Top -> Colombo 04, verified
      by same phone across site/Facebook/UberEats; Mineral Earth -> 2,183 IG followers + 4 stockists.
- [x] Instagram/TikTok counts often unreadable (login wall, not indexed; SerpApi has 1 search left this
      month) -> shown as "couldn't be read, open profile", brand value marked partial, never "low" for it.
- [x] Report UI desktop + 390px: all sections visible, 0 JS errors, no overflow.

## 2026-10-14 — Test notifications: email timeout + WhatsApp "only hello_world"
- [x] Email: smtp.gmail.com 465 AND 587 both time out from this laptop's network (socket test) —
      network/firewall, not code. Test row now says so.
- [x] WhatsApp: a business-sent template does NOT open the 24h window, so the follow-up text was
      accepted by Meta and silently dropped. New setting "Team alert template" (utility template with
      one {{1}} variable) carries the alert; without it the test shows "accepted, may not arrive".
- [ ] User: register the alert template in Meta + enter its name; email works from VPS/other network.

## 2026-10-14 — Brief pre-collected by research + clear AI/human reply mode
User: "brief should be precollected" (chose: research fills it, no chat); "this should be clear if its
ai replying or human has taken over".
- [x] Brief = 4 research items: what they sell, product count, location, Colombo-or-not. Consignment
      willingness (outside Colombo only) is the one chat question, shown as such, not a research gap.
- [x] Step reads "Brief collected (all 4 from research)" or "Brief incomplete (missing: ...)"
- [x] Research: product count (Daraz store / catalogue / stated range), more location sources
      (Google Maps, Facebook About, directories); contact-page footer text; cart boilerplate skipped
- [x] FIX: a failed/timed-out web search no longer wipes the previous good research (Cecrafto lost Galle)
- [x] Max 2 concurrent web-search CLI calls, 360s each (6 in parallel all timed out at 240s)
- [x] Partner Chats: big reply-mode banner (AI replying / Human takeover / Waiting for a human / stopped)
      with an explicit button, replacing the small toggle. Verified in browser, 0 JS errors.
- [ ] Re-verify the 3 gap cards after the timeout fix (House of Leather, Ceylon Elements, Cecrafto)
- [x] Product count, user: "houseofleather.lk/product has a lot", "run a rabbit also has a lot":
      (1) research "3" was a count of CATEGORIES -> prompt + code guard drop category counts;
      (2) RunRabbit IS Shopify but discovery stored platform=None, so products.json was never read ->
      store_catalog now tries Shopify, WooCommerce, then sitemap product URLs regardless of stored
      platform: RunRabbit 217 products / 36 collections (was ">=11");
      (3) custom sites: distinct product-detail links on their shop pages = real lower bound
      (House of Leather >=13, was missing). Cached empty counts are re-checked on every build.
- [x] Re-verified gap cards: Cecrafto complete (Galle, outside Colombo); Ceylon Elements + House of Leather
      location not found anywhere online -> report says "Brief incomplete (missing: location)".

## 2026-10-14 — Nuts on Top: followers + product count (user: "why no facebook, instagram followers detected, tiktok" / "theres SKU more than six")
- [x] Store catalog also checks the brand's other hosts the homepage loads from (shop.nutsontop.com WooCommerce = 13 products, was "≥6"); exact store lists on every host before any sitemap estimate. Verified live: Nuts on Top 13 from Store API at shop.nutsontop.com; Basketeer 327 / Edutoys 389 unchanged.
- [x] Follower counts read directly from each profile's public link preview (FB/IG/TikTok og:description) on every report build; replaces web-search estimates. Verified: Nuts on Top FB 3,958 / IG 4,448 / TikTok 2,621 (search had FB 3,000, IG+TikTok unread).
- [x] Brand value scored on combined followers, Sri Lanka scale (10k+ = high; 3k+ with press/stockists = high). Nuts on Top -> high, verdict Lucrative.
- [x] Startup pass upgrades existing reports: followers refreshed on all 67 pending (unreadable profiles left: 4 IG, 3 TikTok, 1 FB); cards without an exact product list queued for rebuild once (hosts_checked marker).
- [ ] Watch: 44/67 now "high" — threshold may need raising if it stops separating brands.

## 2026-10-14 — Kogul confirms researched facts (user: "let kogul also ask and just clarify")
- [x] Known facts are no longer silently dropped: first brief step is ONE message stating what research found (sells / product count / location) and asking to confirm or correct; their correction wins; no one-by-one re-asking. brain.brief_block + app_kogul._known_facts_for_partner wording.
- [x] Verified with real gpt-4o chats, run twice (2 businesses each): confirmation in one message 2/2 and 2/2; partner correction (389 -> 450) used.
- [x] 2026-10-14 opening message replaced with user's 'new intro' (db.py default + local DB + VPS DB setting); {category} fills e.g. 'Grocery & Fresh Food'; no business name in the greeting.

## 2026-10-14 — Outreach template with a name variable (user's Meta template "Hi {{1}}" header + 2 quick-reply buttons)
- [x] New setting wa_outreach_template_name ("Template greets by name" checkbox): when on, the template send passes the partner's name as the header variable; off = no components. Verified with a stubbed send: on -> header param "Mineral Earth", off -> none.
- [x] Settings page showed "{{1}}" as "1" (Jinja evaluated it); escaped. 
- [ ] Not verified: real Meta delivery (needs the template approved).

## 2026-10-14 — Opportunity-driven search must not depend on someone noticing (user: "if i didnt noticed will it have caught" Hijabs; "yes please do all these")
Finding: Hijabs is a category_gap (Daraz rev 1.7M) but ranks 119/274 in clothing_fashion; search only gets the top 30 by revenue (T-shirts, boxers, luggage...). Hijab brands only came via the manual missing-items route.
- [x] 1. Drop duplicate / umbrella / off-department gaps (repeated "Clothing", bags/luggage/motorcycle gear inside clothing).
- [x] 2. Rank by fit, not revenue: one batched model call per department scores each gap for independent-brand suitability (niche/specialist vs mass commodity); combine with revenue.
- [x] 3. Widen window (top ~60 by fit) and cluster those; pick the cluster to search with rotation memory so every cluster gets its turn over the weeks.
- [ ] Verify: Hijabs enters a modest-wear cluster; simulate N weeks of picks; run a real clothing search on that cluster and see whether hijab brands come back.

- Verified (clothing_fashion): cleaning 274 -> 170 gaps; Hijabs rank 119 -> 2 by fit+revenue; clustering puts Hijabs in a modest/ethnic-wear cluster; 12-week rotation simulation picks it weeks 1,5,9 and rotates 5 of 8 clusters.
- [x] Real web search verified. First try (19-category ethnic-wear cluster) returned saree/salwar/tailor shops, NOT hijab brands -> clustering now capped at 6 specialist categories. Forced search on the tight hijab cluster returned Aayla Hijabs + HijAbaya (the two found by hand). Unforced run on the top cluster also surfaced hijabaya.lk.
- [x] Rotation bug found by simulation (hijab cluster never picked in 24 weeks: strong clusters kept winning every 5 weeks) -> freshness now grows with age so never-searched clusters go first. Re-simulated: hijab cluster picked in week 5 (and 18); 12 of 14 clusters searched in the first 14 weeks. Other 11 departments' fit scores are computed on first weekly run (cached in data/gap_fit_cache.json).

## 2026-10-14 — Robust Kapruka gap analysis (user: "kapruka has many candles not only 2 ... first identify their kind and search their kind ... sometimes their kind can be the brand name ... come up with robust system"; "nuts on top is a chocolate department fix in the code level")
Measured: old check read only page 1 of Kapruka search (<=20 results) then kept exact-kind matches. Paging the real site search (&p=N&onlyCatalogueSection=true, 32/page, stops ~182): "candle" = 102 candle titles, 13 soy candles (report said 2); "chocolate bar" = 43 bar titles (report said 6). Doofinder 429 "Search not available" — user: don't use Doofinder.
- [x] Deep Kapruka search: page the site search (generic term up to 6 pages, their-kind terms 2 pages), dedupe.
- [x] Profile names their KIND as shopper search terms (kind_terms) per range.
- [x] Judge every result: same kind / same category other kind / unrelated; report both numbers (Kapruka has N <category>, M of their kind).
- [x] Brand check: search their brand name on Kapruka; if Kapruka already sells it, say so in the verdict.
- [x] Add a Chocolates department (Kapruka has /online/chocolates); move Nuts on Top and other chocolate makers there.
- [ ] Rebuild existing reports' Kapruka checks (serialized, polite).
- [ ] Verify on Wrook (candles) and Nuts on Top (chocolate) against the measured numbers.
- Verified live (standalone, real Kapruka + gpt-4o): Wrook candle: 46 of their kind, 90 candles in all (was 'only 2'), verdict no longer a false gap; brand not on Kapruka. Nuts on Top: chocolate bar 11 of their kind / 122 in all (KitKat/Snickers judged other kind); truffle 16 / 196; brand not on Kapruka. Nuts on Top moved to new Chocolates department.
- Known weakness: category totals can include near-misses the judge files as 'other kind' (candle holders, number candles, a truffle cake). Their-kind counts looked right.
- [ ] Rebuild of all old-method reports queued at startup (serialized Kapruka, ~1-2 min each).

## 2026-10-14 — Like-for-like gap comparison (user: "make sure the gap analysis is super strong and accurate ... their catalog count and kapruka count of actual ... their category named oil can be skin care, kapruka oil search gives cooking/coconut oil — not apples to apples")
- [x] Ranges named unambiguously for a general store ("face oil", not "oil") + real example titles from their catalog.
- [x] Their count per range: classify every catalog title into a range (not the model's guess).
- [x] Kapruka judge compares against THEIR example products; keep excluded examples ("not counted: coconut cooking oil").
- [x] Ambiguity guard: generic term mostly another category -> flagged, specific terms carry it.
- [x] Report: side-by-side Theirs | Kapruka their kind | Kapruka category, + excluded.
- [x] Verify on Wrook, Nuts on Top, and an oil/skincare brand.
- Verified end-to-end through the app build (real Kapruka + gpt-4o): Wrook candle theirs 38 vs Kapruka 49 their kind (30 other kind, 102 not counted). Ceylon Elements 'Oil' range searched as 'face oil' (flagged ambiguous), theirs 11 vs Kapruka 7 -> real gap; face wash theirs 3 vs 5 -> thin. Nuts on Top chocolate bar theirs 3 vs Kapruka 71 their kind; truffles 3 vs 37. Rebuilt twice: identical (ranges reused per catalog).
- Bugs found by testing and fixed: 'theirs' was 3 for every range (range description carried 3 example names, model counted only those); truffle count swung 85 -> 39 between runs (ranges re-extracted each rebuild) -> ranges now fixed per store catalog; judge at temperature 0 in batches of 60.

## 2026-10-15 — Kapruka gap comparison app-wide (user: 'change it app wide' -> chose 'The Kapruka gap comparison')
Audit: only partner_report uses the deep like-for-like check; metrics.live_kapruka_check (legacy category-card checker, no current caller) still read page 1 + word matching. Expand Catalog hints / card evidence quote Opportunity-Tool page counts (not a search) — noted, not changed.
- [x] metrics.live_kapruka_check delegates to partner_report.kapruka_check (one engine).
- [x] Verify it on real categories (Hijabs, a facial-oil concept) vs old behaviour.

## 2026-10-15 — 'What they sell' line on every partner card + report (user: 'what do they sell - is it handmade chocolates like very specific kinda thing')
- [x] extract_profile returns 'sells' (one specific line); old reports backfilled by derive_sells at startup; card + report header + brief show it.
- [x] Verify the lines on real cards (Nuts on Top, Wrook, Ceylon Elements, a few others).
- Verified: 69/69 cards got a specific line (e.g. Nuts on Top: 'Handmade small-batch Sri Lankan chocolate bars and seasonal truffle collections'); DOM shows the line on all 69 cards and in the report header, no JS errors.
- Category checker (metrics.live_kapruka_check) now uses the same engine; found+fixed: Daraz-title-derived search words were wrong ('cap turban head' for Hijabs, 'light tea' for Soy Candles) -> category name leads; judge needed same-intended-user rule (baby turban hat is not a hijab): Hijabs now 0 of their kind = real opportunity; Soy Candles 46 covered. Partner-report regression rerun: Wrook 44 vs 49, Ceylon face oil 1 vs 7 (stricter), consistent.

## 2026-10-15 — Pushed to VPS (user: 'can push everything to vps')
- [x] Uploaded only changed code (10 files, verified byte-for-byte, backups in C:\apps\kogul_project\_backup_20261015-0847); did NOT run deploy_kogul_to_vps.py (it uploads the laptop .env over the VPS .env = would wipe KOGUL_ADMIN_PASSWORD, and overwrites live data json).
- [x] VPS settings set: opening message, outreach template pc_kogul + name box on (whatsapp_live still 0); Nuts on Top -> chocolates.
- [x] Restarted: first relaunch via restart_remote_app() left nothing running; started with redirected logs instead -> listening on 5011, serves chocolates/brand-sells/template box.

