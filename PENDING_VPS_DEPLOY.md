# Pending VPS deploy — Kogul (PC Agent)

Written 2026-10-13 because the VPS (23.111.183.110:5011) went unreachable
(100% ping loss, HTTP + SSH both timing out) mid-session, right after a real
fix was verified working locally but never pushed. Updated same day with a
second, larger pending change (the pipeline interface). Updated AGAIN
2026-10-14 with a third batch (notifications, settings, brand redesign) —
VPS was STILL unreachable at every check (confirmed via fresh curl+ping
each time, most recently 2026-10-14). Run this once the VPS is back up.

## Pending changes, in order

1. **Message-brevity fix** (brain.py, KOGUL.md) — see section 2A below.
2. **Partner pipeline interface** (app_kogul.py, templates/kogul.html,
   static/kogul.css) — see section 2B below.
3. **Dual-channel notifications + Partner Central contacts + settings
   cleanup + Kapruka brand redesign** (db.py, app_kogul.py,
   templates/kogul.html, static/kogul.css) — see section 2C below.

All three built in the same local session with the VPS unreachable
throughout, so deploying once picks up all of them together — no need to
deploy in separate passes.

## 1. Check the VPS is actually back

```
curl -s http://23.111.183.110:5011/ -o /dev/null -w "%{http_code}\n" --max-time 15
ping -n 3 23.111.183.110
```
Both need to succeed before anything below is worth attempting. If still
down, this is an infrastructure problem (reboot, provider outage, firewall),
not something fixable from this machine.

## 2A. Message-brevity fix

**What**: message-brevity fix. A real transcript showed PC Agent still
sending 3 dense, multi-clause sentences in one reply ("create an
account... fill in business details, upload documents, submit bank
info... activated within 48 hours" — all stacked into one message instead
of spread across the conversation). The existing "1-3 sentences" rule
wasn't strong enough; it was technically satisfied while still reading
like a paragraph.

**Files changed** (already edited and saved locally, NOT yet on the VPS):
- `kogul/KOGUL.md` — "SMS/WhatsApp-short" section rewritten with a concrete
  ~35-word cap, "one idea per message," "answer only what was asked, not
  everything you know" (multi-step answers now give step 1 only, then ask
  if they want the next step).
- `kogul/brain.py` — `build_partner_reply_prompt`'s output instruction
  updated to match (same ~35-word cap stated directly in the prompt, right
  before the model's final decision — stronger-attention spot than the
  brief alone).

**Verified locally** (2026-10-13, Simulate as Partner, simulation id 13,
since deleted): replayed the same "hi" → "how it works" → "yes" flow from
the real transcript. New replies:
- "You can create an account on the Partner Central website. Want to know
  the next step?" (15 words, one step, invites next turn)
- "Next, you'll need to provide your product and business information.
  Would you like to know more about what details are needed?"

Both one idea, both short, both inviting the next turn instead of
front-loading — confirms the fix works as intended.

## 2B. Partner pipeline interface

