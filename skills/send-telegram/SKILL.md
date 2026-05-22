---
name: send-telegram
description: >
  Send the finished German CEO briefing (Markdown) to the fund's Telegram chat. Use this
  as the Editor's final delivery step after the briefing is written and persisted. Keeps
  within Telegram's 4096-character limit.
---

# send-telegram

Delivers a Markdown message to the fund's configured Telegram chat via the Bot API.

## How to run
Pipe the briefing markdown in:
```bash
echo "$BRIEFING_MD" | /srv/ai-tech-fund/venv/bin/python /srv/ai-tech-fund/fund_skills/send_telegram.py
```
Optional: `--chat-id <id>` to override the default chat.

## Notes
- Reads `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` from the fund/macro-agent env (or Paperclip secrets).
- Truncates to 4096 chars — keep briefings under ~3500.
- Run this only after the briefing is validated and persisted (`status = done`). Report the returned message id in your task comment.
- This sends a real message to the CEO. Do not send drafts or test content.
