# AI/Tech Equity Intelligence Firm — Architektur

> Investment-Firma mit Fokus auf AI/Tech **Public Equities**. Ein Newsfeed aus
> Scrapern/APIs speist ein Gremium aus AI-Agenten (orchestriert über n8n), die
> Nachrichten analysieren und dem CEO täglich ein Briefing mit
> **Handlungsempfehlung + Devil's Advocate** liefern.

**Festlegungen:** Public Equities · Daily Briefing (1–2×/Tag) · n8n self-hosted ·
Daten Lean, LLM großzügig.

---

## 0. Budget-Realität: Ist „Lean" bei Daten konkurrenzfähig?

**Ja — für genau diesen Fall.** Bei Daily-Briefing-Kadenz ist Latenz egal,
Synthese ist alles. Teure Terminals (Bloomberg/Refinitiv) und Tick-Daten braucht
nur, wer in Millisekunden tradet. Der Edge liegt zu ~90 % in der
**Agenten-/Kuratierungsschicht**, nicht im Daten-Einkauf.

| Kostenlos / fast gratis (reicht für 90 %) | Lohnt sich später als gezieltes Upgrade ($) |
|---|---|
| SEC EDGAR (8-K, 10-Q, 13F-Holdings, **Form 4 Insider-Trades**) | Earnings-Call-Transkripte als saubere API (sonst scrapen) |
| FRED (Makro), Zentralbank-Feeds | Options-Flow / Unusual-Activity-Feed |
| Yahoo/stooq Kursdaten, IR-RSS der Firmen | Ein konsolidierter Finanz-News-Aggregator (Benzinga o.ä.) |
| **arXiv + GitHub-Trends + Hacker News** (AI-Research-Frühsignal!) | Alt-Data (nur bei echtem Scale sinnvoll) |
| GDELT, X/Twitter, Reddit, Substack-Analysten | |

**Verdict:** Voll Lean starten. Einziges empfohlenes Daten-Upgrade auf Sicht:
ein News-Aggregator (~$50–150/Mo), damit nicht 20 fragile HTML-Scraper gepflegt
werden müssen. Beim LLM Gas geben (Opus für Debatte/Synthese, Haiku für Triage).

**Recycling:** `macro-agent` hat bereits funktionierende Adapter für FRED, GDELT,
NewsAPI, X, Zentralbanken — die werden wiederverwendet.

---

## 1. Leitprinzip

**n8n ist der Dirigent, nicht der Arbeiter.** Schweres Scraping, Embeddings und
Agenten-Loops laufen in **Python-Microservices** (FastAPI), die den vorhandenen
`macro-agent`-Code wiederverwenden. n8n macht Scheduling, Glue, LLM-Calls,
Routing und Delivery. So bleibt n8n schnell und es geht keine Zeit damit verloren,
Scraper in n8n-Nodes nachzubauen.

---

## 2. Gesamtbild

```
┌──────────────────────────────────────────────────────────────────────┐
│ 1) INGESTION (Python-Services, recycelt aus macro-agent)               │
│    EDGAR · IR-RSS · FRED · GDELT · NewsAPI · X · Reddit · HN · arXiv ·  │
│    GitHub-Trends · Earnings-Kalender · Kursdaten                        │
│         │  (alle normalisieren → ein "Event/Doc"-Schema)               │
└─────────┼──────────────────────────────────────────────────────────────┘
          ▼
┌──────────────────────────────────────────────────────────────────────┐
│ 2) STORE       Postgres + pgvector                                      │
│    raw_items · events(entity/ticker, importance) · theses · briefings · │
│    positions · track_record                                             │
└─────────┬──────────────────────────────────────────────────────────────┘
          ▼
┌──────────────────────────────────────────────────────────────────────┐
│ 3) ENRICHMENT (Haiku, billig & schnell)                                 │
│    Dedup → Ticker-/Entity-Mapping → Relevanz-Filter → Importance-Score  │
│    → semantische Cluster (Vektor)                                       │
└─────────┬──────────────────────────────────────────────────────────────┘
          ▼
┌──────────────────────────────────────────────────────────────────────┐
│ 4) AGENT-GREMIUM  (n8n orchestriert, Claude Opus/Sonnet)               │
│                                                                         │
│   Scout/Triage → wählt die ~15 wichtigsten Cluster des Tages           │
│        │                                                                │
│        ├─► Sektor-Analysten (parallel):                                │
│        │     • Semis & Hardware (NVDA/AMD/TSMC/ASML)                    │
│        │     • Hyperscaler & Cloud (MSFT/GOOGL/AMZN/Power & Energy)     │
│        │     • AI-Software/Apps & Modell-Anbieter                       │
│        │     • Makro/Rates/FX-Overlay (was killt die These?)           │
│        │                                                                │
│        ├─► Fundamental-Agent (Bewertung, Guidance, Insider/13F)        │
│        ├─► Sentiment/Flow-Agent (X/Reddit/Options, Crowding)           │
│        │                                                                │
│        ▼                                                                │
│   THESE-AGENT  ──►  baut Bull-/Bear-Case + Trade-Idee + Sizing         │
│        │                                                                │
│        ▼                                                                │
│   DEVIL'S ADVOCATE (Red-Team) ──► greift jede These an, sucht          │
│        │            Falsifikation, Gegen-Evidenz, „Was übersehen wir?" │
│        ▼                                                                │
│   PM/RISK-AGENT ──► gleicht mit offenen Positionen + Track-Record ab,  │
│        │            finalisiert Conviction & Risiko-%                  │
│        ▼                                                                │
│   CHIEF-OF-STAFF/EDITOR ──► verdichtet zum CEO-Briefing                │
└─────────┬──────────────────────────────────────────────────────────────┘
          ▼
┌──────────────────────────────────────────────────────────────────────┐
│ 5) DELIVERY     HTML-Dashboard · Telegram · E-Mail (Gmail-MCP)         │
│ 6) FEEDBACK     Track-Record-Job bewertet alte Calls → kalibriert      │
│                 Agent-Conviction (lernt, wer recht hatte)             │
└──────────────────────────────────────────────────────────────────────┘
```