**What**: Dashboard redesign (user: "remove this week, add a date range
filter that will dynamically change the values... so the order pipeline
reached / in progress / need human interference / agreed and pushed /
unresponsive / disagreed... a breakdown of partners who is in progress
stage... click it should take to the partner conversation and read an ai
analysis of the conversation... a pipeline interface"). Replaces the old
fixed-"This week" stat row entirely.

**Files changed** (already edited and saved locally, NOT yet on the VPS):
- `kogul/app_kogul.py`:
  - `/api/dashboard` rewritten from week-scoped to `date_from`/`date_to`-scoped
    (filters on `partners.created_at` — "first contacted" cohort, user
    confirmed this over "last activity"). No date params = all-time.
    `handover_count` stays global/unfiltered (nav badge still polls it
    every 30s regardless of the visible range). Added `needs_handover`
    (date-scoped count) and `reached` to the response; stage order in the
    JSON matches the user's requested pipeline order.
  - `/api/partners` extended with two virtual status values —
    `status=in_progress` (maps to `contacted|replied|negotiating`) and
    `status=reached` (maps to `status != 'new'`) — plus `date_from`/`date_to`
    params, same created_at-based filtering as the dashboard endpoint.
  - New `/api/partners/<id>/analyze` — real, on-demand OpenAI (gpt-4o) call
    per partner (NOT precomputed for every partner up front — most
    conversations are never opened, so that would be wasted real spend).
    Reads the actual message history, outputs JSON
    `{summary, interest_level, interest_reason, next_step}`. Fails open
    with a plain error (never a fabricated analysis) if the model call or
    JSON parse fails.
- `kogul/templates/kogul.html`:
  - Dashboard tab: "This week" header removed, replaced with "Partner
    pipeline" + a date-range `<input type=date>` pair (`#dashDateFrom`/
    `#dashDateTo`) + Apply/All-time buttons. The 6 stat cards reordered to
    the user's requested pipeline order and made clickable
    (`.pipeline-stage`, `data-stage="..."`) — clicking one sets
    `partnerChatsPendingFilter` (status + the currently active date range)
    and programmatically clicks the Partner Chats nav tab, reusing the
    EXISTING partner-list/conversation UI for drill-down rather than
    building a second, parallel one.
  - Partner Chats tab: filter dropdown gained `in_progress`/`reached`
    options (previously only had the raw DB status values + needs_handover);
    `loadPartnerList()` now picks up `partnerChatsPendingFilter` once (then
    clears it) and carries an active date range alongside the status
    filter, with a "Clear date range" button once one is set.
  - Partner conversation pane (`openPartnerChat`): new "AI analysis" button
    in the header. Click → calls `/api/partners/<id>/analyze`, renders an
    interest-level badge (color-coded: high=green/medium=blue/low=red/
    unclear=gray) + summary + reason + suggested next step in a card above
    the message list.
- `kogul/static/kogul.css`: `.pipeline-stage:active` press feedback,
  `.handover-stage` red color variant (top bar / number / icon) matching
  the existing agreed/not-agreed/unresponsive pattern.

**Verified locally** (2026-10-13, via Claude-in-Chrome browser automation
against `http://127.0.0.1:5010`):
- Dashboard renders the new "Partner pipeline" / "All time" header, date
  range inputs, and all 6 stage cards in the correct order with correct
  color coding (screenshot confirmed).
- Clicking the "In progress" stage card correctly jumped to Partner Chats
  with the filter dropdown pre-set to "In progress" (screenshot confirmed —
  empty list was correct, since this machine's local kogul.sqlite has no
  real partners right now, the real ones are on the VPS).
- Created one real test partner + 2-message conversation locally
  ("Test Pipeline UI Co", partner id 8, since deleted along with its
  messages and weekly_categories row), opened it, clicked "AI analysis" —
  got back a real, accurate GPT-4o analysis: "high interest" badge,
  summary correctly noting the partner "explicitly stated they are 'very
  interested'" and asked about commission, suggested next step "Provide
  detailed information about the commission structure." Confirms the full
  real-LLM-call pipeline works, not just the UI shell.
- `/api/dashboard` and `/api/partners?status=in_progress`/`?status=reached`
  tested directly via curl with and without date params — correct shape,
  no errors.

## 2C. Dual-channel notifications + Partner Central contacts + settings cleanup + brand redesign

**What**: four related changes from the same request (user: "i want to know
how human intervention and interested candidaes will be informed... i want
it both email and to whatsapp message as well... use the env admin and all
the creds to send whatsapp notification along with email... i want in
settings to Partner Central team email and number to fill... we can add
upto 3 email and 3 numbers... and remove unnessary settings... improve the
UI interface of the entriee app - make it more kapruka and more interactive
and visually appealing").

**1. Real WhatsApp notifications (new), alongside the existing email ones**:
- `kogul/app_kogul.py` — new `_notify_pc_team_whatsapp(text)`, called
  alongside the existing `_notify_pc_team(subject, body)` email call at
  BOTH trigger points (partner agreed, human-handover needed). Sends to
  every number in the new `pc_team_phones` setting.
- **Real constraint, not a bug**: WhatsApp refuses a plain text message to
  a number that hasn't messaged Kogul's WABA number within 24h (error
  131047) — only a pre-approved TEMPLATE can open that window cold, and
  Kogul only has Meta's free `hello_world` template (fixed greeting, no
  custom text) today. User confirmed (AskUserQuestion) the two-step
  approach: send `hello_world` first to guarantee the window is open, then
  immediately send the real notification text as a normal follow-up
  message. Costs one harmless extra greeting on a recipient whose window
  was already open; never blocks the real text.
