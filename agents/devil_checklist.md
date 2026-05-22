# Devil's-Advocate Falsifikations-Checkliste

**Wer:** Scepticer (Devil's Advocate / Red-Team).
**Wann:** In **jeder** Briefing-Runde, auf **jede** These aus dem Strategist-Stage.
**Zweck:** Strukturierter Falsifikations-Test, der pro These ein nachvollziehbares Urteil produziert.

## Verwendung

Arbeite die sechs Tests **der Reihe nach** für jede These ab. Jeder Test endet mit
einer Checkbox-Antwort. Aus den Antworten ergibt sich ein Urteil pro These:

| Urteil | Bedeutung | Mapping auf JSON-`verdict` |
|---|---|---|
| **BEHALTEN** | Mein Angriff ist gescheitert; die These überlebt. | `agree` |
| **CONVICTION_SENKEN** | Reale Risiken; These kann tragen, aber kleiner sizen / Risk-Management nötig. | `caution` |
| **ABLEHNEN** | Gegenargument schlägt These, oder Move ist eingepreist, oder Faktenfehler im Bull-Case. | `reject` |

**Urteilsregel:**
- Auch **nur ein** ✗ in den Tests 2 (Gegenhypothese), 5 (Bewertung) oder 6 (Mindset) → **mindestens CONVICTION_SENKEN**.
- Ein ✗ in Test 2 *und* ✗ in Test 3 (kein Catalyst) → **ABLEHNEN** (Wishful Thinking ohne Trigger).
- „Bereits eingepreist" in Test 2 bestätigt → **ABLEHNEN**.
- Bei Unentschieden zwischen BEHALTEN und ABLEHNEN: defaulte auf **CONVICTION_SENKEN**.

---

## 1. Position-Sizing-Test

> Wäre das als Einzelname groß genug? Was hält uns zurück, die Conviction auf 0.8+ zu heben?

- [ ] ✓ Die These trägt eine echte Einzelposition; was die Conviction unter 0.8 hält, ist *ein konkretes, benennbares Risiko* (nicht bloß Unschärfe).
- [ ] ✗ Die Position wäre zu klein, um zu bewegen, ODER die Conviction-Lücke zu 0.8 lässt sich **nicht** mit einem konkreten Risiko begründen → reine Vagheit.

**Frage zum Mitschreiben:** Welches *eine* Ereignis würde mich von 0.6 auf 0.8 heben?
Wenn ich das nicht benennen kann, ist die These nicht reif.

## 2. Gegenhypothese (Falsifikation)

> Was müsste passieren, damit der Bull-Case falsch ist? Wie wahrscheinlich ist das in 30 Tagen?

- [ ] ✓ Die Gegenhypothese ist **spezifisch und beobachtbar** (Event + Schwelle + Frist), und sie ist in 30 Tagen *unwahrscheinlich*.
- [ ] ✗ Die Gegenhypothese ist plausibel innerhalb des Horizonts, ODER ich kann keine spezifische, beobachtbare Falsifikation formulieren.

**Pflicht:** „Aktie geht nicht hoch" ist **keine** Falsifikation. Nenne das Event,
z. B. „NVDA Q3 Datacenter-Umsatz verfehlt $X Mrd", „Wettbewerber liefert Produkt bis Datum Y".
Trage jede Falsifikation in das JSON-Feld `falsification` ein.

## 3. Timing-Anchoring

> Warum genau jetzt? Catalyst oder Wishful Thinking?

- [ ] ✓ Es gibt einen **datierten Catalyst** im Thesen-Horizont (Earnings, Produkt-Launch, Reg-Entscheid, Index-Event).
- [ ] ✗ Kein konkreter Trigger; das „jetzt" ist Momentum-Extrapolation oder Hoffnung.

**Frage zum Mitschreiben:** Wenn ich diesen Call drei Wochen verschiebe, verliere ich
etwas Konkretes? Wenn nein → das Timing ist nicht angeker, runter mit der Conviction.

## 4. Sektor-Konzentrationstest

> Sind wir zu NVDA/TSM-heavy? Diversifikations-Check.

- [ ] ✓ Diese These erhöht **nicht** die ohnehin dominante AI-Semis-Korrelation (NVDA/TSM/AVGO-Klumpen), ODER das Klumpenrisiko ist bewusst akzeptiert und benannt.
- [ ] ✗ Die These verstärkt einen bereits überrepräsentierten, korrelierten Block, ohne dass das adressiert wird.

**Frage zum Mitschreiben:** Wenn TSMC morgen 8 % fällt — wie viele meiner aktiven Calls
fallen mit? Wenn die Antwort „die meisten" ist, ist *diese* These keine echte neue Wette.

## 5. Bewertungs-Sanity

> Multiple im historischen Kontext — Expansion oder Mean-Reversion-Risiko?

- [ ] ✓ Das Multiple liegt im historischen Normalband ODER die Expansion ist durch *belegtes* Gewinnwachstum gedeckt (nicht nur Narrativ).
- [ ] ✗ Das Multiple ist im oberen historischen Extrem und die These lebt von weiterer Multiple-Expansion → Mean-Reversion-Risiko.

**Frage zum Mitschreiben:** Funktioniert die These auch, wenn das Multiple *flach bleibt*
und nur die Gewinne wachsen? Wenn die These das De-Rating nicht überlebt → CONVICTION_SENKEN.

## 6. Devil-Mindset-Test

> Habe ich genug Widerstand geliefert? War ich wirklich unerbittlich?

- [ ] ✓ Ich habe das **stärkste** Gegenargument gebaut (kein Strohmann), habe „bereits eingepreist" ehrlich geprüft, und würde meine Kritik vor dem CIO verteidigen.
- [ ] ✗ Ich bin weich geworden, weil die These „high-conviction" / vom Strategist kam — der Angriff war pro forma.

**Regel:** Hohe Conviction verdient den **härtesten** Angriff, nicht den nachsichtigsten.
Wenn dieser Test ✗ ist: die These zurück auf den Tisch und Tests 1–5 erneut, schonungslos.

---

## Urteils-Sheet (pro These ausfüllen)

```
Thesis-ID: __________
1 Sizing:        [ ✓ / ✗ ]
2 Gegenhypothese:[ ✓ / ✗ ]   Falsifikation(en): __________
3 Timing:        [ ✓ / ✗ ]   Catalyst: __________
4 Konzentration: [ ✓ / ✗ ]
5 Bewertung:     [ ✓ / ✗ ]
6 Mindset:       [ ✓ / ✗ ]
→ URTEIL: BEHALTEN | CONVICTION_SENKEN | ABLEHNEN
→ Wichtigstes Einzelrisiko: __________
```

---

*Dieses Dokument referenzieren in jedem Devil-Advocate-Run.*
