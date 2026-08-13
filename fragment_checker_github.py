import asyncio
import json
import os
import random
import re
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from telethon import TelegramClient
from telethon.sessions import StringSession

# ================= CONFIG =================
# All values come from environment variables. In GitHub Actions these are
# injected from repository Secrets (see the workflow file). For local testing
# you can `export` them yourself before running the script.

_required = ("API_ID", "API_HASH", "BOT_TOKEN", "SESSION_STRING")
_missing = [k for k in _required if not os.environ.get(k)]
if _missing:
    raise SystemExit(
        f"❌ Ei environment variable(gula) paoa jayni: {', '.join(_missing)}\n"
        f"   GitHub repo Settings > Secrets and variables > Actions e eigula set koro."
    )

API_ID         = int(os.environ["API_ID"])
API_HASH       = os.environ["API_HASH"]
BOT_TOKEN      = os.environ["BOT_TOKEN"]
SESSION_STRING = os.environ["SESSION_STRING"]
TARGET         = os.environ.get("TARGET", "@muhammadeakub")

BATCH_SIZE       = 5
BATCH_DELAY_MIN  = 2.0   # seconds
BATCH_DELAY_MAX  = 4.0   # seconds
MAX_RETRY        = 2

BASE_DIR       = Path(__file__).resolve().parent
USERNAMES_FILE = BASE_DIR / "usernames.txt"
HISTORY_FILE   = BASE_DIR / "history.json"
# ===========================================

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Linux; Android 13; Pixel 7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/126.0.0.0 Mobile Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}

executor = ThreadPoolExecutor(max_workers=BATCH_SIZE)


def load_usernames() -> list:
    """Reads usernames.txt (one per line, '#' comments allowed, leading '@' stripped)."""
    if not USERNAMES_FILE.exists():
        raise SystemExit(f"❌ usernames.txt paoa jayni ekhane: {USERNAMES_FILE}")

    lines = USERNAMES_FILE.read_text(encoding="utf-8").splitlines()
    result = []
    for ln in lines:
        ln = ln.strip()
        if not ln or ln.startswith("#"):
            continue
        result.append(ln.lstrip("@"))
    return result


def load_history() -> dict:
    if HISTORY_FILE.exists():
        try:
            return json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def save_history(history: dict):
    HISTORY_FILE.write_text(json.dumps(history, indent=2, ensure_ascii=False), encoding="utf-8")


def fetch_sync(username: str) -> dict:
    res = {
        "username": username,
        "status": "—",
        "min_bid_ton": "—",
        "min_bid_usd": "—",
        "highest_bid_ton": None,
        "highest_bid_usd": None,
        "_debug": ""
    }
    url = f"https://fragment.com/username/{username}"
    last_err = ""
    for attempt in range(MAX_RETRY + 1):
        try:
            with requests.Session() as s:
                s.headers.update(HEADERS)
                r = s.get(url, timeout=20, allow_redirects=True)
                html = r.text

                if "Just a moment" in html or "Checking your browser" in html:
                    res["status"] = "🛡️ CF Challenge"
                    res["_debug"] = html[:400]
                    return res
                if r.status_code == 404:
                    res["status"] = "Not on Fragment"
                    return res
                if r.status_code == 403:
                    res["status"] = "🛡️ 403 Blocked"
                    res["_debug"] = html[:400]
                    return res

                return parse_fragment(username, html, r.status_code)
        except Exception as e:
            last_err = f"{type(e).__name__}: {str(e)[:40]}"
            import time
            time.sleep(1.5 * (attempt + 1))

    res["status"] = f"❌ {last_err}"
    return res


