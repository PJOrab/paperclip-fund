# Briefing-Template (v2 — HED-30, 2026-05-21)

Wiederverwendbare Vorlage für das tägliche CEO-Briefing. Straffe, kurze,
verständliche Struktur. Gilt für den Editor (manuell) UND die automatische
Pipeline (`prompts.py`).

## STEP 0 — PFLICHT vor jedem Briefing
Lies zuerst `agents/ceo_preferences.md` (bzw. para-memory-Schlüssel
`CEO-Praeferenzen`) und wende ALLE stehenden Regeln an. Der CEO soll dasselbe
Feedback nie zweimal geben müssen. Die CEO-Präferenzen schlagen diese Vorlage,
falls etwas kollidiert.

## Harte Regeln (CEO-Standing-Prefs)
- **Länge:** ~1500–2000 Zeichen gesamt. Ein Handy-Screen, Lesezeit < 1 Minute.
  Im Zweifel den schwächsten Call streichen — NIE die Erklärungen.
- **Calls:** MAX 2–3 Top-Calls. Lieber 2 starke als 3 mittelmäßige.
- **Sprache:** Lage zuerst erklären (Leser hat den Tag nicht verfolgt), dann
  der Punkt. Jeden Fachbegriff/jedes Akronym beim ersten Mal in Klammern in
  Alltagssprache erklären — oder weglassen.
- **Devil's Advocate:** zu jedem Call das stärkste Gegenargument zeigen. Nie
  weglassen, um einen Call stärker aussehen zu lassen.

## Struktur
```
<Überschrift mit Datum>

Δ seit gestern
1 Satz: das eine große Thema bzw. was sich seit gestern geändert hat — und
warum es unsere Aktien bewegt.

Top-Calls            (MAX 2–3)
N) <Ticker> — <Long/Short> · Conviction <0,xx>
   1 Satz: was ist passiert und warum bewegt es die Aktie (Fachbegriff erklärt).
   ⚖️ Gegenargument: 1 Zeile — stärkster Devil's-Advocate-Einwand, verständlich.
   👉 Fazit: 1 Zeile — was das praktisch heißt (z. B. „halten, klein" / „abwarten bis …").

Beobachten
• 1 Zeile: Ereignis + warum es zählt.

Risiko
• 1 Zeile: das eine, was alle Calls gleichzeitig kippen könnte.
```

## Format-Hinweise
- Editor (manuell, Telegram): `<b>…</b>` für Betonung, `parse_mode=HTML`,
  literale `< > &` als `&lt; &gt; &amp;` escapen. KEIN Markdown.
- Pipeline (`prompts.py`): Markdown-Überschriften wie in `editor_user()`.
- Nach dem Schreiben: Länge prüfen (≤~1200), `persist-run` (status=done),
  `send-telegram`. Über Budget → kürzen und erneut senden, erst dann „done".
