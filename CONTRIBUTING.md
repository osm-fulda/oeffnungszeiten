# Mitmachen

Dieses Repository beobachtet Websites von Betrieben auf **geänderte Öffnungszeiten**. Eine Datei je
Watch liegt in [`entries/`](./entries); hinzufügen, ändern oder entfernen ist ein Pull Request. Du
brauchst keinen laufenden Dienst, keinen API-Schlüssel und keine XPath-Kenntnisse.

**Sprache:** Diese Seite, die README, das Issue-Formular und die Nachrichten des Bots sind deutsch,
weil sie liest, wer in Fulda mitmacht. Handwerk und Begründung liegen auf Englisch, damit eine
andere Stadt das hier nachbauen kann: [FILTERS.md](./FILTERS.md), [CONCEPT.md](./CONCEPT.md),
`docs/` und [entries/README.md](./entries/README.md). Code, Docstrings und Commit-Messages sind
englisch. Ein `note` zitiert, was auf der Seite steht ("Termine nur nach Vereinbarung"), und bleibt
deutsch: ein übersetztes Zitat taugt weniger als Beleg.

---

## Zwei Wege hinein

**Ohne Fork:** ein Issue über die Vorlage
[Watch vorschlagen](../../issues/new?template=watch-vorschlagen.yml) mit der Seite, dem Namen des
Betriebs und seiner OSM-Id. Ein Maintainer setzt das Label `wizard`, dann holt ein Bot die Seite und
kommentiert die Filterkandidaten samt dem Text, den jeder einfangen würde. Die Kandidaten sind
durchnummeriert; antworte mit `/pick` und der Nummer des Blocks, der die Zeiten trägt, etwa
`/pick 1`. Dann schreibt er die Entry-Datei, schiebt den Branch und kommentiert einen Link, der den
Pull Request mit ausgefülltem Titel und Text öffnet. Nichts zu installieren, nichts auszuführen. Das Lesen bleibt
bei dir: welcher Block wirklich die Zeiten *dieses* Betriebs trägt, kann kein Werkzeug entscheiden.

Der letzte Klick ist Absicht. GitHub lässt Actions ohne eine eigene Repo-Einstellung keine Pull
Requests anlegen, und ein von einem Menschen geöffneter Pull Request löst CI aus, während einer aus
`GITHUB_TOKEN` überhaupt keinen Workflow startet.

Der Bot holt die Seite ohne Browser. Eine Seite, deren Zeiten erst nach JavaScript erscheinen, sagt
er ab, sie bleibt ein Fall für den langen Weg. Dasselbe gilt für eine Seite, die schon beobachtet
wird: er nennt dann die Datei, die sie abdeckt. Dabei zählt nicht die Zeichenkette, sondern die
Seite — `www.`, `http` gegen `https`, ein Schrägstrich am Ende, ein `?utm_...` und der `#anker`
sind dieselbe Seite. Trägt dein Vorschlag eine `osm_id`, die ein Eintrag schon hat, sagt er auch
das: derselbe Betrieb über eine andere Seite.

Er kennt auch die Sperrliste. Steht die Seite in `no-watch.json`, sagt er ab und zeigt Grund,
Datum und die damalige Notiz, statt einen Vorschlag zu bauen, den CI ohnehin zurückweist. War der
Eintrag zum Wiederansehen fällig, baut er den Watch und nimmt den Sperrlisten-Eintrag im selben
Pull Request heraus: eine Seite gehört in genau eine der beiden Listen.

**Mit dem Repository**, der Weg unten: er gibt dir die volle Ausgabe des Wizards, den
Browser-Rückfall und alles Weitere auf dem eigenen Rechner. Beide Wege enden im selben Pull Request
und in derselben Prüfung.

---

## 1. Die Seite finden, die die Zeiten trägt

Meist die Startseite, `/kontakt` oder `/oeffnungszeiten`. Bei einer Kette fast immer die Seite der
Filiale, nicht die des Konzerns. Prüfe, dass die Zeiten zu *diesem* Betrieb gehören: ein Link im
Fußbereich führt oft zu den Bürozeiten des Vermieters oder zu einer Barrierefreiheitserklärung.

Eine Seite, die nirgends Zeiten nennt, ist kein Watch wert: sie bleibt für immer still und sieht
dabei kerngesund aus ([FILTERS.md](./FILTERS.md) §0 hat die Messung dazu). So eine Seite gehört in
`no-watch.json`, siehe unten.

## 2. Die OSM-Id holen

