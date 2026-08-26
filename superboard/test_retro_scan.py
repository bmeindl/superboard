"""Tests für retro_scan.py — den Kandidaten-Finder der Fehler-Retrospektive.

Drei Eigenschaften, die den Scanner brauchbar machen. Die ersten beiden fielen am 06.08.
im zweiten Retro-Lauf auf, die dritte beim Archiv-Audit am 13.08.:

1. **Gedächtnis.** Ohne es serviert jeder Folgelauf dieselben Fäden — 5 von 6
   Kandidaten des zweiten Laufs waren bereits im ersten abgehandelt. Ein Eintrag
   heißt „Faden X ist bis Zeitpunkt T geprüft"; was danach an X passiert, muss
   wieder auftauchen, alles davor nicht.
2. **Zeitstempel aus dem Sidecar.** Ein board-Fund trug sonst das Datum der letzten
   Item-Aktivität. An einem Faden, der über zwei Wochen lief, zeigte das einen Fund
   vom 15.07. als 27.07. — und der Prüfer las den falschen Tag nach.
3. **Archivdatum.** Sweep-Archivzeilen tragen nach dem Datum noch ihre Herkunft. Wird das
   Datum dadurch verloren, zieht `--archiv --days 10` den gesamten Altbestand herein.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import retro_scan  # noqa: E402


def _fund(**kw) -> retro_scan.Fund:
    basis = dict(signal="korrektur", gc_id="abc123", titel="t", datum="2026-08-01",
                 score=2, beleg="b", quelle="board.md")
    basis.update(kw)
    return retro_scan.Fund(**basis)


def test_zeit_aus_sidecar_pfad():
    d, z = retro_scan.zeit_aus_pfad("inbox/gc-threads/5c93246e2d74-20260715-105639-1af4.md")
    assert d == "2026-07-15"
    assert z == "2026-07-15T10:56:39"


def test_ohne_sidecar_kein_zeitstempel():
    assert retro_scan.zeit_aus_pfad("board.md") == ("", "")


def test_zeitpunkt_ohne_uhrzeit_gilt_als_tagesende():
    # Im Zweifel NACH dem letzten Retro-Lauf — lieber einmal zu viel zeigen,
    # als einen echten Fund still zu verschlucken.
    assert _fund().zeitpunkt() == "2026-08-01T23:59:59"
    assert _fund(zeit="2026-08-01T09:00:00").zeitpunkt() == "2026-08-01T09:00:00"


def test_transkript_faellt_auf_die_session_zurueck(tmp_path, monkeypatch):
    strom = tmp_path / "run.cap.jsonl"
    strom.write_text("{}", encoding="utf-8")
    assert retro_scan.transkript(str(strom), "sess-1") == str(strom)

    heim = tmp_path / "home"
    slug = str(retro_scan.ROOT).replace("/", "-").replace(".", "-")
    sess = heim / ".claude" / "projects" / slug / "sess-1.jsonl"
    sess.parent.mkdir(parents=True)
    sess.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(retro_scan.Path, "home", classmethod(lambda cls: heim))
    assert str(sess) in retro_scan.transkript(str(tmp_path / "weg.jsonl"), "sess-1")
    assert "nicht mehr da" in retro_scan.transkript(str(tmp_path / "weg.jsonl"), "")


def test_transkript_codex_faellt_auf_rollout_zurueck(tmp_path):
    """Phase 7 (Codex-Runner): Killed-Eintraege von Codex-Runs (model `codex*`) haben ihr
    dauerhaftes Transkript nicht unter ~/.claude/projects/, sondern als Rollout unter
    ~/.codex/sessions/YYYY/MM/DD/ — ohne den Zweig verliert die Retro jeden Codex-Beleg."""
    tag = tmp_path / "2026" / "08" / "12"
    tag.mkdir(parents=True)
    rollout = tag / "rollout-2026-08-12T10-00-00-thread0815.jsonl"
    rollout.write_text("{}", encoding="utf-8")
    hit = retro_scan.transkript("", "thread0815", "codex-xhigh", codex_sessions=tmp_path)
    assert str(rollout) in hit and "Codex-Rollout" in hit
    # Rollout schon weg → ehrliches „nicht mehr da", NICHT der Claude-Projektpfad.
    leer = retro_scan.transkript("", "thread0815", "codex", codex_sessions=tmp_path / "gibtsnicht")
    assert "nicht mehr da" in leer


def test_blatt_antwort_ist_keine_korrektur():
    # „# Fehler-Retrospektive — Entscheidungen (2026-08-06)" wurde als schaerfstes
    # Korrektur-Signal gemeldet, nur weil `\bfehler\b` im Titel steht.
    blatt = "# Fehler-Retrospektive — Entscheidungen (2026-08-06) Q1: B · Q2: C"
    assert retro_scan.RE_STARK.search(blatt)          # der alte Grund fuer den Alarm
    assert retro_scan.RE_BLATT.match(blatt)           # …wird jetzt vorher abgefangen
    assert not retro_scan.RE_BLATT.match("nein, das ist falsch — Entscheidungen sind offen")


def test_korrektur_nach_blatt_antwort_wird_uebersprungen(tmp_path):
    board = tmp_path / "board.md"
    board.write_text(
        "# Themen\n"
        "- [ ] Testitem *(2026-08-06)*\n"
        "  @gc-id: abc123\n"
        "  @gc-re: erledigt, hier das Blatt\n"
        "  @gc: # Fehler-Retrospektive — Entscheidungen (2026-08-06) Q1: B\n"
        "  @gc-re: eingebaut\n"
        "  @gc: nein, das ist falsch\n",
        encoding="utf-8",
    )
    items = retro_scan.parse_board(board)
    funde = [f for f in retro_scan.scan_board(items, __import__("datetime").date(2026, 1, 1))
             if f.signal == "korrektur"]
    assert [f.beleg for f in funde] == ["nein, das ist falsch"]


def test_geprueft_ledger_liest_den_spaetesten_stand(tmp_path, monkeypatch):
    p = tmp_path / "geprueft.jsonl"
    p.write_text(
        '{"gc_id": "abc123", "bis": "2026-08-01T10:00:00"}\n'
        "\n"
        "kein json\n"
        '{"gc_id": "abc123", "bis": "2026-08-06T12:00:00"}\n'
        '{"gc_id": "zzz999", "bis": "2026-07-01T08:00:00"}\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(retro_scan, "GEPRUEFT", p)
    bis = retro_scan.lade_geprueft()
    assert bis == {"abc123": "2026-08-06T12:00:00", "zzz999": "2026-07-01T08:00:00"}


def test_merke_und_filter_zusammen(tmp_path, monkeypatch):
    p = tmp_path / "geprueft.jsonl"
    monkeypatch.setattr(retro_scan, "GEPRUEFT", p)
    retro_scan.merke(["abc123"], "2026-08-06T12:00:00")
    bis = retro_scan.lade_geprueft()

    alt = _fund(zeit="2026-08-06T09:00:00")      # vor dem Lauf: erledigt
    neu = _fund(zeit="2026-08-06T15:00:00")      # danach: muss wieder auftauchen
    fremd = _fund(gc_id="anderer", zeit="2026-08-06T09:00:00")

    def sichtbar(f: retro_scan.Fund) -> bool:
        return not (f.gc_id in bis and f.zeitpunkt() <= bis[f.gc_id])

    assert not sichtbar(alt)
    assert sichtbar(neu)
    assert sichtbar(fremd)


def test_schleife_nutzt_gc_last_fuer_checkpoint_am_selben_tag(tmp_path):
    """Ein unveränderter Schleifen-Fund darf nach einem Vormittags-Checkpoint nicht
    wegen des pauschalen Tagesendes erneut in der Kandidatenliste stehen."""
    board = tmp_path / "board.md"
    board.write_text(
        "# Themen\n"
        "- [ ] Langläufer *(2026-08-14)*\n"
        "  @gc-id: abc123\n"
        "  @gc-re: eins\n"
        "  @gc: nein, das ist falsch\n"
        "  @gc-re: zwei\n"
        "  @gc-re: drei\n"
        "  @gc-re: vier\n"
        "  @gc-last: opus · ~12k · 2026-08-14 09:45 · $1.23\n",
        encoding="utf-8",
    )
    fund = next(
        f for f in retro_scan.scan_board(retro_scan.parse_board(board), date(2026, 8, 4))
        if f.signal == "schleife"
    )
    assert fund.datum == "2026-08-14"
    assert fund.zeitpunkt() == "2026-08-14T09:45:00"
    assert fund.zeitpunkt() <= "2026-08-14T10:23:00"


def test_schleife_nutzt_letztes_antwort_sidecar_ohne_gc_last(tmp_path):
    """Personen-/Sondersektionen haben teils kein `@gc-last`; ihr Antwort-Sidecar
    verhindert trotzdem, dass ein unveränderter Faden am selben Tag zurückkehrt."""
    board = tmp_path / "board.md"
    board.write_text(
        "# Personen\n"
        "- [ ] Langläufer *(2026-08-14)*\n"
        "  @gc-id: abc123\n"
        "  @gc-re: eins\n"
        "  @gc: nein, das ist falsch\n"
        "  @gc-re: zwei\n"
        "  @gc-re: drei\n"
        "  @gc-re: vier … → volle Antwort: "
        "inbox/gc-threads/abc123-20260814-094400-abcd.md\n",
        encoding="utf-8",
    )
    fund = next(
        f for f in retro_scan.scan_board(retro_scan.parse_board(board), date(2026, 8, 4))
        if f.signal == "schleife"
    )
    assert fund.datum == "2026-08-14"
    assert fund.zeitpunkt() == "2026-08-14T09:44:00"
    assert fund.zeitpunkt() <= "2026-08-14T10:23:00"


def test_schleife_zaehlt_nur_antworten_im_fenster_und_keine_crashes(tmp_path):
    """Ein Langläufer mit vielen alten Antworten ist keine aktuelle Schleife."""
    board = tmp_path / "board.md"
    board.write_text(
        "# Themen\n"
        "- [ ] Langläufer *(2026-08-14)*\n"
        "  @gc-id: abc123\n"
        "  @gc-re: alt 1 … → volle Antwort: inbox/gc-threads/abc123-20260720-090000-a001.md\n"
        "  @gc-re: alt 2 … → volle Antwort: inbox/gc-threads/abc123-20260721-090000-a002.md\n"
        "  @gc-re: alt 3 … → volle Antwort: inbox/gc-threads/abc123-20260722-090000-a003.md\n"
        "  @gc-re: alt 4 … → volle Antwort: inbox/gc-threads/abc123-20260723-090000-a004.md\n"
        "  @gc-re: ❌ Agent-Run fehlgeschlagen: exit -9\n"
        "  @gc-re: neu … → volle Antwort: inbox/gc-threads/abc123-20260814-094400-a005.md\n",
        encoding="utf-8",
    )
    funde = retro_scan.scan_board(retro_scan.parse_board(board), date(2026, 8, 4))
    assert [f for f in funde if f.signal == "schleife"] == []


def test_schleife_braucht_korrektur_signal(tmp_path):
    """Reines 1:1-Ping-Pong (jede Antwort auf einen distinkten Auftrag) ist vom Owner
    getriebene Iteration, keine Reibung — alle 5 Schleifen-Kandidaten der Retros vom
    19./20.08. waren Fehlalarme dieser Form. `schleife` feuert nur noch, wenn derselbe
    Faden auch Korrektur-Wortschatz trägt."""
    board = tmp_path / "board.md"
    board.write_text(
        "# Themen\n"
        "- [ ] Vertragsentwurf *(2026-08-14)*\n"
        "  @gc-id: abc123\n"
        "  @gc: mach einen Entwurf\n"
        "  @gc-re: eins\n"
        "  @gc: recherchiere noch die Frist\n"
        "  @gc-re: zwei\n"
        "  @gc: wie viel kommt on top?\n"
        "  @gc-re: drei\n"
        "  @gc: speichere als PDF\n"
        "  @gc-re: vier\n"
        "  @gc-last: opus · ~12k · 2026-08-14 09:45 · $1.23\n",
        encoding="utf-8",
    )
    funde = retro_scan.scan_board(retro_scan.parse_board(board), date(2026, 8, 4))
    assert [f for f in funde if f.signal == "schleife"] == []


def test_killed_ignoriert_eigenen_stop_button(tmp_path, monkeypatch):
    """reason=stop ist der eigene Abbruch (⏹ „Stopped by you") — nie ein
    Agentenfehler. Wächter-Abbrüche (cap/idle/hung) bleiben Kandidaten."""
    p = tmp_path / "killed-runs.jsonl"
    p.write_text(
        '{"ts": "2026-08-18 22:25:33", "gc_id": "aaa111", "title": "Stop", '
        '"reason": "stop", "elapsed_min": 0.1, "steps": 0}\n'
        '{"ts": "2026-08-18 11:45:35", "gc_id": "bbb222", "title": "Cap", '
        '"reason": "cap", "elapsed_min": 60.0, "steps": 870}\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(retro_scan, "KILLED", p)
    funde = retro_scan.scan_killed(date(2026, 8, 10))
    assert [f.gc_id for f in funde] == ["bbb222"]


def test_erwaehnter_crash_ist_kein_crash(tmp_path):
    # 06.08.: der Aufraeum-Turn NACH einem Absturz („nur Runner-Crash-Echos, keine echten
    # Auftraege") wurde als zweiter Absturz gezaehlt — ein Fehlalarm des Scanners.
    board = tmp_path / "board.md"
    board.write_text(
        "# Themen\n"
        "- [ ] Testitem *(2026-08-06)*\n"
        "  @gc-id: abc123\n"
        "  @gc-re: ❌ Runner-Crash: [Errno 2] No such file or directory: 'x.out.json'\n"
        "  @gc-re: Das Item ist korrekt erfasst — die Turns davor waren nur "
        "Runner-Crash-Echos, keine echten Auftraege.\n",
        encoding="utf-8",
    )
    items = retro_scan.parse_board(board)
    funde = [f for f in retro_scan.scan_board(items, __import__("datetime").date(2026, 1, 1))
             if f.signal == "crash"]
    assert len(funde) == 1
    assert funde[0].beleg.startswith("❌ Runner-Crash")


def test_rate_limit_ist_kein_kandidat(tmp_path):
    # 06.08. 17:07: das API-Rate-Limit killte fuenf parallele Runs auf einen Schlag,
    # inklusive der Retro selbst. Nie ein Agentenfehler — sonst frisst es den Deckel.
    board = tmp_path / "board.md"
    board.write_text(
        "# Themen\n"
        "- [ ] Testitem *(2026-08-06)*\n"
        "  @gc-id: abc123\n"
        "  @gc-re: ❌ Agent-Run fehlgeschlagen: is_error=True subtype=success — "
        "You've hit your session limit · resets 6:30pm (Europe/Berlin)\n"
        "  @gc-re: ❌ Runner-Crash: [Errno 2] No such file or directory: 'x.out.json'\n",
        encoding="utf-8",
    )
    items = retro_scan.parse_board(board)
    funde = [f for f in retro_scan.scan_board(items, __import__("datetime").date(2026, 1, 1))
             if f.signal == "crash"]
    assert [f.beleg.startswith("❌ Runner-Crash") for f in funde] == [True]


def test_fund_erbt_nicht_das_datum_der_letzten_item_aktivitaet(tmp_path):
    # Cockpit-Dauerlaeufer: ein Signal von Ende Juli erschien als 06.08., weil das Item
    # zuletzt am 06.08. lief. Der Zeitstempel muss aus dem naechstgelegenen Sidecar kommen.
    board = tmp_path / "board.md"
    board.write_text(
        "# Themen\n"
        "- [ ] Dauerlaeufer *(2026-08-06)*\n"
        "  @gc-id: abc123\n"
        "  @gc-re: Bericht … → volle Antwort: inbox/gc-threads/abc123-20260727-143251-ddbd.md\n"
        "  @gc: das hat scheinbar nicht funktioniert, schau nochmal\n"
        "  @gc-re: ❌ Agent-Run fehlgeschlagen: Timeout nach 30 min\n",
        encoding="utf-8",
    )
    items = retro_scan.parse_board(board)
    funde = {f.signal: f for f in retro_scan.scan_board(items, __import__("datetime").date(2026, 1, 1))}
    assert funde["korrektur"].datum == "2026-07-27"
    assert funde["crash"].datum == "2026-07-27"      # frueher: 2026-08-06
    assert funde["crash"].zeit.startswith("2026-07-27T14:32")


def test_archivzeile_behaelt_datum_und_faellt_aus_altem_fenster(tmp_path):
    """Sweep hängt die Herkunft hinter das Datum; `--archiv` darf deshalb nicht
    den ganzen Altbestand als undatiert in jedes 10-Tage-Fenster ziehen."""
    board = tmp_path / "board-archive.md"
    board.write_text(
        "# Archiv\n"
        "- [x] Altes Item *(2026-07-20)* ← Dev (Board) / Jetzt\n"
        "  @gc-id: alt123\n"
        "  @gc-re: erledigt\n"
        "  @gc: nein, das ist falsch\n",
        encoding="utf-8",
    )
    items = retro_scan.parse_board(board)
    assert items[0].datum == "2026-07-20"
    assert items[0].titel == "Altes Item"
    assert retro_scan.scan_board(items, date(2026, 8, 3)) == []


def test_funddatum_schneidet_altes_signal_aus_langlaeufer(tmp_path):
    board = tmp_path / "board.md"
    board.write_text(
        "# Themen\n"
        "- [ ] Langlaeufer *(2026-08-13)*\n"
        "  @gc-id: long123\n"
        "  @gc-re: alt … → volle Antwort: inbox/gc-threads/long123-20260720-100000-abcd.md\n"
        "  @gc: nein, das ist falsch\n",
        encoding="utf-8",
    )
    fund = retro_scan.scan_board(
        retro_scan.parse_board(board), date(2026, 8, 3)
    )[0]
    assert fund.datum == "2026-07-20"
    assert not retro_scan.im_fenster(fund, date(2026, 8, 3))


def test_nachbar_sidecar_ist_datumsanker_kein_beleg(tmp_path):
    # 18.08.: Crash-Turns stehen inline in board.md und haben kein eigenes Sidecar.
    # Der Scanner gab die Sidecar-Datei des NACHBAR-Turns als Beleg aus — zwei Pruef-Subs
    # lasen daraufhin eine normale Erfolgsantwort (10af606c5850) bzw. einen Owner-Turn
    # (6ecba2c3e110) als „Crash-Beleg". Der Nachbar darf nur das Datum ankern.
    board = tmp_path / "board.md"
    board.write_text(
        "# Themen\n"
        "- [ ] Testitem *(2026-08-16)*\n"
        "  @gc-id: abc123\n"
        "  @gc-re: Done … → volle Antwort: inbox/gc-threads/abc123-20260815-124417-577b.md\n"
        "  @gc-re: ❌ Agent run failed: is_error=True subtype=error_during_execution\n"
        "  @gc: nein, das ist falsch … → voller Text: inbox/gc-threads/abc123-20260816-090000-aaaa.md\n",
        encoding="utf-8",
    )
    items = retro_scan.parse_board(board)
    funde = {f.signal: f for f in retro_scan.scan_board(items, date(2026, 1, 1))}
    crash = funde["crash"]
    assert crash.quelle == "board.md"                 # frueher: …-20260815-124417-577b.md
    assert crash.datum == "2026-08-15"                # Datums-Anker bleibt erhalten
    # Ein Turn mit EIGENEM Sidecar behaelt seinen Pfad als Quelle.
    assert funde["korrektur"].quelle.endswith("abc123-20260816-090000-aaaa.md")


def test_action_trigger_ist_keine_korrektur(tmp_path):
    # „▶ Fehler-Retro ausfuehren" ist der Startknopf, kein Widerspruch — das Wort
    # „Fehler" im Action-Titel machte die Retro zu ihrem eigenen staerksten Signal.
    board = tmp_path / "board.md"
    board.write_text(
        "# Cockpit\n"
        "- [ ] Fehler-Retro *(2026-08-06)*\n"
        "  @gc-id: abc123\n"
        "  @gc-re: 6 Faeden geprueft\n"
        "  @gc: ▶ Fehler-Retro ausführen\n",
        encoding="utf-8",
    )
    items = retro_scan.parse_board(board)
    funde = retro_scan.scan_board(items, __import__("datetime").date(2026, 1, 1))
    assert [f for f in funde if f.signal == "korrektur"] == []


def test_themenwort_im_titel_ist_keine_korrektur(tmp_path):
    """Heisst das Item „Fehler-Retro", trifft `\\bfehler\\b` jeden Owner-Turn dort —
    am 18.08. wurde so ein Lob („… dass da vielleicht Fehler sind") zum
    korrektur·3-Signal. Wortlaut im Item-Titel = Thema, kein Widerspruch."""
    board = tmp_path / "board.md"
    board.write_text(
        "# Themen\n"
        "- [ ] Fehler-Retro *(2026-08-18)*\n"
        "  @gc-id: retro01aaaa\n"
        "  @gc-re: 6 Faeden geprueft\n"
        "  @gc: Sehr gut! Viele ueberprueft, wo aus den Faeden hervorgeht, dass da vielleicht Fehler sind.\n"
        "  @gc-re: naechste Runde\n"
        "  @gc: nein, das ist falsch gelaufen\n",
        encoding="utf-8",
    )
    items = retro_scan.parse_board(board)
    funde = [f for f in retro_scan.scan_board(items, date(2026, 1, 1))
             if f.signal == "korrektur"]
    assert [f.beleg for f in funde] == ["nein, das ist falsch gelaufen"]


def test_zufalls_pool_zieht_nur_signalfreie_ungepruefte_faeden():
    """Reliability-Stichprobe (18.08.): nur Faeden ohne jedes Signal,
    nicht schon geprueft (ausser es gab danach neue Aktivitaet), im Fenster."""
    def item(gid, datum, sidecar=None):
        it = retro_scan.Item(titel=f"t-{gid}", gc_id=gid, datum=datum,
                             turns=[("gc", "auftrag"), ("gc-re", "erledigt")])
        if sidecar:
            it.sidecars.append(sidecar)
        return it

    items = [
        item("sauber000001", "2026-08-15"),                       # rein
        item("signal000001", "2026-08-15"),                       # hat Signal -> raus
        item("zualt0000001", "2026-07-01"),                       # vor Fenster -> raus
        item("geprft000001", "2026-08-10"),                       # geprueft, nichts Neues -> raus
        item("wieder000001", "2026-08-10",                        # geprueft, aber neue Aktivitaet
             sidecar="inbox/gc-threads/wieder000001-20260816-120000-abcd.md"),
    ]
    pool = retro_scan.zufalls_pool(
        items, date(2026, 8, 8), signal_ids={"signal000001"},
        bis={"geprft000001": "2026-08-12T09:00:00", "wieder000001": "2026-08-12T09:00:00"},
    )
    assert set(pool) == {"sauber000001", "wieder000001"}
    assert pool["wieder000001"][1] == "2026-08-16"
