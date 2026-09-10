# Öffnungszeiten

Welche Websites von Betrieben in Fulda auf **geänderte Öffnungszeiten** geprüft werden, damit ein
Mapper davon erfährt und OpenStreetMap nachziehen kann. Eine Datei je Watch, geändert wird über
Pull Requests.

Das Beobachten selbst erledigt eine [changedetection.io](https://changedetection.io)-Instanz, die
Flux aus diesem Repository ausrollt, siehe [docs/changedetection.md](./docs/changedetection.md).

Jeder Watch wird alle drei Tage geprüft.

> **English.** This repository watches the websites of businesses in Fulda for **changed opening
> hours**, so a mapper hears about it and can update OpenStreetMap. One file per watch in
> `entries/`, added and changed through pull requests; a changedetection.io instance deployed by
> Flux does the watching. Everything a contributor here reads is German, because they read German
> opening hours anyway. The craft and the reasoning are English, so another city can rebuild this:
> [FILTERS.md](./FILTERS.md) for how to find the block that carries the hours and how to know the
> filter is right, [CONCEPT.md](./CONCEPT.md) for why it exists and why watches are files in git,
> [docs/changedetection.md](./docs/changedetection.md) and
> [docs/notifications.md](./docs/notifications.md) for the deployment and the way a change reaches
> Matrix, [entries/README.md](./entries/README.md) for what an entry holds and under which licence.
> Code, docstrings and commit messages are English too.

## Einen Watch hinzufügen

**Der kurze Weg, ohne Installation und ohne Fork:** ein Issue über die Vorlage
[Watch vorschlagen](../../issues/new?template=watch-vorschlagen.yml) mit der Seite, dem Namen des
Betriebs und seiner OSM-Id. Ein Maintainer setzt das Label `wizard`, dann holt ein Bot die Seite und
kommentiert die Filterkandidaten, jeden davon als **den Text, den er einfangen würde**. Diese sind
durchnummeriert; antworte mit `/pick` und der Nummer des Blocks, der die Zeiten trägt, etwa
`/pick 1`. Dann schreibt er die Entry-Datei, schiebt den Branch und gibt einen Link zurück, der den
Pull Request mit einem Klick öffnet.

Das eine, was der Bot nicht abnimmt, ist das Lesen: welcher dieser Blöcke die Zeiten *dieses*
Betriebs trägt und nicht die eines Terminformulars, einer Nachbarfiliale oder einer mitlaufenden
Uhr. Das ist die eigentliche Entscheidung, und sie bleibt beim Menschen.

**Der lange Weg, auf dem eigenen Rechner**, wenn du die volle Ausgabe des Wizards willst, den
Browser-Rückfall für Seiten, die ihre Zeiten erst per JavaScript zeigen, oder gleich mehrere
Einträge auf einmal:

```bash
pip install lxml
python3 scripts/filter_wizard.py https://example.de/kontakt \
    --emit entries --name "Example GmbH" --osm-id node/123456789 --tags fulda-restaurants
```

`--emit` schreibt die fertige Entry-Datei, du committest sie und öffnest einen Pull Request.

Beide Wege enden im selben Pull Request und in derselben Prüfung. Einen Watch zu ändern oder zu
entfernen ist eine Änderung an seiner Datei, und eine Seite, die kein Watch verdient, gehört mit
Begründung in `no-watch.json`. Alles dazu: **[CONTRIBUTING.md](./CONTRIBUTING.md)**.

## Wozu überhaupt ein Filter

changedetection schlägt bei *jeder* Textänderung an. Ungefiltert meldet sich ein Watch bei einem
rotierenden Teaser, einem Cookie-Banner oder einem Besucherzähler, und der eine Alarm, auf den es
ankommt, geht darin unter. Deshalb trägt jeder Eintrag einen Selektor, der die Seite auf ihren
Öffnungszeiten-Block eingrenzt. Einen zu finden, und einen guten von einem plausiblen zu
unterscheiden, ist das Handwerk dieses Projekts: **[FILTERS.md](./FILTERS.md)**.

## Aufbau

```
entries/            eine Datei je Watch, die Quelle der Wahrheit
  .lock.json        slug -> Watch-uuid, je changedetection-Instanz
no-watch.json       die Sperrliste: angesehene Seiten, die kein Watch wert sind, mit Begründung
scripts/            Wizard, Prescreen, Sync, Audit, Renderer  (stdlib, außer lxml)
deploy/             gepflegte globale Einstellungen: Rauschfilter, Prüfintervall
charts/             Helm-Chart für changedetection und seinen Browser
apps/               Flux-HelmRelease und die Werte für dieses Cluster
clusters/           Flux-Einstiegspunkt, was das Cluster abgleicht
```

## Die Skripte

| | |
|---|---|
| `filter_wizard.py` | schlägt Filter für eine Seite vor, ausgewählt wird am eingefangenen Text |
| `wizard_bot.py` | derselbe Wizard, aus einem GitHub-Issue gefahren, damit niemand forken muss |
| `entries_sync.py` | gleicht eine changedetection-Instanz gegen `entries/` ab |
| `validate_entries.py` | was CI bei jedem Pull Request prüft: Struktur, und der Filter gegen die Seite |
| `watch_audit.py` | was jeder Watch wirklich eingefangen hat: RED / AMBER / green mit Begründung |
| `rotation_check.py` | haben sich die Zeiten geändert oder nur die Reihenfolge? deutet einen Alarm aus den Snapshots |
| `cdp_render.py` | rendert eine Seite durch einen Headless-Browser, ohne changedetection |
| `cd_export.py` | macht aus einem Versuch in der UI wieder Entry-Dateien |
| `apply_global_settings.py` | schreibt `deploy/global-settings.json` in eine Instanz |
| `matrix_relay_seed.py` | erzeugt die Matrix-Sitzung, auf der das Notification-Relay läuft |
| `no_watch.py` | die Sperrliste: welche Seiten bewusst nicht beobachtet werden und was ansteht |
| `prescreen.py` | nennt diese Seite überhaupt Zeiten, die Frage vor jedem Filter |
| `audit_report.py` | der Wochenbericht: was das Audit fand, gepostet ins Benachrichtigungszimmer |

Jedes kennt `--help`. Keines schreibt ohne `--apply` nach changedetection.

`hours_lang.py` und `osm_cd_common.py` sind Bibliotheken, keine Befehle: Zeitenerkennung, die auf
deutschen Seiten trägt, und der changedetection-API-Client.

### Was *nicht* beobachtet wird

[`no-watch.json`](./no-watch.json) ist das Gegenstück zu `entries/`: Seiten, die angesehen wurden
und nichts Beobachtbares hergeben, jede mit Begründung und einem Datum, wann man wieder hinsieht.
Beide Listen sind über die Seite geführt, und CI schlägt fehl, wenn eine Seite in beiden steht.
Warum die Begründungen so geschnitten sind: [CONCEPT.md](./CONCEPT.md).

```bash
python3 scripts/no_watch.py                    # Zusammenfassung je Grund
python3 scripts/no_watch.py --faellig          # heute wieder anzusehen
python3 scripts/no_watch.py --standortwechsel  # was ein Umzug wieder ins Spiel bringt
```

### Welche Seiten Watches werden

Ein Watch lohnt nur, wenn seine Seite überhaupt Zeiten nennt. `prescreen.py` beantwortet das, bevor
jemand einen Filter baut, und sortiert Kandidaten in geblockt, Lieferplattform, nicht erreichbar,
gedrosselt, ohne Zeiten und lohnend. Nur die letzte Gruppe braucht einen Menschen.

```bash
python3 scripts/prescreen.py --csv kandidaten.csv --anzahl 10
```

Die CSV braucht vier Spalten: `osm_id`, `name`, `kategorie`, `website`. Woher diese Liste kommt,
liegt außerhalb dieses Repositories, und das mit Absicht: Objekte in OpenStreetMap zu finden und
Tags in die Karte zurückzuschreiben ist eine andere Aufgabe mit anderen Folgen im Fehlerfall. Hier
wird eine URL genommen und beobachtet.

## Dokumentation

| | |
|---|---|
| [CONTRIBUTING.md](./CONTRIBUTING.md) | einen Watch hinzufügen, ändern oder entfernen |
| [FILTERS.md](./FILTERS.md) | wie man einen Zeitenblock findet und weiß, dass der Filter stimmt (englisch) |
| [CONCEPT.md](./CONCEPT.md) | warum es das gibt und warum Watches Dateien in git sind (englisch) |
| [docs/changedetection.md](./docs/changedetection.md) | wie es ausgerollt ist und die drei Entscheidungen dahinter (englisch) |
| [docs/notifications.md](./docs/notifications.md) | wie eine Änderung ins Matrix-Zimmer kommt und wie die Sitzung entsteht (englisch) |
| [entries/README.md](./entries/README.md) | was ein Eintrag enthält und unter welcher Lizenz (englisch) |

Sechs Dokumente, jedes beantwortet eine Frage. Diese Seite ist die Karte, nichts wird hier zweimal
erklärt.

**Sprache:** Deutsch, was zum Mitmachen nötig ist, Englisch, was Handwerk und Begründung trägt,
in der Tabelle darüber markiert. Welcher Text zu welcher Hälfte gehört und warum:
[CONTRIBUTING.md](./CONTRIBUTING.md).

## Lizenz

GPL-3.0-or-later, siehe [LICENSE](./LICENSE). Öffnungszeiten von Betriebswebsites sind Tatsachen,
keine Werke. Wer sie nach OSM überträgt, hält Quelle und Datum fest (`source:opening_hours`,
`check_date:opening_hours`) und kopiert nie aus einem Kartendienst, dessen Lizenz das verbietet.
