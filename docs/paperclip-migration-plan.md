# AI/Tech Fund → Paperclip — Migrationsplan

> Ziel: Den bestehenden automatisierten AI/Tech-Fund (Daten-Ingestion → 5-Agenten-Investment-Committee → Briefing → Telegram/Dashboard) als **„Zero-Person Company" in Paperclip** abbilden — mit echten Paperclip-**Agents**, **Instructions** und **Skills**, statt n8n als Orchestrator.

Stand heute: `~/ai-tech-fund` (Python-Ingestion + Supabase + n8n + Dashboard), Briefing-Pipeline läuft headless über `claude -p` auf Opus 4.7. Paperclip ist installiert und öffentlich unter https://paperclip.hedgingalpha.com (authenticated mode, läuft als systemd-Service unter User `paperclip`).

---

## 0. Leitidee (aus dem Paperclip-Modell)

Paperclip ist das **Control Plane einer Firma**, nicht ein einzelner Agent:

| Paperclip-Baustein | Bedeutung | Bei uns |
|---|---|---|
| **Company** | Die Firma / das Org-Konstrukt | „AI/Tech Fund" |
| **Agent** | Mitarbeiter mit Titel, Boss (`reportsTo`), Job-Description, Budget | Triage / Analyst / Thesis / Devil / Editor / Data-Ops |
| **Issue / Ticket** | Arbeitsauftrag, voll getract (jede Instruktion, Tool-Call, Entscheidung) | „Tages-Briefing erstellen", „Adapter X reparieren" |
| **Instructions** | Die Job-Description + firmenweite Regeln | Unsere System-Prompts + Investment-Policy |
| **Skill** | Wiederverwendbare Fähigkeit (SKILLS.md + Code), zur Laufzeit injiziert | Ingestion-Adapter, JSON-Schema-Check, Telegram-Versand, Dashboard-Build |
| **MCP-Server** | Externe Tool-/Daten-Anbindung | Supabase (DB), Telegram |
| **Approval / Budget** | Governance: nichts Großes ohne Freigabe; Kostenlimits | Trade-Empfehlungen, Modellkosten/Monat |

**Konsequenz:** Die 5 Pipeline-Stufen werden zu **Agenten mit Job-Descriptions**. Die Python-Adapter und Delivery werden zu **Skills** (Code bleibt erhalten, wird nur „aufrufbar" gemacht). n8n entfällt als Orchestrator — Paperclip plant, triggert und protokolliert die Arbeit; der Cron-Trigger wird zur Paperclip-Routine.

---

## 1. Voraussetzung / aktueller Blocker (zuerst lösen)

**Problem:** Agenten laufen als User `paperclip`. Der hat **keinen Zugriff auf `/root/ai-tech-fund`** (`/root` ist `0700`). Zusätzlich erwartet die Agenten-Runtime ein **Git-Repo als Workspace** (daher `fatal: not a git repository`).

**Lösung — Repo in einen paperclip-zugänglichen Workspace bringen:**
1. Repo klonen nach `/srv/ai-tech-fund` (oder `/home/paperclip/ai-tech-fund`), Owner `paperclip`:
   ```bash
   sudo install -d -o paperclip -g paperclip /srv/ai-tech-fund
   sudo -u paperclip git clone git@github.com:PJOrab/ai-tech-fund.git /srv/ai-tech-fund
   # Deploy-Key/SSH für paperclip einrichten ODER HTTPS+PAT verwenden
   ```
2. `.env`, `venv` und Secrets für den `paperclip`-User bereitstellen (Supabase-Keys, macro-agent-Pfad). macro-agent ebenfalls paperclip-lesbar machen (klonen/kopieren nach `/srv/macro-agent`).
3. In Paperclip diesen Pfad als **Workspace** des Company/der Agenten konfigurieren, damit `claude` dort im Git-Repo startet.

> Damit ist der Git-Fehler weg und die Agenten arbeiten auf dem echten Code.

---

## 2. Ziel-Org-Chart (Agents)

```
CEO (du, Mensch)
  └─ PM / Chief Investment Officer (Agent, „orchestrator")
        ├─ Data-Ops (Agent)        — Ingestion-Health, Adapter-Fixes
        ├─ Triage (Agent)          — Feed → ~12 Cluster
        ├─ Analyst (Agent)         — Cluster → Impact/Direction/Magnitude
        ├─ Thesis (Agent)          — 3–5 investierbare Thesen
        ├─ Devil's Advocate (Agent)— Red-Team jede These  ← Kern-Differentiator
        └─ Editor (Agent)          — CEO-Briefing (DE, Telegram-tauglich)
```

- **PM/CIO** ist der einzige Agent, der die Tages-Routine „besitzt": er erzeugt Sub-Tickets an die Stufen-Agenten und setzt am Ende das Briefing zusammen. (Alternativ Stufen sequenziell als ein Ticket — siehe Phasen.)
- Jeder Agent bekommt **Titel, `reportsTo`, Job-Description, Monatsbudget** (Kostenkontrolle).
- Modelle pro Rolle konfigurierbar (Start: alle Opus 4.7; später Triage/Analyst auf günstigere Modelle wie heute geplant).

---

## 3. Instructions (Job-Descriptions + Firmenregeln)

Zwei Ebenen:

**a) Firmenweite Instruktionen / `SKILLS.md`** (Company-Level, in jeden Agentenlauf injiziert):
- Investment-Policy: nur AI/Tech-Equities (Watchlist), Briefing-Kadenz, Risikohinweise, „keine echten Trades ohne Approval".
- Dat_enquellen-Reliability-Logik, Dedup-Regel (`content_hash`), Output-Sprache (Briefing auf Deutsch).
- Verweis auf die verfügbaren Skills + wann sie zu nutzen sind (Runtime-Skill-Discovery).

