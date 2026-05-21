#!/usr/bin/env python3
"""Telegram feedback poller — the CEO's two-way channel.

Long-polls Telegram getUpdates. For each text message the CEO sends in the
allowed chat (e.g. a reply to a briefing), it creates a high-priority Paperclip
issue assigned to the CIO so the agents act on it AND remember it for the next
briefing, then acks on Telegram and wakes the CIO. The update offset is
persisted so messages are never processed twice.

Run as a systemd service (telegram-feedback.service). getUpdates and webhooks
are mutually exclusive — nothing else may poll this bot.
"""
import json
import os
import time
import urllib.request
from pathlib import Path

from dotenv import dotenv_values

FUND_DIR = os.environ.get("FUND_DIR", "/srv/ai-tech-fund")
MACRO_DIR = os.environ.get("MACRO_AGENT_DIR", "/srv/macro-agent")
CFG = {**dotenv_values(f"{MACRO_DIR}/.env", interpolate=False),
       **dotenv_values(f"{FUND_DIR}/.env", interpolate=False)}

BOT = CFG["TELEGRAM_BOT_TOKEN"]
CHAT = str(CFG.get("TELEGRAM_CHAT_ID", "") or "")
API = (CFG.get("PAPERCLIP_API_BASE", "http://127.0.0.1:3100")).rstrip("/")
KEY = CFG["PAPERCLIP_API_KEY"]
CID = CFG["PAPERCLIP_COMPANY_ID"]
CIO = CFG["PAPERCLIP_CIO_AGENT_ID"]
STATE = Path(FUND_DIR) / ".tg_offset"

ROUTING = (
    "Behandle dies als CEO-Feedback mit hoher Priorität. Triagiere: Formatierung/Ton -> Editor (er aktualisiert seine eigene Instruktion); "
    "fehlende Daten/verpasste Story -> Coverage-Ticket an den Data-Engineer; These/Qualität/kritisches Denken -> zuständiger Analyst/Devil's Advocate. "
    "Setze es um UND merke die Präferenz dauerhaft (para-memory-files + eine CEO-Präferenzen-Notiz, die der Editor vor jedem Briefing liest), damit das "
    "NÄCHSTE Briefing es bereits berücksichtigt. Bestätige dem CEO am Ende kurz per send-telegram, was du verstanden hast und was sich konkret ändert."
)


def tg(method, params, timeout=70):
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{BOT}/{method}",
        data=json.dumps(params).encode(),
        headers={"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=timeout))


def papi(path, payload, method="POST"):
    req = urllib.request.Request(
        f"{API}{path}", data=json.dumps(payload).encode(), method=method,
        headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=30))


def handle(msg):
    chat_id = str((msg.get("chat") or {}).get("id", ""))
    text = msg.get("text") or msg.get("caption") or ""
    if not text:
        return
    if CHAT and chat_id != CHAT:
        return                       # only the CEO's chat
    if (msg.get("from") or {}).get("is_bot"):
        return                       # ignore the bot's own briefings
    reply = msg.get("reply_to_message") or {}
    meta = f"chat={chat_id} msg_id={msg.get('message_id')}"
    if reply:
        meta += f" reply_to_msg_id={reply.get('message_id')}"
    body = f"📨 CEO-Feedback via Telegram:\n\n{text}\n\n---\n{meta}\n\n{ROUTING}"
    papi(f"/api/companies/{CID}/issues",
         {"title": "📨 CEO-Feedback (Telegram)", "description": body,
          "assigneeAgentId": CIO, "priority": "high"})
    try:
        tg("sendMessage", {"chat_id": chat_id,
                           "text": "✅ Feedback erhalten — fließt ins nächste Briefing ein.",
                           "reply_to_message_id": msg.get("message_id")}, timeout=20)
        papi(f"/api/agents/{CIO}/wakeup", {})
    except Exception:  # noqa: BLE001
        pass


def main():
    offset = int(STATE.read_text().strip()) if STATE.exists() else 0
    while True:
        try:
            res = tg("getUpdates", {"offset": offset, "timeout": 60,
                                    "allowed_updates": ["message"]})
        except Exception:  # noqa: BLE001
            time.sleep(5)
            continue
        for upd in res.get("result", []):
            offset = upd["update_id"] + 1
            try:
                handle(upd.get("message") or {})
            except Exception:  # noqa: BLE001
                pass
        STATE.write_text(str(offset))
        time.sleep(1)


if __name__ == "__main__":
    main()
