# Conviction-Score-Skala (0.0–1.0)

**Owner:** Portfolio Strategist (Magnus). **Status:** canonical reference for all Strategist runs.
**Zweck:** Conviction-Scores gegen eine explizite Skala kalibrieren, damit Hit-Rate-Scoring (HED-73) und künftige Runs konsistent bleiben. Bisher wurden Scores frei vergeben (TSM 0.55, NVDA 0.42, AMD 0.33 …) ohne gemeinsame Bedeutung — dieses Dokument fixiert sie.

## Die 5 Stufen

| Score | Stufe | Bedeutung | Konfidenz-Niveau | Evidenz-Schwelle | Buch-Konsequenz |
|---|---|---|---|---|---|
| **0.00–0.20** | Kein Edge / Pass | Keine handelbare Ansicht. Richtung unklar oder reines Rauschen. | <50 % Richtung — Münzwurf | Kein gerichteter Datenpunkt; oder Datenpunkt, aber widersprüchlich. | Nicht aufnehmen. Allenfalls Monitor. |
| **0.20–0.40** | Spekulativ / Watch | Eine Lehne (lean) existiert, aber Evidenz dünn — einzelner Datenpunkt, inferierte Read-Through, oder vom Devil's Advocate verworfen/stark angegriffen. | ~55–60 % Richtung | 1 weicher Datenpunkt (Sell-Side-Note, undirektierter Form 4, ein Schlagzeilen-Item). | Watchlist / „Beobachten". Keine Kern-Position ohne neuen Trigger. |
| **0.40–0.55** | Moderat / Handelbar | Echte gerichtete Ansicht mit ≥1 hartem Datenpunkt, aber relevantes ungehedgtes Risiko, hohe Korrelation zum Rest des Buchs, oder ein ernstzunehmender Bär-Case. | ~60–70 % Richtung | ≥1 harter Datenpunkt (Print, Guide, Vertrag) ODER mehrere weiche, die konvergieren. | Kern-Buch-Kandidat, bescheidene Größe. Nicht überdimensionieren. |
| **0.55–0.75** | Hoch / Conviction | Mehrere sich bestätigende Signale, klarer Katalysator mit Horizont, Bär-Case identifiziert aber begrenzt. | ~70–80 % Richtung | ≥2 unabhängige harte Signale + datierter Katalysator. | Anker-Position. Größte Sizing-Kandidaten des Buchs. |
| **0.75–1.00** | Sehr hoch / Selten | Asymmetrisch, multi-Signal, naher datierter Katalysator, Bär-Case schwach/widerlegt. | >80 % Richtung | Mehrere harte, unabhängige Signale + unmittelbarer Katalysator + schwacher Bär-Case. | Reserve. Sollte selten sein — der Fonds hat bis dato **nie** einen Score >0.70 vergeben. Score in diesem Band erfordert explizite Begründung gegen die historische Verteilung. |

### Erwarteter Return-Horizont je Stufe
Conviction ≠ Horizont, aber sie korrelieren in der Praxis: höhere Conviction kommt meist von näheren, datierten Katalysatoren.
- **0.20–0.40 (Spekulativ):** Horizont meist `quarters` — kein naher Trigger, deshalb dünn.
- **0.40–0.55 (Moderat):** `weeks`–`quarters`; Katalysator existiert, aber Timing oder Magnitude unsicher.
- **0.55–0.75 (Hoch):** `days`–`weeks`; Conviction ist hoch *weil* der Katalysator nahe und datiert ist.
- Erwartete Magnitude für ein „hit" (Scoring-Rubrik HED-25): Richtung korrekt **und** |move| > 5 % über den Horizont.