**b) Pro-Agent Job-Description** (1:1 aus `agents/prompts.py`):
- **Triage:** „Wähle NUR Items, die AI/Tech-Equities bewegen können … gruppiere in Cluster, mappe Ticker. Qualität > Quantität." → Output-Schema `{clusters:[…]}`.
- **Analyst:** Impact/Direction/Magnitude/Horizon je Cluster → `{analyses:[…]}`.
- **Thesis:** 3–5 Thesen mit bull/bear, Katalysatoren, Conviction → `{theses:[…]}`.
- **Devil's Advocate:** „Dein EINZIGER Job: jede These angreifen — stärkstes Gegenargument, was ist schon eingepreist, konkrete Falsifikationskriterien, blinder Fleck. Hart aber fair, keine Strohmänner." Bekommt Thesen **ohne** Pro-Argumente. → `{critiques:[…]}`.
- **Editor:** „Knappes deutsches CEO-Briefing; jede Top-Empfehlung direkt neben dem Devil's-Advocate-Gegenargument; < 3500 Zeichen." → Markdown.

> Diese Prompts existieren bereits — sie werden zu den Agenten-Instructions. Output-Schemas werden über einen **Validation-Skill** erzwungen.

---

## 4. Skills (Code bleibt, wird aufrufbar)

