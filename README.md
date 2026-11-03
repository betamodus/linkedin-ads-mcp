# LinkedIn-Ads-MCP

Internes Werkzeug der Betamodus GmbH. Kein Support, keine Gewähr.

MCP-Server, über den Claude LinkedIn-Werbedaten lesen kann: Werbekonten, Kampagnen, Anzeigengruppen, Anzeigen und Kennzahlen. Jede Person meldet sich mit ihrem eigenen LinkedIn-Konto an und sieht genau die Werbekonten, auf denen sie dort eine Rolle hat. Nur lesend, Kampagnen werden nicht verändert.

## Begriffe

Der Server verwendet die Begriffe des Campaign Managers. Die API nennt die Ebenen anders:

| Campaign Manager | LinkedIn-API | Aufschlüsselung in `get_analytics` |
|---|---|---|
| Kampagne | campaign group | `CAMPAIGN_GROUP` |
| Anzeigengruppe | campaign | `CAMPAIGN` |
| Anzeige | creative | `CREATIVE` |

## Werkzeuge

| Werkzeug | Zweck |
|---|---|
| `login` | LinkedIn-Anmeldung im Browser (einmalig, danach automatische Erneuerung) |
| `auth_status` | Zeigt, ob und wie lange die Anmeldung gültig ist |
| `list_ad_accounts` | Werbekonten mit Name, Währung, Status, eigener Rolle |
| `list_campaign_structure` | Kampagnen mit ihren Anzeigengruppen: Status, Ziel, Laufzeit, Budgets, Gebot, Ausgaben seit Start, Auslastung des Gesamtbudgets |
| `get_analytics` | Kennzahlen nach Zeitraum und Aufschlüsselung (Konto, Kampagne, Anzeigengruppe, Anzeige, Demografie). Jede Zeile trägt ihren vollständigen Pfad Werbekonto > Kampagne > Anzeigengruppe > Anzeige. Berechnete Kennzahlen (CTR, Engagement-Rate, CPC, CPM, Kosten pro Lead/Conversion, Lead-Formular-Abschlussquote, Frequenz, Video-Quoten) und Summen. Optional nur die Top N Einträge (`max_rows`), bei Zeitreihen mit allen Zeiträumen. |
| `list_metrics` | Alle verfügbaren Kennzahlen, Formeln der berechneten Kennzahlen und Einschränkungen |

## Grundsätze im Code

1. Jede Zeile ist für sich verständlich (vollständiger Pfad).
2. Berechnet wird nur, was zwingend aus den gelieferten Daten folgt. Alle Formeln stehen in einer Tabelle (`DERIVED` in `server.py`); eine neue Kennzahl ist eine Zeile.
3. Nichts verschwindet stillschweigend: Angefragte, aber nicht gelieferte Kennzahlen und gekürzte Antworten werden gemeldet.
4. Alle Werkzeuge antworten über denselben Ausgabeweg mit derselben Größenbremse.
5. Fachwissen über LinkedIn (Attribution, Reichweite, Demografie) steht in den Server-Anweisungen (`INSTRUCTIONS`), nicht als Sonderregel im Code.

## Einrichtung in Claude Desktop (macOS)

**1. uv installieren** (einmalig, im Terminal)

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Danach `which uvx` ausführen und den angezeigten Pfad notieren, z. B. `/Users/NAME/.local/bin/uvx`.

**2. Konfiguration eintragen**
Claude Desktop → Einstellungen → Entwickler → „Konfiguration bearbeiten“. Folgenden Block einfügen und Pfad sowie Zugangsdaten ersetzen:

```json
{
  "mcpServers": {
    "linkedin-ads": {
      "command": "/Users/NAME/.local/bin/uvx",
      "args": [
        "--refresh",
        "--from", "git+https://github.com/betamodus/linkedin-ads-mcp",
        "linkedin-ads-mcp"
      ],
      "env": {
        "LINKEDIN_CLIENT_ID": "…",
        "LINKEDIN_CLIENT_SECRET": "…"
      }
    }
  }
}
```

Client ID und Secret gibt es intern, sie gehören nie in dieses Repository.

**3. Claude Desktop komplett beenden und neu starten.**

**4. Anmelden**
Im Chat schreiben: „Melde mich bei LinkedIn an.“ Der Browser öffnet sich, mit dem eigenen LinkedIn-Konto anmelden und bestätigen. Das Token liegt danach im macOS-Schlüsselbund.

