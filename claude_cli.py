"""Runs Kogul's reasoning through the local `claude` CLI, never a direct
Anthropic API call — user was explicit: "MAKE SURE IT RUNS ON CLAUDE CLI".
This is the same _find_claude_cli/_claude_cli_env/_run_claude_cli pattern
already proven in app_ship.py (Akthar/Research tabs), including the VPS
SYSTEM-account HOME/USERPROFILE pin (see memory:
vps-system-account-claude-cli-env-bug-2026-09-22) — copied rather than
imported from app_ship.py so kogul/ stays a standalone app with no import
dependency on the 2,900-line main app.
"""
from __future__ import annotations
import json
import os as _os
import re
import subprocess
from pathlib import Path

_claude_cli_path: str | None = None


def _sandbox_cwd() -> str:
    import tempfile
    d = Path(tempfile.gettempdir()) / "kogul_cli_sandbox"
    d.mkdir(exist_ok=True)
    return str(d)


def _no_mcp_args() -> list[str]:
    """--tools only covers built-in tools; the CLI would still load the
    account's MCP connectors. An empty --mcp-config plus --strict-mcp-config
    loads none. Passed as a file because claude.cmd runs through cmd.exe,
    which can mangle inline JSON quoting."""
    cfg = Path(_sandbox_cwd()) / "no_mcp.json"
    if not cfg.exists():
        cfg.write_text('{"mcpServers": {}}', encoding="utf-8")
    return ["--strict-mcp-config", "--mcp-config", str(cfg)]


def find_claude_cli() -> str:
    global _claude_cli_path
    if _claude_cli_path is not None:
        return _claude_cli_path
    import shutil
    candidates = [
        Path.home() / "AppData/Local/Programs/nodejs/claude.cmd",
        Path.home() / "AppData/Roaming/npm/claude.cmd",
        Path("C:/Users/Administrator/AppData/Local/Programs/nodejs/claude.cmd"),
        Path("C:/Users/Administrator/AppData/Roaming/npm/claude.cmd"),
    ]
    for c in candidates:
        if c.exists():
            _claude_cli_path = str(c)
            return _claude_cli_path
    found = shutil.which("claude.cmd") or shutil.which("claude")
    if found:
        _claude_cli_path = found
        return _claude_cli_path
    raise RuntimeError("`claude` CLI not found — checked common install paths and PATH. "
                        "Kogul needs Claude Code installed and logged in on this machine.")


def claude_cli_env() -> dict:
    exe = find_claude_cli()
    env = _os.environ.copy()
    exe_path = Path(exe)
    parts = exe_path.parts
    if "AppData" in parts:
        profile_root = Path(*parts[: parts.index("AppData")])
        if profile_root.exists():
            env["HOME"] = str(profile_root)
            env["USERPROFILE"] = str(profile_root)
    return env


def run_claude_cli(prompt: str, model: str = "claude-sonnet-5", timeout: int = 180) -> str:
    """Non-interactive print mode, stdin (not argv — real prompts here can
    exceed Windows' ~8191-char command-line limit, same reasoning as
    app_ship.py's _run_claude_cli). No tool access (--allowedTools "") —
    Kogul reasons only over what's handed to it in the prompt (KOGUL.md +
    category/partner/chat context), it does not get filesystem/bash access
    into the project."""
    exe = find_claude_cli()
    try:
        # --tools "" removes every built-in tool (--allowedTools "" only skips
        # auto-approval and can still leave read-only tools usable), and the
        # empty cwd means there is no project file within reach either way.
        proc = subprocess.run(
            [exe, "-p", "--model", model, "--tools", "", *_no_mcp_args()],
            input=prompt,
            capture_output=True, text=True, timeout=timeout, encoding="utf-8", errors="replace",
            shell=False,
            env=claude_cli_env(),
            cwd=_sandbox_cwd(),
        )
    except FileNotFoundError as e:
        raise RuntimeError(f"Found claude CLI at {exe} but could not execute it: {e}")
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"Claude CLI timed out after {timeout}s.")
    if proc.returncode != 0:
        raise RuntimeError(f"Claude CLI exited {proc.returncode}: {(proc.stderr or '').strip()[:500]}")
    return (proc.stdout or "").strip()


_OPENAI_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"


