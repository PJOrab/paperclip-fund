#!/usr/bin/env python3
"""send-telegram: send a Markdown briefing to the fund's Telegram chat.

Usage: <briefing markdown> | python fund_skills/send_telegram.py
Reads TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID from the fund and macro-agent .env.
"""
import argparse
import os
import sys
from pathlib import Path

FUND_DIR = os.environ.get("FUND_DIR", str(Path(__file__).resolve().parent.parent))
MACRO_DIR = os.environ.get("MACRO_AGENT_DIR", "/srv/macro-agent")
try:
    from dotenv import load_dotenv
    load_dotenv(Path(FUND_DIR) / ".env")
    load_dotenv(Path(MACRO_DIR) / ".env")
except ImportError:
    pass

import requests  # noqa: E402

TG_MAX = 4096


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default="-", help="message source ('-' = stdin)")
    ap.add_argument("--chat-id", default=os.environ.get("TELEGRAM_CHAT_ID", ""))
    a = ap.parse_args()
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token or not a.chat_id:
        print("missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID", file=sys.stderr)
        sys.exit(1)
    text = (sys.stdin.read() if a.file == "-" else open(a.file).read())[:TG_MAX]
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    base = {"chat_id": a.chat_id, "text": text, "disable_web_page_preview": True}
    # Telegram renders <b>/<i>/<a> via HTML; if the editor emits invalid markup
    # Telegram returns 400 — fall back to plain text so the briefing still arrives.
    r = requests.post(url, json={**base, "parse_mode": "HTML"}, timeout=30)
    if not r.ok:
        r = requests.post(url, json=base, timeout=30)
    try:
        print(r.json())
    except Exception:  # noqa: BLE001
        print(r.text)
    sys.exit(0 if r.ok else 1)


if __name__ == "__main__":
    main()