- **Real bug caught and fixed while writing this**: `notify_whatsapp.py`'s
  `send_text()` is written as a CLI-script function that calls `sys.exit()`
  on an HTTP/API failure — `sys.exit()` raises `SystemExit`, which a bare
  `except Exception:` does NOT catch. Without the fix, one bad phone number
  would have killed the Flask request thread outright instead of just
  skipping that recipient. `_notify_pc_team_whatsapp` explicitly catches
  `(Exception, SystemExit)` at both the per-recipient and outer level.

**2. Partner Central team contacts setting (replaces the old single hardcoded recipient)**:
- `kogul/db.py` — new settings `pc_team_emails`/`pc_team_phones` (JSON
  arrays, up to 3 each, server-enforced cap in `api_settings_update`). The
  old `pc_team_email` (singular) setting field is now dead/unused by the
  notification path — kept only so an existing saved value isn't silently
  lost, no longer shown in the UI.
- `kogul/app_kogul.py` — `_notify_pc_team`/`_notify_pc_team_whatsapp` read
  these fresh via new `_pc_team_emails()`/`_pc_team_phones()` helpers,
  replacing the old hardcoded `_NOTIFY_RECIPIENTS = ["shanaka@kapruka.com"]`
  constant (kept as `_NOTIFY_RECIPIENTS_FALLBACK`, used only if the
  settings array is genuinely empty — e.g. a fresh install — so a real
  handover alert is never silently lost to an empty recipient list).
- `kogul/templates/kogul.html` — Partner Central Settings tab: old single
  "Partner Central team email" field replaced with a "Partner Central team
  contacts" card, 3 email inputs + 3 phone inputs, one Save button
  (`btnSavePcContacts`) posting both arrays at once.

**3. Settings cleanup (user: "remove unnessary settings")** — both removed
from `kogul/templates/kogul.html`, confirmed via AskUserQuestion which ones
these meant:
- "WhatsApp — send a test message" card (PC Agent Settings tab) and its JS
  (`loadWaTestSends`, `btnWaSendTest` handler) — removed. The underlying
  `/api/whatsapp/send_test` route, `whatsapp_test_sends` table, and
  `api_whatsapp_test_sends` endpoint were DELIBERATELY KEPT server-side —
  the Cost tab's WhatsApp-spend calculation still reads
  `whatsapp_test_sends` (`SELECT COUNT(*) ... WHERE status != 'failed'`);
  removing the table/route would have silently broken cost tracking for a
  UI-only cleanup request.
- "Partner Central pitch / terms" explainer card (Partner Central Settings
  tab) — removed. It never held real content, just a pointer to KOGUL.md's
  "Partner Central" section; KOGUL.md editing itself is untouched, still
  under PC Agent Settings → PC Agent's brief.