**5. Testen**
„Welche LinkedIn-Werbekonten kann ich sehen?“

## Einrichtung in Claude Code

```bash
claude mcp add linkedin-ads \
  -e LINKEDIN_CLIENT_ID=… -e LINKEDIN_CLIENT_SECRET=… \
  -- uvx --refresh --from git+https://github.com/betamodus/linkedin-ads-mcp linkedin-ads-mcp
```

## Beispielfragen

> Wie haben sich CTR, CPC und Kosten pro Lead der Kampagnen von [Kunde] im Oktober gegenüber September entwickelt?

> Welche Anzeigen von [Kunde] hatten im letzten Monat die beste Engagement-Rate, welche sollten pausiert werden?

> Welche Jobfunktionen, Branchen und Unternehmensgrößen konvertieren bei [Kunde] am günstigsten?

> Wie hoch ist die Frequenz der Anzeigengruppen von [Kunde], gibt es Anzeichen für Ad Fatigue?

> Erstelle einen Monatsreport für [Kunde] mit Budgetauslastung und Lead-Formular-Abschlüssen.

## Wichtig

* **Nur `r_ads` und `r_ads_reporting`.** Kommt später ein Recht dazu (z. B. `rw_ads`), müssen sich alle neu anmelden.
* **Eigene LinkedIn-App.** Nie mit den Zugangsdaten einer anderen App betreiben. Eine Anmeldung mit zusätzlichen Rechten entwertet bei LinkedIn bestehende Tokens derselben App.
* **Rücksprung-Adresse** muss in der LinkedIn-App exakt `http://localhost:8765/callback` lauten.
* **API-Version** steht in `api.py` (`202610`) und lässt sich per Umgebungsvariable `LINKEDIN_API_VERSION` überschreiben. LinkedIn unterstützt jede Version mindestens ein Jahr.
* **Demografie** (`MEMBER_*`): Werte sind von LinkedIn geschätzt. Die Zeilen überschneiden sich oder lassen Teile weg, ihre Summe ist kein Gesamtwert (im Test bei Branchen 255 %, bei Jobtiteln 59 % der echten Kosten). Die Antwort enthält deshalb die echten Gesamtwerte (`gesamt_laut_konten`) und den erfassten Anteil (`abdeckung_prozent`). Namen werden live bei LinkedIn nachgeschlagen, nur für die Einträge, die in der Antwort landen; wo LinkedIn keinen Klartext liefert, bleibt die Kennung stehen und die Antwort sagt das.
* **Einheiten:** `averageVideoWatchTime` in Millisekunden, `audiencePenetration` als Anteil von 0 bis 1, `averageDwellTime` in Sekunden (geprüft gegen den Campaign Manager).
* **Video-Quoten** (Aufrufrate, Abschlussquote) wie im Campaign Manager: bezogen auf die Impressionen der Videoanzeigen, deshalb nur bei Aufschlüsselung nach Anzeigengruppe oder Anzeige.
* **Gebot:** Bei automatischer Gebotsstrategie (`MAX_*`) zeigt `list_campaign_structure` kein Gebot, nur die Strategie.
* **Anzeigen ohne Namen** werden mit ID und Anzeigengruppe bezeichnet. Den Post-Text liefert LinkedIn nur mit Rechten auf der Unternehmensseite.
* **Größenbremse:** Antworten über 35.000 Zeichen werden gekürzt (Zeilen mit den höchsten Kosten bleiben, Summen enthalten immer alles). Einstellbar per Umgebungsvariable `LINKEDIN_MCP_MAX_CHARS`.
* **Geldbeträge** werden auf 2 Nachkommastellen gerundet angezeigt, gerechnet wird mit den genauen Werten.
* **Leere Antwort heißt nicht null.** Hat die Person auf einem Konto keine Rolle, liefert LinkedIn keine Daten und keinen Fehler.

## Befehle im Terminal

```bash
uvx --from git+https://github.com/betamodus/linkedin-ads-mcp linkedin-ads-mcp login    # anmelden
uvx --from git+https://github.com/betamodus/linkedin-ads-mcp linkedin-ads-mcp status   # Anmeldung prüfen
uvx --from git+https://github.com/betamodus/linkedin-ads-mcp linkedin-ads-mcp logout   # Token löschen
```

Für `login` müssen `LINKEDIN_CLIENT_ID` und `LINKEDIN_CLIENT_SECRET` als Umgebungsvariablen gesetzt sein.
