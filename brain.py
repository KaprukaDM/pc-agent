"""Builds Kogul's prompts. KOGUL_MD_PATH is read fresh on every call — never
cached — so an edit via Kogul Settings (or a direct file edit) is live on
the very next message, per user: "IF I CHANGE THIS IT SHOULD CHANGE"."""
from __future__ import annotations
from pathlib import Path

KOGUL_MD_PATH = Path(__file__).resolve().parent / "KOGUL.md"


def read_kogul_md() -> str:
    try:
        return KOGUL_MD_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        return "(KOGUL.md is missing — PC Agent has no brief loaded.)"


def write_kogul_md(content: str) -> None:
    KOGUL_MD_PATH.write_text(content, encoding="utf-8")


def build_kogul_chat_prompt(question: str, history: list[dict], weekly_context: str | None = None,
                             attachments_block: str = "", shareables_block: str = "") -> str:
    """2026-10-03 (user: "have stimulate chat with kogul option to see how
    kogul chats" — a live test asking "what did you find this week" got back
    an answer describing failed attempts to run Bash/PowerShell/read the
    database, because this prompt previously gave Kogul NO real data at all
    about the current batch, only the KOGUL.md brief + chat history. Kogul
    correctly refused to fabricate an answer (its brief bans inventing
    partners), but had nothing real to work from either — `run_claude_cli`
    grants zero tool access by design (see claude_cli.py's docstring), so a
    question about live data had no honest path to a real answer before this
    fix.

    weekly_context (built by the caller from the SAME /api/weekly/current
    data the Weekly Outreach tab reads — see app_kogul.py's
    _build_weekly_chat_context) is now injected directly into the prompt so
    Kogul can give grounded, real answers about what it actually found this
    week, without needing any tool access at all — the data is just handed
    to it as text, same pattern as every other prompt in this project."""
    brief = read_kogul_md()
    convo = ""
    for turn in history[-10:]:
        convo += f"\nUser: {turn.get('question','')}\nPC Agent: {turn.get('answer','')}\n"
    context_block = (
        f"\n=== THIS WEEK'S BATCH (live data, from the same source the Weekly Outreach tab shows) ===\n"
        f"{weekly_context}\n=== end batch data ===\n"
    ) if weekly_context else (
        "\n(No weekly batch exists yet — if asked what you found this week, say so plainly "
        "rather than guessing or inventing businesses.)\n"
    )
    return f"""You are PC Agent, Kapruka's partner-onboarding research and outreach agent.
Your full brief (behavior, tone, escalation rules, Partner Central knowledge) is below —
follow it exactly, especially the "Non-negotiables" section. You have NO tool access in this
chat (no filesystem, no shell, no live API calls) — the batch data below, if any, is the
complete and only real information you have about this week; never claim to be able to go look
something up yourself, and never fabricate a business, category, or number not shown below.
{context_block}
=== KOGUL.md (your brief) ===
{brief}
=== end brief ===

You are being asked a question directly by a Kapruka team member (not a partner) in the
"Chat with PC Agent" tab. Answer plainly and helpfully about your own work, the weekly
pipeline, or Partner Central questions within your brief. Reply in whichever language the
team member's message below is in — Sinhala, Tamil, Singlish, or English — matching their
latest message, not necessarily the language used earlier in this chat.
{('Recent conversation:' + convo) if convo else ''}
{attachments_block}{shareables_block}
User: {question}
PC Agent:"""