def parse_fragment(username: str, html: str, code: int) -> dict:
    res = {
        "username": username,
        "status": "—",
        "min_bid_ton": "—",
        "min_bid_usd": "—",
        "highest_bid_ton": None,
        "highest_bid_usd": None,
        "_debug": ""
    }
    if code == 404:
        res["status"] = "Not on Fragment"
        return res

    soup = BeautifulSoup(html, "html.parser")
    for t in soup(["script", "style"]):
        t.decompose()
    text = soup.get_text(" ", strip=True)
    clean = re.sub(re.escape(username), " ", text, flags=re.IGNORECASE)

    # The real status badge sits right after "<username>.t.me" on the page, e.g.
    # "addicting.t.me On auction". Matching it there avoids false hits from boilerplate
    # text elsewhere on the page that happens to contain words like "available".
    badge_match = re.search(r'\.t\.me\s*(On\s*auction|Sold|Available|Taken|Unavailable)', clean, re.IGNORECASE)
    if badge_match:
        badge = badge_match.group(1).lower().replace(" ", "")
        if badge == "onauction":
            res["status"] = "On Auction"
        elif badge == "sold":
            res["status"] = "Sold"
        elif badge == "available":
            res["status"] = "Available"
        else:
            res["status"] = "Taken"
    elif re.search(r'\bsold\b', clean, re.IGNORECASE):
        res["status"] = "Sold"
    elif re.search(r'\btaken\b|\bunavailable\b', clean, re.IGNORECASE):
        res["status"] = "Taken"
    elif re.search(r'\bavailable\b', clean, re.IGNORECASE):
        res["status"] = "Available"
    else:
        res["status"] = "Unknown"

    # ─── Auction table: "Highest Bid | Bid Step | Minimum Bid" (in that column order) ───
    # Fragment shows these as numbers in a row once at least one bid has been placed:
    #   <highest> ~$<usd>   <step> [<pct>%]   <minimum> ~$<usd>
    # The "%" after bid step isn't always shown, so it's optional here.
    auction_table = re.search(
        r'Highest\s*Bid\s*Bid\s*Step\s*Minimum\s*Bid\s*'
        r'(\d[\d,]*(?:\.\d+)?)\s*(?:~\s*\$\s*(\d[\d,]*(?:\.\d+)?))?\s*'   # highest bid + usd
        r'(\d[\d,]*(?:\.\d+)?)\s*(?:\d+\s*%\s*)?\s*'                     # bid step amount + optional pct (ignored)
        r'(\d[\d,]*(?:\.\d+)?)\s*(?:~\s*\$\s*(\d[\d,]*(?:\.\d+)?))?',    # minimum bid + usd
        clean, re.IGNORECASE
    )

    if auction_table:
        res["highest_bid_ton"] = auction_table.group(1).replace(",", "")
        if auction_table.group(2):
            res["highest_bid_usd"] = auction_table.group(2).replace(",", "")
        res["min_bid_ton"] = auction_table.group(4).replace(",", "")
        if auction_table.group(5):
            res["min_bid_usd"] = auction_table.group(5).replace(",", "")
    else:
        # No bids placed yet (or a different layout) — fall back to a plain "Minimum Bid: X" read
        min_bid_pattern = re.search(
            r'minimum\s+bid\s*:?\s*([\d][\d,]*(?:\.\d+)?)\s*(?:~\s*\$\s*([\d][\d,]*(?:\.\d+)?))?',
            clean, re.IGNORECASE
        )
        if min_bid_pattern:
            res["min_bid_ton"] = min_bid_pattern.group(1).replace(",", "")
            if min_bid_pattern.group(2):
                res["min_bid_usd"] = min_bid_pattern.group(2).replace(",", "")
        else:
            fallback = re.search(r'(?:minimum|bid)\s*:?\s*([\d][\d,]*(?:\.\d+)?)', clean, re.IGNORECASE)
            if fallback:
                res["min_bid_ton"] = fallback.group(1).replace(",", "")

        has_bid_activity = bool(re.search(r'(\d[\d,]*(?:\.\d+)?)\s+bids?\b', clean, re.IGNORECASE))
        if has_bid_activity:
            res["highest_bid_ton"] = "?"  # bids exist but we couldn't parse the table

    if res["min_bid_ton"] == "—" and not res["highest_bid_ton"]:
        res["_debug"] = html[:600]

    return res


async def check_one(username: str) -> dict:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(executor, fetch_sync, username)


# ================= REPORT FORMATTING =================

STATUS_ICON = {
    "Available": "🟢",
    "On Auction": "🔥",
    "Sold": "🔴",
    "Taken": "🟠",
    "Unknown": "⚪",
    "Not on Fragment": "❔",
}


def status_line(status: str) -> str:
    if status.startswith("🛡️") or status.startswith("❌"):
        return status
    icon = STATUS_ICON.get(status, "⚪")
    return f"{icon} {status}"


def fmt_num(n) -> str:
    try:
        f = float(n)
        if f.is_integer():
            return f"{int(f):,}"
        return f"{f:,.2f}"
    except (TypeError, ValueError):
        return str(n)


def to_float(n):
    try:
        return float(n)
    except (TypeError, ValueError):
        return None


def compare_to_history(r, prev):
    """Returns a small tag showing change vs the previous run. Empty on first run (no prev) to avoid clutter."""
    if not prev:
        return ""
    cur = to_float(r.get("min_bid_ton"))
    old = to_float(prev.get("min_bid_ton"))
    if cur is None or old is None:
        return ""
    if cur > old:
        return f" 📈 (was {fmt_num(old)})"
    if cur < old:
        return f" 📉 (was {fmt_num(old)})"
    return ""  # unchanged — no need to say so on every line


