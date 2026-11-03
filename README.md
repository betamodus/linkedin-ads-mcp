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
| `list_campaign_structure` | Kampagnen mit ihren Anzeigengruppen: Status, Ziel, Laufzeit, Budgets, Gebot, Ausgaben seit Start, Budgetauslastung |
| `get_analytics` | Kennzahlen nach Zeitraum und Aufschlüsselung (Konto, Kampagne, Anzeigengruppe, Anzeige, Demografie) mit Namen in Klartext. Berechnet CTR, CPC, CPM, Kosten pro Lead, Kosten pro Conversion, Lead-Formular-Abschlussquote, Frequenz sowie Summen. Optional nur die Top N (`max_rows`). |
| `list_metrics` | Alle verfügbaren Kennzahlen und Einschränkungen |

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
* **Demografie** (`MEMBER_*`): Werte sind von LinkedIn geschätzt, kleine Gruppen fehlen. Namen werden live bei LinkedIn nachgeschlagen; wo LinkedIn keinen Klartext liefert (z. B. einzelne neuere Branchen), bleibt die Kennung stehen und die Antwort sagt das.
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