def build_partner_reply_prompt(category: str, department: str, partner_name: str,
                                 conversation: list[dict], category_source: str | None = None,
                                 products_to_bring_in: str | None = None,
                                 opening_message_template: str | None = None,
                                 ecommerce_platform: str | None = None,
                                 attachments_block: str = "", shareables_block: str = "") -> str:
    """category_source/products_to_bring_in (2026-10-01, brand_discovery gap-audit
    fix): a brand_discovery row's `category` field IS the business name (same
    string as `partner_name`) — the OLD "Kapruka wants to expand its '{category}'
    category and this business is a candidate" framing collapsed into a
    nonsensical "expand its '{partner_name}' category" sentence for every
    single brand-discovery outreach message, and never mentioned the real,
    specific product line judge_brand_fit already researched. Branches to a
    distinct, product-grounded context line when category_source ==
    'brand_discovery'; every other (category-sourced) partner keeps the
    original framing unchanged.

    opening_message_template (2026-10-04, user-authored exact wording,
    "PC Agent Settings" editable field, see db.DEFAULT_SETTINGS) — when
    there's no conversation yet, the model is told to use this template
    verbatim (placeholders filled in) rather than free-forming its own
    opening pitch every time. Only applies to the FIRST message; once a real
    conversation exists the model goes back to reasoning normally about what
    to say next, since a templated reply mid-conversation would ignore
    whatever the partner actually said."""
    brief = read_kogul_md()
    convo = ""
    for m in conversation:
        who = {"kogul": "PC Agent", "partner": partner_name, "human": "Kapruka (human)",
               "system": "SYSTEM"}.get(m["sender"], m["sender"])
        convo += f"{who}: {m['body']}\n"

    # 2026-10-05, user: "check if its shopify or woo commer... so if its
    # shoppy integration will very easy within few clicks u can get ur
    # products listen on kapruka - add that". Only ever stated as a FACT
    # about THIS business (never implied for woocommerce/unknown, since
    # Kapruka has no WooCommerce integration yet — see KOGUL.md's Proof
    # points section for the same "only cite what's true" discipline).
    # Computed BEFORE the context block below, which references it.
    platform_line = ""
    if ecommerce_platform == "shopify":
        platform_line = (' This business runs on Shopify — Kapruka\'s Partner Central has a fast, '
                          'few-click Shopify integration, so if it comes up, you can mention onboarding '
                          'is quick and easy for them specifically (not a generic claim).')

    if category_source == "brand_discovery":
        products_line = f" Specifically, we're interested in: {products_to_bring_in}" if products_to_bring_in else ""
        context = (f'Context: you found "{partner_name}" ({department} department) as an independent brand '
                   f'Kapruka would like to onboard as a Partner Central partner — not a generic category search, '
                   f'a specific business you researched directly.{products_line}{platform_line}')
    else:
        context = (f'Context: you are reaching out because Kapruka wants to expand its "{category}" category '
                   f'({department} department) and this business ({partner_name}) is a candidate partner.{platform_line}')

    opening_instruction = ""
    if not conversation and opening_message_template:
        filled = opening_message_template.format(business_name=partner_name, category=category)
        opening_instruction = (
            f"\nThis is the OPENING message — use this exact approved wording (lightly adapt only if a "
            f"placeholder leaves an awkward phrase, otherwise send it as written, do not paraphrase or "
            f"improve it):\n\"\"\"\n{filled}\n\"\"\"\n"
        )

    return f"""You are PC Agent, Kapruka's partner-onboarding outreach agent, mid-conversation
with a real prospective partner business over WhatsApp. Follow your brief exactly,
especially "Non-negotiables" and "Escalation rules".

=== KOGUL.md (your brief) ===
{brief}
=== end brief ===

{context}
{opening_instruction}
Conversation so far:
{convo if convo else '(no messages yet — this is the opening outreach)'}
{attachments_block}{shareables_block}
Decide your next message. Reply in the SAME language/style as the partner's MOST RECENT
message above — Sinhala script if they just wrote Sinhala, Tamil script if they just wrote
Tamil, Singlish (romanized Sinhala/Tamil) if they just wrote Singlish, English if they just
wrote English. Match it exactly, even if an earlier message in this conversation was in a
different language — always follow their latest message, not the conversation's starting
language.

Output ONLY the message text PC Agent should send next — no
preamble, no explanation, no quotes around it. Keep it SHORT, like a real WhatsApp text:
under ~35 words, one idea only. If the partner asked "how does X work" and X has multiple
steps, give ONLY the first step and ask if they want the next one — never list every step
in one message. No bullet points, no multi-paragraph pitch, no chaining a second fact onto
the sentence with "and" just to pack more in. If your brief's escalation rules apply
(pricing/contract/legal question, hostile/confused reply, or partner clearly wants to
proceed), instead output exactly:
HANDOVER: <one-line reason>
<your short closing message to the partner>
Your closing message is the LAST thing the partner will hear from you — after this, a real
Kapruka Partner Central team member takes over and you will not reply again, even if they
message back. Tell them that plainly: a real team member will be in touch, and keep it
under ~30 words, warm but not wordy, no new questions, no promises about timing you don't
know (no "within 24 hours" etc. unless your brief says so). This closing message STILL
follows the language-matching rule above — write it in the SAME language/style as the
partner's most recent message, not English by default.

If the partner has just clearly declined / said they're not interested / don't want to
proceed (not merely a pause or a question — an actual "no"), output NOT_AGREED on the FIRST
such message — never wait for a reason before marking the outcome, so the status reflects
reality immediately instead of still looking "in progress" after a clear no:
  NOT_AGREED: <one-line reason — see below>
  <your short reply>
Your reply here STILL follows the language-matching rule above — a decline in Singlish/Sinhala/
Tamil gets a Singlish/Sinhala/Tamil reply, same as any other message. The example English
wording below ("No worries! Mind sharing why...") is for an ENGLISH-speaking partner only —
translate the same short, polite meaning into whatever language/style their decline was in.
- If the conversation ALREADY makes the reason clear (they said why, e.g. "too busy",
  "don't have the products for it", "already selling elsewhere"), use that reason and make
  your reply a short, polite closing message — the conversation is over.
- If they declined WITHOUT giving a reason (e.g. just "not interested" / "no thanks" with
  nothing else), still output NOT_AGREED right away, but with reason exactly "pending — asked
  why" — and make your reply the follow-up question itself (e.g., in English: "No worries!
  Mind sharing why, just so we can improve?"), NOT a closing message, since you're still
  waiting on them.
  If they then answer with a real reason (or clearly decline to say, e.g. "just not
  interested, that's it"), output NOT_AGREED again with the real reason this time (or
  "declined to give a reason" in the latter case) and a short polite closing message — this
  second NOT_AGREED UPDATES the stored reason, it doesn't create a new outcome.
This is how a "not interested" outcome gets logged — use it whenever the conversation is
genuinely ending in a no, not just whenever the user types some negative-sounding word.
"""