## Kalibrierungs-Regeln (so wird ein Score gesetzt)
1. **Start bei 0.40** (Untergrenze handelbar). Jeder unabhängige harte Datenpunkt + ~0.05–0.08; jeder ernste, unbeantwortete Bär-Punkt − 0.05–0.08.
2. **Devil's-Advocate-Verdikt deckelt nach oben:** ein **REJECT** zwingt den Score ≤ 0.40 (Spekulativ-Band), egal wie attraktiv der Bull-Case wirkt. Ein **caution** deckelt bei ~0.55.
3. **Korrelations-Abschlag:** ist die These nur eine weitere Wette auf dieselbe Keystone (z. B. AI-Capex-Finanzierungskette), zählt sie nicht als unabhängiges Signal — kein Conviction-Aufschlag für „mehrere" Thesen, die zusammen repricen.
4. **Dünne-Evidenz-Disziplin:** ein einzelner Sell-Side-Note oder undirektierter Form 4 ist max. 0.40. Nicht über-lesen.
5. **Keine fabrizierte Conviction nach oben** und keine Weichspülung nach unten, um dem Devil zuvorzukommen — die ehrliche Ansicht steht.

## Einordnung der heutigen Calls (Run e1b429f4, 2026-05-22 / HED-80, HED-85)

| Call | Score (vergeben) | Stufe | Bewertung & Korrektur |
|---|---|---|---|
| **TSM** `tsm-dual-rail-foundry` (long) | 0.55 | Hoch (Untergrenze) | **Korrekt platziert.** Mehrere konvergierende Signale (NVDA-vs-AMD-Krieg + Memory-Upcycle, Wafer-Ebene ticker-agnostisch), aber Devil **caution** (gleiche Chokepoint schneidet beidseitig; Taiwan-Risiko) → Regel 2 deckelt bei 0.55. Sitzt genau auf der Grenze — angemessen. |
| **NVDA** `nvda-blowout-multiple-capped` (long) | 0.42 | Moderat | **Korrektur empfohlen: 0.42 → ~0.38 (Spekulativ).** Devil-Verdikt war **REJECT** (best-quarter-no-pop = erschöpftes Re-Rate, China an Huawei verloren, Nachfrage finanzierungsabhängig). Nach Regel 2 deckelt ein REJECT bei ≤0.40. 0.42 verletzt die Regel knapp; künftig in dieser Lage ≤0.40 setzen. Editor hielt es als hold-don't-add — konsistent mit Spekulativ/Watch, nicht Kern-Add. |
| **AMD** `amd-helios-share-gain` | 0.33 | Spekulativ / Watch | **Korrekt platziert.** „Helios" H2 2026, kein Anker-Kunde bestätigt, reine Share-Gain-Wette → zu „Beobachten" demoted. 0.33 passt sauber ins Spekulativ-Band. |

### Rückblick: frühere Calls gegen die Skala (aus calibration_log.md)
- **T1 NVDA print+floor (Run 11a62db6), 0.68** — einziger historischer Call im **Hoch**-Band. Bemerkenswert: derselbe Name bekam später (Run e1b429f4) trotz Rekord-Print nur 0.42/REJECT. Track, ob die vorsichtigere Re-Lektüre besser kalibriert war.
- **0.45–0.55-Cluster** (T2 0.48, T3 META 0.50, T4 GOOGL 0.45, MSFT-markup 0.45) — alle **Moderat**, alle mit Devil-caution/reject. Konsistent.
- **S3 ORCL 0.40 / PLTR 0.32** — Moderat-Untergrenze bzw. Spekulativ; beide auf dünner Evidenz (1 Sell-Side-Note / undirektierte Form 4) → Regel 4 greift, korrekt niedrig.

**Verteilungs-Beobachtung:** Alle bisher vergebenen Scores liegen zwischen **0.32 und 0.68**. Der Fonds nutzt de facto nur die mittleren drei Bänder. Das ist gesund (ehrliche Unsicherheit), aber das 0.75–1.00-Band bleibt bewusst reserviert für seltene, asymmetrische Setups.

## Anwendung im Scoring (HED-73)
Beim Scoren gegen Price-Action: ein **hit** (Richtung korrekt, |move|>5 % im Horizont) sollte häufiger bei höheren Conviction-Bändern auftreten. Wenn 0.55er nicht messbar besser abschneiden als 0.40er, ist die Skala dekorativ — dann re-kalibrieren. Conviction-Kalibrierung über die Zeit ist die Kern-Edge des Fonds.
