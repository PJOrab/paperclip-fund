# Agenten-Schicht — AI/Tech Investment Committee

n8n feuert die Prompts; die eigentlichen LLM-Calls laufen über die **Claude Code
CLI headless** (`claude -p`, Opus 4.7 via Abo-Auth) — **keine Anthropic API**.

## Pipeline

```
Schedule (n8n) → SSH:triage → SSH:analyst → SSH:thesis → SSH:devil → SSH:editor → Telegram
```

| Stufe | Modell | Aufgabe |
|---|---|---|
| triage | opus | Roh-Feed → ~12 materielle Cluster (legt neuen `briefing_runs`-Eintrag an) |
| analyst | opus | Faktenlage & Marktwirkung je Cluster |
| thesis | opus | 3-5 investierbare Thesen (Bull/Bear, Katalysatoren, Conviction) |
| devil | opus | Devil's Advocate: greift jede These an, Falsifikation, Verdikt |
| editor | opus | CEO-Briefing (Markdown, deutsch) → stdout → Telegram + E-Mail |

Alle Stufen laufen auf **Opus 4.7** (`MODEL` in `run.py`). Tier je Rolle ist dort
einzeilig anpassbar (z.B. triage auf `haiku` für günstigere Vorfilterung).

Jede Stufe arbeitet auf der jüngsten `briefing_runs`-Zeile mit passendem `status`
(`analyst→thesis→devil→editor→done`). Kein Argument-Passing zwischen Nodes nötig.

## Lokal testen (ohne n8n)

```bash
cd /root/ai-tech-fund && source venv/bin/activate
python -m agents.run pipeline --window 24   # alle 5 Stufen in-memory, druckt Briefing
# einzelne DB-Stufen (brauchen Migration 0002):
python -m agents.run triage --window 24
python -m agents.run analyst   # usw.
```

## n8n-Setup (einmalig)

1. **Migration** `supabase/migrations/0002_agents.sql` im Supabase-SQL-Editor ausführen
   (legt `briefing_runs` an).
2. **SSH-Credential** in n8n anlegen (Credentials → SSH):
   - Host: die VPS-Adresse (vom n8n-Container aus erreichbar)
   - User: `root` (oder der User mit gültiger Claude-Code-Auth in `~/.claude`)
   - Private Key: ein Key, dessen Public-Key in `~/.ssh/authorized_keys` des VPS liegt
   - *Wichtig:* `claude` muss für diesen SSH-User funktionieren (eingeloggt sein).
3. **Telegram-Credential** anlegen (Bot-Token aus `macro-agent/.env` → `TELEGRAM_BOT_TOKEN`).
4. **Gmail-OAuth2-Credential** anlegen (Credentials → Gmail OAuth2):
   - In der Google Cloud Console ein OAuth-Client (Web) erstellen, Gmail-API aktivieren
   - n8n zeigt dir die Redirect-URL → in der Cloud Console als „Authorized redirect URI"
     eintragen; dann in n8n „Sign in with Google" klicken
   - (n8n-Doku: Gmail-Credential — führt Schritt für Schritt durch)
5. **Workflow importieren**: `n8n/ai_tech_briefing.workflow.json`
   (Workflows → Import from File).
6. Nach dem Import zuweisen:
   - jede SSH-Node → **SSH-Credential** (`REPLACE_SSH_CRED`)
   - Telegram-Node → Credential + `chatId` (`REPLACE_CHAT_ID` → `TELEGRAM_CHAT_ID`)
   - Gmail-Node → **Gmail-OAuth2-Credential** (`REPLACE_GMAIL_CRED`); `sendTo` ist auf
     philipp.baro@gmail.com vorbelegt. Prüfe, dass „Email Type = HTML" gesetzt ist.
7. Workflow aktivieren. Standard-Schedule: werktags 06:30 (Cron `30 6 * * 1-5`).

Der Editor liefert Markdown. Telegram bekommt es direkt; die E-Mail läuft über einen
**Markdown→HTML**-Node und geht **formatiert (HTML)** an den Gmail-Node. Beide Kanäle
feuern parallel — brauchst du nur einen, lösch den anderen Zweig.

## Hinweise

- Telegram-Limit 4096 Zeichen → der Editor hält das Briefing bewusst < ~3500.
  Volle Thesen/Kritiken liegen in `briefing_runs` (jsonb) für ein späteres Dashboard.
- Tool-Use ist in den Calls deaktiviert (`--disallowedTools`) → reine Reasoning-Calls.
- Fehlerfall: die betroffene Zeile bekommt `status='error'` + `error`-Text;
  der n8n-Lauf bricht an der Stufe ab (Exit-Code 1).
