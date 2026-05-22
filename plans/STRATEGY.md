# Strategisches Mandat — CEO/Investor-Perspektive

**Pflichtlektüre am Anfang jedes Zyklus. Wenn dein geplantes Work keinen der drei Hebel unten bewegt, wähl eine andere Aufgabe.**

---

## Wo wir stehen

Der AI/Tech Fund hat das Gerüst einer Researchoperation, aber noch nicht die Tiefe. Wir ingestieren Daten, produzieren tägliche Briefings, haben ein Dashboard. Ein kritischer Investor würde sagen: *die Infrastruktur ist da, aber der Output ist noch nicht investment-grade.*

Das ändert sich jetzt. Die drei strategischen Lücken sind die einzige Priorität.

---

## Die drei Hebel — in Prioritätsreihenfolge

### 1. Datentiefe und -menge

**Ist-Zustand:** Wir lesen RSS-Feeds, EDGAR-Filings, ein paar Makro-Overlays. Breit, aber flach — wir lesen Headlines, wir bauen keine Überzeugung.

**Was fehlt (konkrete Targets):**

| Datenquelle | Signal | Schwierigkeit |
|---|---|---|
| Earnings-Call-Transkripte | Mgmt-Ton, Forward Guidance, unerwartete Formulierungen | mittel |
| Optionsmarkt (Put/Call-Ratio, IV-Skew) | Institutionelles Positioning, erwartete Moves | mittel |
| Job-Posting-Velocity | Forward-Revenue-Indikator, Kapazitätsaufbau | niedrig |
| Short-Interest-Daten | Contrarian-Signal, Squeeze-Setup-Erkennung | niedrig |
| Supply-Chain-Signale (TSMC-Aufträge, Equipment-Bookings) | Leading Indicator für Semiconductor-Zyklus | hoch |
| 10-K/10-Q-Sprach-Sentiment | Mgmt-Risikowahrnehmung über Zeit | mittel |
| Insider-Transaktionen (dollar-gewichtet, nicht nur Richtung) | Conviction-Signal | niedrig |
| Historische Kursdaten + technische Levels | Kontext für Thesis-Timing | niedrig |

**Was ein guter Zyklus aussieht:** Eine neue tiefe Datenquelle, die nicht-konsensuales Signalvoraus liefert. Nicht das 16. RSS-Feed — eine Quelle, für deren Aufbau ein Konkurrent-Quant-Team einen Monat bräuchte.

**Messlatte:** Würde ein Hedgefonds-Analyst sagen "das gibt uns eine Informationskante"? Dann ja.

---

### 2. Analysequalität

**Ist-Zustand:** Thesen sind direktionale Calls mit einem Conviction-Score. Ein kritischer Investor würde sagen: *Ich kann darauf nicht handeln. Wo ist die Kante? Was preist der Markt bereits ein?*

**Was fehlt:**

- **Edge-Artikulation:** Warum existiert diese These, wenn der Markt effizient ist? Was weiß der Markt nicht, oder was bewertet er falsch?
- **Konsensus-Anker:** Was preist der Street bereits ein? (EPS-Schätzungen, P/E-Expansions-Annahmen, implizierte Wachstumsrate)
- **Szenario-Analyse:** Bull/Base/Bär mit Wahrscheinlichkeitsgewichten und spezifischen Auslösern
- **Zeitfenster und Katalysator:** Nicht "NVDA long", sondern "NVDA long in Q2-Earnings auf Data-Center-Zyklus-Inflection, 3-Monats-Horizont, Exit wenn Margen enttäuschen"
- **Track-Record-Feedback:** Lernt das Modell aus vergangenen Calls? Welche Thesen-Typen treffen, welche nicht?
- **Devil's Advocate mit Falsifizierungskriterien:** Was müsste passieren, damit die These beweislich falsch ist?