Skills = SKILLS.md (Beschreibung „wann/wie nutzen") + ausführbares Skript. Wir verpacken den vorhandenen Python-Code, statt ihn neu zu schreiben.

| Skill | Was es tut | Quelle |
|---|---|---|
| `ingest-feed` | Adapter-Lauf → Supabase `raw_items` | `python -m ingestion.run_ingest` |
| `read-feed` | letzte N `raw_items` (Fenster, gefiltert) holen | Supabase-Query (MCP) |
| `validate-output` | JSON gegen Triage/Analyst/Thesis/Devil-Schema prüfen | neuer kleiner Validator |
| `persist-run` | Zwischenstände in `briefing_runs` schreiben/lesen (State-Machine) | DB (MCP/Skript) |
| `send-telegram` | Markdown-Briefing an Bot pushen | bestehender Telegram-Versand |
| `build-dashboard` | statisches HTML neu bauen (`/var/www/html/fund`) | `python -m dashboard.build` |
| `adapter-doctor` | Health-Check je Adapter (für Data-Ops) | aus `ingestion_runs` |

Die 8 Daten-Adapter (EDGAR, arXiv, GitHub-Trending, HN, Tech-RSS, NewsAPI, X, FRED) bleiben hinter `ingest-feed` gekapselt; die Watchlist (`watchlist.py`) wird ein Skill-Parameter/Config.

---

## 5. MCP-Server (Daten- & Delivery-Anbindung)

- **Supabase-MCP** (oder schlanker DB-Skill): Lese/Schreibzugriff auf `sources`, `raw_items`, `ingestion_runs`, `briefing_runs`. So können Agenten den Feed lesen und Zwischenstände persistieren, ohne dass jede Stufe Argumente durchreichen muss (State in DB — wie heute).
- **Telegram**: als Skill (`send-telegram`) oder MCP. Bot-Token + Chat-ID als Paperclip-Secret.
- Secrets (Supabase Service-Key, NewsAPI, FRED, X-Token, Telegram) gehören in **Paperclip Secrets**, nicht in Klartext-`.env` im Agenten-Workspace.

---

## 6. Orchestrierung & Scheduling (n8n → Paperclip)

- **Heute:** n8n-Cron `30 6 * * 1-5` → 6 SSH-Nodes → Telegram.
- **Neu:** Paperclip-**Routine/Schedule** (werktags 06:30) erzeugt ein Ticket „Tages-Briefing" beim PM/CIO. Der fährt die Stufen (entweder als ein durchgehender Agentenlauf oder als Sub-Tickets pro Stufe) und ruft am Ende `send-telegram` + `build-dashboard`.
- **Ingestion** (alle 30 min) und **Dashboard** (alle 15 min) können vorerst als Cron unter `paperclip` bleiben (rein mechanisch, kein LLM nötig) oder ebenfalls Paperclip-Routinen werden.
- **State-Machine** `briefing_runs` (`analyst → thesis → devil → editor → done`) bleibt erhalten — passt perfekt zu Paperclip-Tickets mit Zwischenständen.

---

## 7. Governance (der eigentliche Mehrwert ggü. n8n)

- **Budgets** pro Agent (Monats-Cents) → Kostenkontrolle der Opus-Läufe, sichtbar im Dashboard.
- **Approvals**: „echte" Aktionen (z. B. eine Trade-Empfehlung als verbindlich markieren, neue Datenquelle/Account hinzufügen, Agent einstellen) erfordern CEO-Freigabe. Briefing-Erstellung selbst läuft autonom.
- **Tracing**: jede Stufe, jeder Tool-Call, jede Entscheidung ist im Ticket nachvollziehbar — ersetzt die heutigen Log-Dateien.
- **Track-Record-Loop (Phase 3 des bestehenden Plans)**: ein „Performance"-Agent bewertet alte Thesen gegen tatsächliche Kursbewegungen → füttert Conviction-Kalibrierung. In Paperclip als wiederkehrendes Ticket + Skill (`score-past-calls`). Das ist laut Architektur „der größte Moat".

---

## 8. Migrationsphasen

**Phase 0 — Workspace & Zugriff (Blocker)**
- Repo + macro-agent + venv + Secrets paperclip-zugänglich machen (Abschnitt 1). Git-Fehler beheben. Smoke-Test: ein triviales Agent-Ticket läuft im Repo durch.

**Phase 1 — Company & Agenten anlegen**
- Company „AI/Tech Fund". 6 Agenten mit Titel/`reportsTo`/Budget. Job-Descriptions aus `prompts.py` einsetzen. Firmen-`SKILLS.md` (Policy + Skill-Index).

**Phase 2 — Kern-Skills + DB-Anbindung**
- Skills `read-feed`, `validate-output`, `persist-run`, `send-telegram`, `build-dashboard` registrieren. Supabase-MCP/DB-Skill verbinden. Secrets in Paperclip hinterlegen.

**Phase 3 — Briefing-Pipeline end-to-end**
- PM/CIO-Routine werktags 06:30. Erst manuell ein Ticket auslösen, Output gegen das heutige n8n-Briefing vergleichen (gleiche `briefing_runs`-Daten → gleiches Ergebnis?). Dann automatisieren.

**Phase 4 — Delivery & Dashboard**
- Telegram + Dashboard über Skills. n8n-Workflow deaktivieren, sobald Paperclip-Pfad stabil.

**Phase 5 — Governance & Track-Record**
- Budgets/Approvals scharf schalten. Performance-Agent + `score-past-calls`. Modelle pro Rolle optimieren (Triage/Analyst günstiger).

---

## 9. Offene Entscheidungen

1. **Migrationstiefe**: (a) Paperclip nur als Orchestrator/Supervisor über die bestehende Python/n8n-Pipeline, (b) Pipeline-Stufen als native Paperclip-Agenten neu, Adapter als Skills (empfohlen), (c) kompletter Rebuild inkl. Daten-Layer.
2. **Workspace-Ort**: `/srv/ai-tech-fund` vs `/home/paperclip/ai-tech-fund`.
3. **DB**: Supabase über MCP vs. schlanke DB-Skills; bleibt Supabase oder auf Paperclips eingebettetes Postgres?
4. **Secrets-Migration**: `.env` → Paperclip Secrets (welche Keys zuerst).
5. **Reihenfolge**: erst Workspace-Blocker + 1 Test-Agent, dann schrittweise Stufen?

---

## 10. Unmittelbar nächste Schritte (sobald Tiefe bestätigt)

1. `/srv/ai-tech-fund` als paperclip-Workspace einrichten (Repo + macro-agent + venv + Secrets).
2. Git-Fehler verifizieren: Test-Ticket an einen Agenten, der nur `git status` + `read-feed` macht.
3. Company + 1 Agent (Editor) anlegen und ein Briefing aus vorhandenen `briefing_runs`-Daten rendern lassen (kleinster sinnvoller End-to-End-Test).
4. Von dort die übrigen Stufen + Skills ergänzen.