def run_openai_chat_text(prompt: str, model: str = "gpt-4o", timeout: int = 60,
                          caller: str = "kogul.openai_chat") -> str:
    """Same (prompt, model, timeout) -> str shape as run_claude_cli, so the
    two call sites that use it (Chat with PC Agent, partner replies) barely
    change — 2026-10-04, user: "let kogul use open ai key to respond" (chat
    and outreach replies specifically; every OTHER Kogul call — brand
    discovery's search/judgment gates, worth_pursuing, etc. — stays on the
    Claude CLI, this is scoped to just these two conversational call sites).

    Reuses the project's shared scripts/openai_cost_tracker.call_openai_chat
    (already proven for brand_discovery.py's vision step) rather than a bare
    requests.post, so this spend is logged through the same per-caller cost
    ledger as every other OpenAI call in the project, not an invisible
    parallel cost source.

    NOT a plain-text wrapper: still sends a single-turn chat completion
    (one user message = the whole prompt string, same as run_claude_cli
    takes one prompt string via stdin) — the prompt already carries full
    conversation history as text (see brain.py's builders), so a multi-turn
    messages array isn't needed here."""
    import sys
    sys.path.insert(0, str(_OPENAI_SCRIPTS_DIR))
    from openai_cost_tracker import call_openai_chat
    from _env import env

    api_key = (env() or {}).get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY not set in .env — Kogul chat/outreach needs it now that "
                            "it's configured to answer via OpenAI.")
    try:
        resp = call_openai_chat(
            api_key=api_key, model=model,
            messages=[{"role": "user", "content": prompt}],
            caller=caller, temperature=0.4, timeout=timeout)
    except Exception as e:
        raise RuntimeError(f"OpenAI chat call failed: {e}")
    try:
        return resp["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, TypeError):
        raise RuntimeError(f"OpenAI chat call returned an unexpected shape: {str(resp)[:300]}")


def extract_json(raw: str):
    """Extracts a JSON array/object from a Claude CLI response that may
    carry explanatory prose BEFORE or AFTER a ```json fence, or no fence at
    all. Every JSON-parsing call site in this project previously used
    `re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.MULTILINE)`,
    which only strips a fence at the very start/end of the string — any
    preamble text ("All three check out as genuine...") before the fence
    defeated it completely, json.loads() then raised, and every caller's
    fail-open except-block silently returned an empty/unfiltered result.

    Confirmed live 2026-10-02: a real `run_claude_cli_with_search` response
    for brand_discovery._open_web_search_for_brands began with a sentence of
    commentary before its ```json block, containing 3 genuine candidates —
    the old regex produced invalid JSON, the function silently returned
    `[]`, and the real result was lost with no visible error anywhere. This
    one bug affected EVERY Claude-CLI JSON call site in kogul/ (worth_pursuing,
    specialization, dedupe, brand_discovery) identically, since they all
    copied the same regex pattern.

    Strategy: try the exact old behavior first (cheap, works for the common
    fenced-with-no-preamble case), then fall back to locating a ```json ...```
    fence anywhere in the string via search (not anchored), then fall back to
    the outermost [...] or {...} span in the raw text. Raises the same way
    json.loads would (ValueError/json.JSONDecodeError) if nothing parses, so
    every existing fail-open except-block keeps working unchanged — this is
    a drop-in replacement for `json.loads(cleaned)`, not a new error path."""
    raw = raw.strip()
    attempts = []
    attempts.append(re.sub(r"^```(?:json)?|```$", "", raw, flags=re.MULTILINE).strip())
    fence = re.search(r"```(?:json)?\s*(.*?)```", raw, re.DOTALL)
    if fence:
        attempts.append(fence.group(1).strip())
    first_bracket = min((i for i in (raw.find("["), raw.find("{")) if i != -1), default=-1)
    if first_bracket != -1:
        last_bracket = max(raw.rfind("]"), raw.rfind("}"))
        if last_bracket > first_bracket:
            attempts.append(raw[first_bracket:last_bracket + 1].strip())
    last_err = None
    for candidate in attempts:
        if not candidate:
            continue
        try:
            return json.loads(candidate)
        except Exception as e:
            last_err = e
    raise last_err or ValueError("no JSON found in response")


def run_claude_cli_with_search(prompt: str, model: str = "claude-sonnet-5", timeout: int = 180) -> str:
    """Same as run_claude_cli but grants --allowedTools "WebSearch" — the ONE
    deliberate exception to this project's "Kogul gets no tool access"
    pattern (user, 2026-10-01, asked directly: real web search is the only
    way brand_discovery.py can find NEW brand candidates once BRAND_SEEDS.txt
    runs dry for the week — Python here has no search API of its own to call,
    and judge_brand_fit/worth_pursuing/specialization etc. all stay on the
    tool-free run_claude_cli since they only ever reason over text already
    fetched for them). Use ONLY for the brand-discovery search step; every
    other Kogul gate must keep using run_claude_cli."""
    exe = find_claude_cli()
    try:
        proc = subprocess.run(
            [exe, "-p", "--model", model, "--tools", "WebSearch", "--allowedTools", "WebSearch", *_no_mcp_args()],
            input=prompt,
            capture_output=True, text=True, timeout=timeout, encoding="utf-8", errors="replace",
            shell=False,
            env=claude_cli_env(),
            cwd=_sandbox_cwd(),
        )
    except FileNotFoundError as e:
        raise RuntimeError(f"Found claude CLI at {exe} but could not execute it: {e}")
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"Claude CLI (with search) timed out after {timeout}s.")
    if proc.returncode != 0:
        raise RuntimeError(f"Claude CLI (with search) exited {proc.returncode}: {(proc.stderr or '').strip()[:500]}")
    return (proc.stdout or "").strip()