---

## 3. Das Agenten-Gremium (Kern des Edges)

Bewusst als **„Investment Committee"** modelliert — jeder Agent ist ein
System-Prompt mit klarer Rolle, eigenem Datenzugriff und Output-Schema:

1. **Scout/Triage** (Haiku) — siebt aus hunderten Items die ~15 relevantesten
   Cluster. Billig, hohe Frequenz.
2. **Sektor-Analysten** (Sonnet/Opus, parallel) — je ein Spezialist für Semis,
   Hyperscaler/Cloud+Power, AI-Software, plus ein **Makro-Overlay**. Jeder
   liefert: Faktenlage, betroffene Tickers, Richtung, Unsicherheit.
3. **Fundamental-Agent** — Bewertung, Guidance-Revisionen, **Insider-Käufe
   (Form 4) & 13F-Bewegungen** der „Smart Money".
4. **Sentiment/Flow-Agent** — Crowding-Check: Wo ist der Konsens schon
   eingepreist?
5. **These-Agent** — formuliert investierbare These mit Bull/Bear,
   Katalysatoren, Zeithorizont, Trade-Struktur.
6. **Devil's Advocate / Red-Team** (Opus) — **Kern-Feature**: bekommt die These
   *ohne* die Pro-Argumente zu kennen, muss sie zerlegen, Falsifikationskriterien
   nennen und das stärkste Gegenargument liefern. Output landet sichtbar neben
   jeder Empfehlung.
7. **PM/Risk-Agent** — Abgleich mit Portfolio, Korrelations-/Klumpenrisiko,
   finale Conviction + Risiko-%.
8. **Chief-of-Staff/Editor** (Opus) — schreibt das **CEO-Briefing**: Executive
   Summary → Top-Calls mit Handlungsempfehlung → daneben jeweils der
   Devil's-Advocate-Einwand → Watchlist → Risiko-Radar.

---

## 4. n8n-Workflows (4 Stück)

- **WF-1 Ingestion** — Cron alle 15–30 Min: triggert die Python-Scraper-Services,
  schreibt nach Postgres.
- **WF-2 Enrichment** — getriggert bei neuen Items: Dedup/Mapping/Scoring (Haiku).
- **WF-3 Morning-Briefing** — Cron z. B. 06:30 + 13:00: orchestriert die komplette
  Agenten-Kette (Triage → Analysten → These → Devil's Advocate → PM → Editor) →
  Dashboard + Telegram/E-Mail.
- **WF-4 Track-Record** — nachbörslich: bewertet vergangene Calls gegen
  tatsächliche Kursbewegung → kalibriert Conviction. **Langfristig der größte
  Moat** — das System lernt, welchen Agenten/Thesen-Typen man trauen kann.

---

## 5. Tech-Stack (alles self-hosted, Docker-Compose)

- **n8n** (Orchestrierung) + **Postgres/pgvector** (Daten + Vektoren + n8n-DB)
- **Python/FastAPI-Service** „intel-svc" — recycelt `macro-agent`-Adapter,
  liefert normalisierte Events; n8n ruft per HTTP
- **Claude API** — Tiered (Opus Debatte/Editor, Sonnet Analysten, Haiku Triage);
  Prompt-Caching für die Daten-Kontexte
- **Delivery** — HTML-Dashboard (Vorlage aus `macro-agent`), Telegram-Bot,
  Gmail-MCP für E-Mail

---

## 6. Phasenplan

- **Phase 0 (1–2 Tage):** Docker-Compose: n8n + Postgres+pgvector. `macro-agent`-
  Adapter in FastAPI-Service kapseln. Schema definieren.
- **Phase 1 — MVP (1 Woche):** Ingestion (EDGAR + IR-RSS + News + X) → Triage →
  **1 Analyst → These → Devil's Advocate → Editor** → Telegram-Briefing. Eine
  durchgehende Kette, die jeden Morgen feuert.
- **Phase 2:** Sektor-Analysten parallelisieren, Fundamental + Sentiment dazu,
  Dashboard.
- **Phase 3:** Track-Record-Feedback-Loop + Conviction-Kalibrierung +
  Portfolio-Abgleich.

---

## 7. Offene nächste Schritte

- **(a)** `docker-compose.yml` + Postgres-Schema + FastAPI-Wrapper um die
  `macro-agent`-Adapter scaffolden (Phase 0). *Empfohlen.*
- **(b)** System-Prompts des Agenten-Gremiums (inkl. Devil's Advocate)
  ausformulieren.