def make_report(results: list, history: dict) -> str:
    now = datetime.now().strftime("%d %b %Y, %I:%M %p")
    header = (
        "📱 Fragment Watch\n"
        f"🕐 {now}  •  {len(results)} checked\n"
        "▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬"
    )

    card_divider = "┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈"

    changed_blocks = []
    other_blocks = []

    for r in results:
        prev = history.get(r["username"])
        change_tag = compare_to_history(r, prev)

        line1 = f"━━ @{r['username']} ━━"
        line2 = f"{status_line(r['status'])} on Fragment{change_tag}"

        if r["min_bid_ton"] != "—":
            usd_part = f" (≈${fmt_num(r['min_bid_usd'])})" if r["min_bid_usd"] != "—" else ""
            line3 = f"🏷️ Min Bid: {fmt_num(r['min_bid_ton'])} TON{usd_part}"
        else:
            line3 = "🏷️ Min Bid: —"

        if r["highest_bid_ton"]:
            detail = "Bids active" if r["highest_bid_ton"] == "?" else f"{fmt_num(r['highest_bid_ton'])} TON"
        else:
            detail = "No bids yet"
        line4 = f"  🔥 Highest  • {detail}"

        block = "\n\n".join([line1, line2, line3, line4])
        if "📈" in change_tag or "📉" in change_tag:
            changed_blocks.append(block)
        else:
            other_blocks.append(block)

    sections = []
    if changed_blocks:
        sections.append("🔔 Price Changed\n\n" + f"\n\n{card_divider}\n\n".join(changed_blocks))
    if other_blocks:
        title = "📋 Listings" if not changed_blocks else "📋 No Change"
        sections.append(f"{title}\n\n" + f"\n\n{card_divider}\n\n".join(other_blocks))

    ok = sum(1 for r in results if r["min_bid_ton"] != "—" and "❌" not in r["status"] and "🛡️" not in r["status"])
    fail = len(results) - ok

    # ─── Shortcut legend: quick counts per status category ───
    counts = {}
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1

    legend_order = ["Available", "On Auction", "Sold", "Taken", "Not on Fragment", "Unknown"]
    legend_lines = []
    for status in legend_order:
        if status in counts:
            legend_lines.append(f"{status_line(status)}  —  {counts[status]}")
    other_count = sum(v for k, v in counts.items() if k not in legend_order)
    if other_count:
        legend_lines.append(f"⚠️ Other/Failed  —  {other_count}")

    legend = "🗒️ Shortcut\n" + "\n".join(legend_lines)

    footer = f"▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬\n{legend}\n\n✅ {ok} parsed  •  ⚠️ {fail} failed"

    return header + "\n\n" + "\n\n".join(sections) + "\n\n" + footer


async def main():
    usernames = load_usernames()
    history = load_history()

    print(f"🧪 Fragment Checker | {len(usernames)} usernames")
    bot = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)
    await bot.start(bot_token=BOT_TOKEN)
    print("✅ Bot connected")

    await bot.send_message(TARGET, f"🚀 Check started\n📦 {len(usernames)} usernames queued")

    results = []
    debug_sent = False
    for i in range(0, len(usernames), BATCH_SIZE):
        batch = usernames[i:i + BATCH_SIZE]
        tasks = [check_one(u) for u in batch]
        batch_results = await asyncio.gather(*tasks, return_exceptions=True)

        for r in batch_results:
            if isinstance(r, Exception):
                results.append({
                    "username": "?", "status": f"❌ {str(r)[:30]}",
                    "min_bid_ton": "—", "min_bid_usd": "—",
                    "highest_bid_ton": None, "highest_bid_usd": None, "_debug": ""
                })
            else:
                results.append(r)

        print(f"✅ [{min(i+BATCH_SIZE, len(usernames))}/{len(usernames)}]")

        if not debug_sent:
            for r in batch_results:
                if not isinstance(r, Exception) and r.get("_debug"):
                    await bot.send_message(TARGET, f"🔧 DEBUG @{r['username']}:\n{r['_debug'][:1500]}")
                    debug_sent = True
                    break

        await asyncio.sleep(random.uniform(BATCH_DELAY_MIN, BATCH_DELAY_MAX))

    report = make_report(results, history)

    # keep blocks intact when splitting for Telegram's ~4096 char limit
    chunks = []
    current = ""
    for part in report.split("\n\n"):
        candidate = (current + "\n\n" + part) if current else part
        if len(candidate) > 3900:
            chunks.append(current)
            current = part
        else:
            current = candidate
    if current:
        chunks.append(current)

    for c in chunks:
        await bot.send_message(TARGET, c)
        await asyncio.sleep(1)

    # update history for next run's comparison
    new_history = {r["username"]: r for r in results if r["username"] != "?"}
    save_history(new_history)

    print("🎉 Done!")
    await bot.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