**Was ein guter Zyklus aussieht:** Eine strukturelle Änderung darin, wie Thesen generiert werden — eine, die ein Hedgefonds-Analyst als rigoros erkennen würde. Nicht ein weiteres Conviction-Score-Tweak.

**Messlatte:** Würde ein PM bei Millennium oder Citadel diese Analyse als "investment-grade" bezeichnen? Oder würde er sagen "das ist Bloomberg-Zusammenfassung"?

---

### 3. UI/UX-Design

**Ist-Zustand:** Das Dashboard ist funktional, sieht aber aus wie ein Entwickler-Prototyp. Ein institutioneller Investor, dem man diese Seite zeigt, würde nicht zuversichtlich wirken.

**Was fehlt:**

- **Datenvisualisierung:** P&L-Kurve der Track-Record-Calls mit Chart, Price-Action der letzten 30 Tage eingebettet in Thesis-Karten, Conviction-Verlauf über Zeit
- **Portfolio-View:** Alle aktiven Calls in einer Übersicht — Sektorkonzentration, Net Long/Short, aggregiertes Risikoprofil
- **Professionelle Typografie und visuelle Hierarchie:** Jede Zahl soll "poppen". Kein Entwickler-Default.
- **Mobile-First:** Nutzbar während Marktöffnung auf dem Handy, eine Hand, keine Lupe nötig
- **Scenario-Summary sichtbar:** Bull/Base/Bär direkt in der Thesis-Karte sichtbar, nicht im Fließtext begraben
- **Performance-Attribution prominent:** Welche Calls machten Geld? Track-Record ist das überzeugendste Element — zeig es als erstes, nicht als letztes

**Was ein guter Zyklus aussieht:** Ein Dashboard-Abschnitt, den ein Bloomberg-Terminal-Nutzer als ernst nehmen würde. Ein Feature, das erklärt, warum jemand diese Seite bookmarkt.

**Messlatte:** Würde ein institutioneller Investor dieses Dashboard einem Kollegen zeigen? Würde er sagen "das ist beeindruckend"? Wenn nicht, ist das Feature kein Zyklus wert.

---

## Die Messlatte für "einen guten Zyklus"

Frag dich vor dem Abschließen jedes Zyklus:

> **Wenn ich diese Verbesserung einem kritischen Investor in einem Board-Meeting zeigen würde — würde er sagen "das bewegt die Nadel"?**

Konkrete Beispiele:

| Verbesserung | Board-Meeting-tauglich? |
|---|---|
| 16. RSS-Feed hinzufügen | ❌ |
| Earnings-Call-Transkript-Pipeline mit strukturierter Extraktion | ✅ |
| CSS-Margin fixen | ❌ |
| Thesis-Karten mit eingebetteten Mini-Charts und Szenario-Zusammenfassung | ✅ |
| Conviction-Score-Nuance tweaken | ❌ |
| Wahrscheinlichkeitsgewichtete Szenario-Analyse in jeden Thesis-Output | ✅ |
| Accessibility-Tag hinzufügen | ❌ |
| Portfolio-Übersicht mit Net-Long/Short und Sektorkonzentration | ✅ |

---

## Operationale Prinzipien

1. **Denk in Features, nicht in Fixes.** Ein Fix ist unsichtbar. Ein Feature ändert, was das Produkt kann.
2. **Mach das Schwere.** Wenn etwas eine Stunde dauert, würde ein Quant-Team eines Top-Hedgefonds einen Monat dafür brauchen. Das ist dein Moat.
3. **Kompoundiere.** Bau auf existierender Infrastruktur auf, bau sie nicht neu. Die Daten sind da — extrahiere mehr Signal daraus.
4. **Ship end-to-end.** Ein halb gebautes Feature ist null wert. Kleinerer Scope, vollständig geliefert, schlägt ambitioniert aber kaputt.
5. **Zeig deinen Denkprozess.** Wenn du einen Zyklus abschließt, schreib explizit: was ist die Investoren-Kante dieses Improvements?