Den Betrieb auf [openstreetmap.org](https://www.openstreetmap.org) suchen, das Objekt öffnen und die
Id aus der Adresse nehmen: `node/123456789`, manchmal `way/…` oder `relation/…`.

Ein Alarm soll in einer OSM-Änderung enden, deshalb trägt die Nachricht einen Link auf das Objekt,
gebaut aus `osm_id`. Ohne sie kommt der Alarm trotzdem an, mit Seiten-URL und Diff, aber ohne Link,
und wer ihn liest, sucht den Betrieb von Hand in der Karte: also genau die Arbeit, die der Alarm
sparen soll. Fast jeder Eintrag hier hat eine Id. Lass sie nur weg, wenn der Betrieb wirklich noch
nicht in OSM steht, und schreib das in den Pull Request. Der Code folgt der Id nirgends, dieses
Repository fragt OSM selbst nie etwas.

## 3. Den Wizard den Eintrag schreiben lassen

```bash
pip install lxml
python3 scripts/filter_wizard.py https://example.de/kontakt --emit entries \
    --name "Example GmbH" --osm-id node/123456789 --tags fulda-restaurants
```

`--tags` gruppiert den Watch, und der Tag besteht aus zwei Teilen: **dem Gebiet** und **dem
OSM-Wert** der Kategorie, also dem Wert, den das Objekt aus Schritt 2 ohnehin trägt. Hier ist das
Gebiet `fulda`, und damit wird `shop=bakery` zu `fulda-bakery`, `amenity=doctors` zu
`fulda-doctors`, `shop=florist` zu `fulda-florist`.

Beide Teile tragen: der Wert unterscheidet Bäcker von Ärzten, das Gebiet unterscheidet Fulda von
einem zweiten Landkreis, der später dazukommen kann. Deshalb steht es auch dann davor, wenn es
noch nur eines gibt. Den Schlüssel statt des Werts zu nehmen ergibt einen Tag, der auf alles
passt und deshalb nichts gruppiert (`fulda-shop`). Für mehrere Tags die Option wiederholen oder
mit Komma trennen. Der Wizard sagt Bescheid, wenn `--tags` oder `--osm-id` fehlen.

Der Tag ist kein OSM-Tag und wird nirgends dorthin zurückgeschrieben. Er gruppiert im UI, und
`entries_sync.py` schreibt daran die Benachrichtigungs-Einstellungen von einem Geschwister-Watch
ab. Was schon in Gebrauch ist:

```bash
grep -ho '"fulda-[a-z_-]*"' entries/*.json | sort | uniq -c | sort -rn | head
```

Er zeigt die Kandidaten als **den Text, den jeder einfangen würde**. Ausgewählt wird am Lesen: du
weißt, wie die Öffnungszeiten deines Betriebs aussehen, und musst keinen Selektor beurteilen. Nimm
die `!`-Warnungen ernst, besonders `only N weekday(s)` (die halbe Woche steht woanders) und
`brittle selector` (bricht beim nächsten Umbau der Seite).

**Steht im einfachen HTML nichts Brauchbares**, versucht es der Wizard von selbst mit einem Browser
und sagt das auch. Er findet einen auf `localhost:3000` ohne jede Option, einen zu starten ist also
die ganze Einrichtung; changedetection ist daran nicht beteiligt:

```bash
docker run --rm -p 3000:3000 dgtlmoon/sockpuppetbrowser
```

`--browser-ws ws://host:port` nennt einen Browser woanders. Was gerendert wurde, landet im Eintrag
als `fetch_backend: html_webdriver`. Ohne Docker druckt der Wizard diesen Befehl und bleibt beim
einfachen Abruf; du kannst den Pull Request trotzdem öffnen, in der Beschreibung sagen, dass die
Seite gerendert werden muss, und ein Maintainer macht es fertig.

Findet sich auch gerendert nichts, sagt der Wizard das und hört auf.

## 4. Den Eintrag prüfen

```bash
python3 scripts/validate_entries.py                                          # Struktur, wie CI es fährt
python3 scripts/validate_entries.py --live --only entries/example-gmbh.json  # und gegen die Seite
```

Der erste Aufruf ist der, den CI fährt: **nur Struktur**, und das mit Absicht, denn so braucht er
kein Netz und scheitert nie an etwas, das du nicht ändern kannst. Lass ihn über alle Einträge
laufen, so wie CI es tut. `--only` ist der schnelle Blick auf eine Datei und sieht weniger: ein
doppelter Slug und eine Seite, die in `entries/` und in `no-watch.json` steht, fallen erst im
vollen Durchlauf auf.

Der zweite Aufruf holt die Seite, und die Minute lohnt sich, bevor etwas deinen Rechner verlässt. Er
lässt einen Eintrag durchfallen, dessen Filter überhaupt keine Zeit einfängt: also den Watch, der
niemals auslösen könnte. Für eine Seite, die JavaScript braucht, `--browser-ws ws://localhost:3000`
dazunehmen; ohne einen Browser gibt es dafür nur eine Warnung. Live geprüft werden nur
`xpath:`-Filter, CSS und JSON-LD nicht.

Ob der Filter wirklich die Zeiten fängt, beurteilt ein Mensch, am `captured_sample` in deinem Diff.
Deshalb muss es da sein, und deshalb müssen darin echte Öffnungszeiten stehen.

## 5. Den Pull Request öffnen

```bash
gh repo fork --remote            # nur ohne Schreibrecht, und nur einmal
git checkout -b add-example-gmbh
git add entries/example-gmbh.json
git commit -m "add Example GmbH"
git push origin add-example-gmbh
gh pr create --fill
```

Niemals direkt auf `main` committen. Ohne die `gh`-CLI auf github.com forken, den Branch in den Fork
schieben und den Pull Request dort öffnen. Nach dem Merge legt der stündliche Sync den Watch an.

---

## Die Entry-Datei

Der Wizard schreibt sie vollständig. Von Hand sähe sie so aus:

```json
{
  "schema": 1,
  "name": "Example GmbH",
  "url": "https://example.de/kontakt",
  "filter": "xpath://div[contains(@class,\"opening-hours\")]",
  "captured_sample": "Mo–Fr 09:00–18:00 · Sa 09:00–13:00",
  "osm_id": "node/123456789",
  "tags": ["fulda-restaurants"],
  "lang": "de",
  "added": "2026-08-05"
}
```

| Feld | |
|---|---|
| `schema`, `name`, `url` | Pflicht |
| `filter` | CSS, `xpath:…` oder `json:…`. Nur weglassen, wenn wirklich die ganze Seite gemeint ist |
| `captured_sample` | Pflicht: daran beurteilt ein Prüfender den Eintrag, ohne selbst etwas abzurufen |
| `osm_id` | `node/…`, `way/…` oder `relation/…`. CI erzwingt es nicht, siehe aber Schritt 2 |
| `tags` | `fulda-` plus OSM-Wert, Namen statt uuids: eine uuid bedeutet in einer anderen Instanz nichts |
| `lang` | `de` (Vorgabe) oder `en`, steuert die Wochentagserkennung |
| `fetch_backend` | `html_webdriver`, wenn die Zeiten JavaScript brauchen, setzt der Wizard |
| `sort_text_alphabetically` | `true`, wenn der Block täglich umsortiert, setzt der Wizard |
| `note` | deutsch, freier Text: was die Seite zeigt und was geprüft wurde. Wissen, keine Zierde |
| `added` | das Datum, an dem der Eintrag entstand, setzt der Wizard |

Der Dateiname entsteht aus Name und URL, damit zwei Filialen einer Kette nicht kollidieren.

## Einen Watch ändern oder entfernen

- **Ändern**: die Datei bearbeiten. Der nächste Sync schreibt sie durch.
- **Entfernen**: die Datei mit `git rm` löschen und die Seite in `no-watch.json` eintragen, im
  selben Pull Request — eine Seite gehört in genau eine der beiden Listen. Der Watch ist binnen
  einer Stunde weg. Viele auf einmal zu entfernen ist das eine, was in den Pull Request gehört:
  der Sync weigert sich, mehr als eine Handvoll in einem Lauf zu löschen, weil ein leer
  angekommener Checkout genauso aussieht wie die Bitte, alles zu löschen.

## Eine Seite ohne Zeiten: `no-watch.json`

Das Gegenstück zu `entries/`. Eine angesehene Seite, an der es nichts zu beobachten gibt, wird dort
mit Begründung festgehalten, damit niemand ein zweites Mal einen Abend darauf verwendet. Das ist ein
Beitrag wie jeder andere, und CI ist dabei strenger als bei einem Eintrag: `reason` muss einer der
gelisteten Gründe sein, `note` muss sagen, was die Seite *stattdessen* zeigt und wie das geprüft
wurde (deutsch, mindestens 30 Zeichen), und `recheck` ist ein Datum, `on-relocation` oder `never`.
Aufbau und Gründe stehen in [CONCEPT.md](./CONCEPT.md); `python3 scripts/no_watch.py` zeigt, was die
Liste enthält. Eine Seite gehört in genau eine der beiden Listen, CI achtet darauf.

**Ohne Fork** geht auch das: die Vorlage
[Watch entfernen](../../issues/new?template=watch-entfernen.yml) fragt die Seite, einen Grund aus
einer Liste und einen Satz darüber, was dort statt der Zeiten steht. Das Formular hängt `watch-entfernen`
an, ein Maintainer setzt `watch-weg` — erst das ist der Startschuss, wie überall hier: ein Label,
das ein Formular selbst vergibt, wäre kein Tor. Dann löscht der Bot den Eintrag, schreibt den Sperrlisten-Record und baut den Pull
Request. Name, OSM-Objekt und die zuletzt erfassten Zeiten nimmt er aus der vorhandenen Datei; die
Zeiten wandern in die Notiz, weil das Löschen sonst der Moment wäre, in dem sie verloren gehen. Die
Seite ruft er **nicht** ab: was auf ihr steht, hast du gelesen, und der Pull Request ist die Stelle,
an der das jemand nachliest. Liegen auf einer Seite zwei Watches, sagt er ab — dann trägt sie Zeiten
für zwei Betriebe, und welcher wegfällt, ist keine Frage für ein Formular.

## Was zurückkommt

CI lässt einen Pull Request durchfallen bei:

- kaputtem JSON, fehlendem `url`/`name`, nicht unterstütztem `schema`
- einer `url`, die nicht `http(s)` ist oder keinen Host hat
- einem doppelten Slug (der Dateiname ist die Identität)
- einem **absoluten XPath** wie `/html/body/div[2]/div/main/…`
- fehlendem `captured_sample`: im Diff wäre nicht zu sehen, was der Filter fängt
- einem `fetch_backend` außer `system`, `html_requests` oder `html_webdriver`
- derselben Seite in `entries/` und in `no-watch.json`, oder einem Eintrag in `no-watch.json`, dem
  ein Feld fehlt

Ein Prüfender schickt zurück, ohne dass CI fehlschlägt:

- einen Filter, der an einer generierten Klasse hängt (`elementor-element-224ed87`)
- mehrere Einträge, die auf einen Filialfinder zeigen. Stattdessen auf die einzelnen Filialseiten
  aufteilen
- eine Seite, die keine Zeiten nennt (Schritt 1), und übergangene `!`-Warnungen des Wizards

Was einen Selektor brüchig macht und woran man stattdessen verankert: [FILTERS.md](./FILTERS.md).

## Hintergrund

- [FILTERS.md](./FILTERS.md): die Seitenformen, die immer wiederkommen, die vier Kriterien, die
  einen Filter belegen, und die Fallen, die echte Zeit gekostet haben (englisch)
- [CONCEPT.md](./CONCEPT.md): warum die Einträge die Quelle sind und was `no-watch.json` festhält
  (englisch)
- [docs/changedetection.md](./docs/changedetection.md): wie der Dienst ausgerollt ist (englisch)

## Lizenz der Beiträge

Mit einem Pull Request stimmst du zu, dass dein **Code** unter [GPL-3.0](./LICENSE) und deine
**Eintragsdaten** unter [ODbL 1.0](https://opendatacommons.org/licenses/odbl/1-0/) beigetragen
werden, passend zu OpenStreetMap, woher der größte Teil dieses Datenbestands stammt.

Zwei Lizenzen, weil eine Codelizenz keine Datenbank lizenzieren kann. Die Trennlinie liegt bei
`entries/`: die Einträge sind die Datenbank, alles andere, `scripts/` und die Dokumentation, ist
Code unter GPL-3.0. Was das im Alltag heißt:

- **Namensnennung**: Wer diese Daten zeigt, nennt "© OpenStreetMap contributors", denn Name,
  `website`-Tag und `osm_id` der Einträge stammen aus OSM.
- **Share-alike**: Wer eine veränderte Fassung dieses Bestands veröffentlicht, veröffentlicht sie
  wieder unter ODbL.
- **Keine personenbezogenen Daten**: Ein Eintrag nennt den Betrieb, nicht die Personen darin. Eine
  Praxis steht mit ihrem Namen da, nicht mit dem der Behandelnden; brauchte ein Arztname, um die
  richtige Filialseite zu finden, gehört er in die Commit-Message, nicht in den Eintrag.

Dieselbe Regel auf Englisch, mit der Herkunft im Detail: [entries/README.md](./entries/README.md).