**4. Kapruka brand redesign (user: "make it more kapruka and more
interactive and visually appealing")**:
- `kogul/static/kogul.css` — `:root` color variables replaced wholesale.
  Old palette was generic Google Material blue (`--primary: #1a73e8`) with
  Kapruka purple confined to just the header bar. New palette pulls REAL
  colors sampled live off kapruka.com (not guessed): `--primary: #402970`
  (the real header purple, applied app-wide now — buttons, tabs, links,
  active states, chat bubbles, badges all flow through this variable, so
  no per-component hardcoding was needed) and a new `--accent: #f8da08`
  (Kapruka's real search/CTA yellow), used sparingly — active-tab
  underline, a small accent dot on the header logo badge, and `.btn.accent`
  for ONE primary action per view (currently just "Find more businesses")
  — mirroring how Kapruka's own site uses yellow as a single strong accent
  against the purple, never a second competing brand color. Material
  elevation shadows re-tinted toward the purple instead of neutral gray.
  `--kapruka-purple`/`--kapruka-purple-dark` kept as aliases since
  header/footer code already referenced those names.
- Added hover/press interactivity throughout (user: "more interactive"):
  button press-scale, pipeline stage cards lift on hover, partner-list
  items shift + gain a left accent bar when active.

**Verified locally** (2026-10-14):
- `/api/settings/update` round-trip tested directly via curl —
  `pc_team_emails`/`pc_team_phones` save and read back correctly as JSON
  arrays, capped at 3 each.
- **Real notification test attempted live** against `akthar@kapruka.com`
  (user-approved via AskUserQuestion) by creating a disposable test
  partner and marking it "agreed" — the API call succeeded, but the actual
  email send failed with a local SMTP timeout. This is NOT a new bug — it
  matches a PREVIOUSLY DOCUMENTED finding from an earlier session (see
  memory: this laptop's network/ISP blocks outbound SMTP ports 587/465
  entirely; the VPS does not have this restriction and was used for the
  original real send verification). Confirmed directly:
  `notify_email.send_email(...)` called in isolation (bypassing the
  silent fail-open wrapper) returned
  `{'ok': False, 'error': 'TimeoutError: timed out'}` — a real, expected,
  LOCAL-ONLY limitation. The code path itself is correct; it cannot be
  fully verified end-to-end until deployed to the VPS. Test partner and
  settings were cleaned up/reset after this test.
- **WhatsApp notification path — LIVE-VERIFIED, both scenarios, real
  device** (2026-10-14, user-provided real test number 94702398628,
  confirmed via AskUserQuestion after each send): unlike SMTP, the Meta
  Graph API is NOT blocked from this local machine. Ran both real triggers
  through the actual in-app code path (not a standalone script call) —
  (1) disposable test partner marked "agreed" via
  `/api/partners/<id>/set_status`, user confirmed receiving the WhatsApp
  message(s); (2) disposable test partner sent a real pricing/contract
  question ("What's your exact commission percentage... before I can sign
  the contract") via `/api/partners/<id>/reply_from_partner`, correctly
  triggered `HANDOVER: commission percentage inquiry`, user confirmed
  receiving that WhatsApp message too. Both test partners and the test
  settings (akthar@kapruka.com / 94702398628) were cleaned up/reset to
  empty after. This closes the "needs live VPS verification" gap for
  WhatsApp specifically — only the EMAIL side still needs VPS
  verification (blocked locally, see above), WhatsApp is confirmed
  working end-to-end already.
- Settings UI, removed cards, and brand redesign all verified visually via
  Claude-in-Chrome screenshots against `http://127.0.0.1:5010` — purple
  app-wide (not just header), yellow active-tab underline, yellow accent
  dot on the logo badge, new 3-email/3-phone contacts card rendering
  correctly, both target settings cards confirmed removed from their tabs.

**Deploy command** (covers 2A, 2B, AND 2C — one combined push):
```
cd "C:\Users\fari\Desktop\SEO AGENT"
python scripts/deploy_kogul_to_vps.py --skip-data-store --no-restart
```
Expect `kogul/KOGUL.md`, `kogul/brain.py`, `kogul/app_kogul.py`,
`kogul/db.py`, `kogul/templates/kogul.html`, `kogul/static/kogul.css` to
upload (6 files) — if the file list looks different, check what else
changed locally since this doc was written before proceeding.
**Do NOT pass `--push-local-db`** — kogul.sqlite stays VPS-owned (see
`scripts/deploy_kogul_to_vps.py`'s own docstring/comments for why: an
earlier session bug had every code deploy silently overwriting the VPS's
live data with this laptop's stale copy; fixed 2026-10-08, don't reintroduce it).

**Restart** (reliable pattern — `Start-Process`/background launch has
intermittently failed to actually bind the port in the past):
```python
# 1. find + kill whatever holds port 5011
import sys; sys.path.insert(0, 'scripts')
from _env import env, require
import paramiko
cfg = env()
vps, user, pw = require(cfg, 'VPS', 'VPS_USERNAME', 'VPS_PASSWORD')
client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect(vps, username=user, password=pw, timeout=20)
stdin, stdout, stderr = client.exec_command(
    'for /f "tokens=5" %a in (\'netstat -ano ^| findstr :5011 ^| findstr LISTENING\') do taskkill /PID %a /F',
    timeout=15)
print(stdout.read().decode(errors='replace'))
client.close()
```
```python
# 2. relaunch in the FOREGROUND (not Start-Process) — this is the one
# consistently reliable method found across this whole project; the Bash
# tool auto-backgrounds it after ~30s but the Windows process survives
# independently of the SSH channel closing
client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect(vps, username=user, password=pw, timeout=20)
cmd = r'cd C:\apps\kogul_project\kogul && set KOGUL_PORT=5011 && python app_kogul.py'
stdin, stdout, stderr = client.exec_command(cmd, timeout=15, get_pty=False)
print(stdout.read().decode(errors='replace')[:1500])
client.close()
```
Then verify: `curl http://23.111.183.110:5011/ -w "%{http_code}\n"` → 200.

**Before relaunching, also check for a zombie/duplicate process** — this
project hit a real bug once where an old pre-fix process and a new one
were BOTH bound to the port simultaneously, and requests randomly hit
whichever responded, making a deployed fix look like it hadn't taken
effect. `netstat -ano | findstr :5011 | findstr LISTENING` should show
exactly ONE PID after step 1 above, before launching step 2.

## 3. Post-deploy verification (don't skip)

For 2A (brevity): run a real "Simulate as Partner" conversation through the
live VPS (`http://23.111.183.110:5011`), same flow as above (opening →
"hi" → "how it works"), and confirm the reply is short/one-step, not the
old 3-sentence dump.

For 2B (pipeline): open `http://23.111.183.110:5011`, confirm the
Dashboard shows "Partner pipeline" (not "This week") with the date-range
filter and 6 reordered stage cards showing the VPS's REAL counts (should
be non-zero — the VPS has real sourced partners, unlike this laptop's
local DB). Click a stage with partners in it, confirm it drills into
Partner Chats correctly filtered. Open one real partner conversation,
click "AI analysis," confirm it returns a real, grounded analysis (not an
error) — this is a live OpenAI call, so also sanity-check
`/api/costs` afterward to confirm one more `kogul.partner_analysis:*`
line logged, same per-caller cost tracking every other OpenAI call here uses.

For 2C (notifications/settings/redesign):
1. Go to Partner Central Settings, fill in a REAL test email (ask the user
   which address to use — do NOT guess or reuse a stale one from this doc)
   and, if ready to test WhatsApp too, a real test phone number. Save.
2. Confirm the email send actually works now that it's running from the
   VPS (not blocked like this laptop was) — trigger a real test the same
   way this doc's local attempt did (a disposable test partner marked
   "agreed," or a handover trigger), then check the test inbox arrived.
   Delete the disposable test partner afterward.
3. If a phone number was set, confirm it received TWO WhatsApp messages
   (hello_world greeting, then the real notification text) — or just the
   real text alone if that number's 24h window was already open.
4. Confirm "WhatsApp — send a test message" and "Partner Central pitch /
   terms" cards are gone from their settings tabs.
5. Confirm the Cost tab's WhatsApp spend figure still renders without
   errors (the underlying `whatsapp_test_sends` table/route were kept,
   only the UI trigger card was removed — this should be unaffected, but
   verify rather than assume).
6. Visually confirm the brand redesign landed — purple should be the
   dominant color throughout the app (not just the header), yellow should
   mark the active nav tab underline and the "Find more businesses" button.

A code push without a live re-test has been wrong before in this project
(stale zombie process serving old code even after a "successful" deploy)
— always confirm against real behavior, not just upload-success output.

## Notes for whoever picks this up

- `kogul/data/kogul.sqlite` is EXCLUDED from the default deploy sync (the
  VPS is the source of truth for live data — see the deploy script's own
  header comment). None of 2A/2B/2C touch the database SCHEMA in a way
  that needs manual migration — `db.py`'s `_migrate()` / `init_db()` runs
  automatically on every app start and will add the new
  `pc_team_emails`/`pc_team_phones` settings rows itself (via
  `INSERT OR IGNORE` against `DEFAULT_SETTINGS`) the first time the
  updated code runs on the VPS — no manual SQL needed.
- If the VPS has been down long enough that the 24h WhatsApp follow-up
  scheduler missed a cycle, that's expected and self-corrects once the
  process restarts (it reads real timestamps from the DB, not an
  in-memory clock) — no manual catch-up needed.
- Real-WhatsApp "Simulate as Partner" testing (pick/add a number, Kogul
  converses over actual WhatsApp, full context on "end," same-number
  restart resets the conversation) was EXPLICITLY DEFERRED this session —
  user confirmed via AskUserQuestion to hold off, since it needs a
  webhook (a public URL Meta posts inbound replies to) that doesn't exist
  anywhere in this project yet, plus manual registration in Meta's App
  Dashboard that only the user can do. Not started, not partially built —
  a clean deferral, treat as a fresh task when picked up.
