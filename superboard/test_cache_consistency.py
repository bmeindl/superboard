"""Tests für die Caches des Boards — dass Abgeleitetes zur Quelle passt.

Das Board hält an mehreren Stellen abgeleiteten Zustand: den Kill-Log-Cache im Server,
den Auto-Reload-Stempel des Frontends, und die Cache-Wärme-Heuristik im JS. Die
Persistenz-Pfade (ETag, `@gc-session`, `@gc-last`) sind in `test_server.py` gut gedeckt;
diese Datei schließt die Lücken, die dort keine Heimat haben.

Warum das eine eigene Datei ist: die Fälle hier teilen kein Setup mit der Kernsuite —
zwei laufen im Browser, einer gegen `bump.py`. Angehängt an `test_server.py` würden sie
in dessen `check()`-Mechanik hineinlaufen, die eine völlig andere Fehlersemantik hat.

Der teuerste Fall ist bewusst NICHT hier: dass ein laufender Server ALTEN Code im
Speicher hält, während auf Platte neuer liegt (Vorfall 28.07.), lässt sich nicht testen,
solange es dafür keinen Guard gibt — ein Test kann kein fehlendes Feature prüfen. Das
ist als Befund festgehalten, nicht als grüner Haken getarnt.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import bump  # noqa: E402
import gc_runner  # noqa: E402
import server  # noqa: E402

SESSION = "board-cache-test"

# Fixtures für die Wärme-Heuristik. Jede Zeile ist ein Zustand, in dem sich JS und Python
# einig sein MÜSSEN — sie entscheiden dieselbe Frage: startet der nächste Run mit --resume?
RESUME_FIXTURES = [
    ("offener Faden, Antwort zuletzt",
     "11111111-2222-3333-4444-555555555555 · board-x",
     [{"kind": "ask"}, {"kind": "reply"}]),
    ("Faden geschlossen NACH der letzten Antwort",
     "11111111-2222-3333-4444-555555555555 · board-x",
     [{"kind": "ask"}, {"kind": "reply"}, {"kind": "done"}]),
    ("done VOR der letzten Antwort (alter Schnitt, danach weitergearbeitet)",
     "11111111-2222-3333-4444-555555555555 · board-x",
     [{"kind": "ask"}, {"kind": "done"}, {"kind": "ask"}, {"kind": "reply"}]),
    ("Session-Handle ist Muell (Hand-Edit)",
     "kein-uuid-handle-!!! · board-x",
     [{"kind": "ask"}, {"kind": "reply"}]),
    ("gar keine Session",
     "",
     [{"kind": "ask"}, {"kind": "reply"}]),
    ("Session ohne Label",
     "11111111-2222-3333-4444-555555555555",
     [{"kind": "ask"}, {"kind": "reply"}]),
    ("leerer Faden",
     "11111111-2222-3333-4444-555555555555 · board-x",
     []),
    ("nur done, nie eine Antwort",
     "11111111-2222-3333-4444-555555555555 · board-x",
     [{"kind": "ask"}, {"kind": "done"}]),
]


def python_will_resume(session: str, thread: list[dict]) -> bool:
    """Die Referenz: der Runner resumt genau dann, wenn der Handle UUID-artig ist UND
    der Faden nicht nach der letzten Antwort geschlossen wurde."""
    return bool(gc_runner.session_uuid(session)) and not gc_runner.session_cut(thread)


# ---------------------------------------------------------------- APP_VERSION-Stempel

@pytest.mark.parametrize("cur,today,erwartet", [
    ("2026-07-28c", "2026-07-28", "2026-07-28d"),   # gleicher Tag → nächster Buchstabe
    ("2026-07-27f", "2026-07-28", "2026-07-28a"),   # neuer Tag → zurück auf a
    ("2026-07-28z", "2026-07-28", "2026-07-28aa"),  # Überlauf, >26 Frontend-Commits am Tag
    ("2026-07-28az", "2026-07-28", "2026-07-28ba"),
    ("2026-07-28", "2026-07-28", "2026-07-28a"),    # Stempel ohne Suffix
])
def test_app_version_stempel_zaehlt_hoch(cur, today, erwartet):
    assert bump.next_app_version(cur, today) == erwartet


def test_app_version_stempel_ist_immer_verschieden():
    """Der einzige Zweck des Stempels: ein offener Tab muss die Änderung SEHEN. Ein
    Bump, der denselben String zurückgibt, wäre schlimmer als keiner — er sähe im
    Diff nach Sorgfalt aus und würde trotzdem keinen Reload auslösen."""
    stamp, today = "2026-07-28a", "2026-07-28"
    gesehen = {stamp}
    for _ in range(60):  # über den z→aa-Überlauf hinaus
        stamp = bump.next_app_version(stamp, today)
        assert stamp not in gesehen, f"Stempel wiederholt sich: {stamp}"
        gesehen.add(stamp)


def test_index_html_traegt_einen_lesbaren_stempel():
    """Verankert die Annahme, auf der write_app_version steht."""
    m = bump.APP_VERSION_RE.search(bump.INDEX.read_text(encoding="utf-8"))
    assert m, "APP_VERSION-Zeile in index.html nicht gefunden — bump.py würde abbrechen"
    assert bump.next_app_version(m.group(1), "2026-07-28") != m.group(1)


# ---------------------------------------------------------------- Kill-Log-Cache

def test_kill_cache_liefert_nach_zweitem_eintrag_frische_daten(tmp_path):
    """`killed_today()` cacht über die mtime und wird alle 5 s abgefragt. Der bestehende
    Test in test_server.py setzt den Cache von Hand zurück (`_KILL_CACHE.update(mtime=-1)`)
    — er UMGEHT die Invalidierung also, statt sie zu prüfen. Hier bleibt sie scharf:
    zweiter Eintrag in dieselbe Datei muss auftauchen, ohne dass jemand nachhilft."""
    server._KILL_CACHE.update(mtime=-1.0, rows=[])
    log = tmp_path / "killed-runs.jsonl"
    heute = time.strftime("%Y-%m-%d")

    def schreibe(gc_id: str) -> None:
        with log.open("a") as f:
            f.write(json.dumps({"ts": f"{heute} 12:00", "gc_id": gc_id, "title": "T",
                                "reason": "idle", "elapsed_min": 1, "last_tool": "Bash"}) + "\n")

    schreibe("aaaaaaaaaaaa")
    erste = server.killed_today(tmp_path)
    assert [r["gc_id"] for r in erste] == ["aaaaaaaaaaaa"]

    # mtime muss sich messbar ändern — sonst prüfen wir die Auflösung der Uhr, nicht den Cache.
    vorher = log.stat().st_mtime
    for _ in range(50):
        schreibe("bbbbbbbbbbbb")
        if log.stat().st_mtime != vorher:
            break
        log.unlink()
        log.write_text("")
        schreibe("aaaaaaaaaaaa")
        time.sleep(0.02)
    else:
        pytest.skip("Dateisystem-mtime ändert sich nicht messbar — Cache nicht prüfbar")

    zweite = server.killed_today(tmp_path)
    assert [r["gc_id"] for r in zweite] == ["aaaaaaaaaaaa", "bbbbbbbbbbbb"], \
        "killed_today liefert gecachte Daten, obwohl die Datei sich geändert hat"


def test_kill_cache_faellt_ueber_mitternacht_nicht_auf_gestern_zurueck(tmp_path):
    """Der Cache hing bis 29.07. NUR an der mtime — „heute" wurde einmal berechnet und
    dann festgehalten. Der Server läuft als Dauerprozess: killte nach dem letzten Abbruch
    niemand mehr einen Run, blieb die Datei unverändert und das Board meldete am nächsten
    Morgen „⚠ N Runs heute abgebrochen" mit den Abbrüchen von GESTERN („Seems
    outdated, there was something yesterday"). Das Datum gehört in den Cache-Schlüssel."""
    server._KILL_CACHE.update(mtime=-1.0, day="", rows=[])
    log = tmp_path / "killed-runs.jsonl"
    log.write_text(json.dumps({"ts": "1999-01-01 23:59", "gc_id": "gesternnnnnn",
                               "title": "T", "reason": "stop"}) + "\n")
    server.killed_today(tmp_path)  # Cache füllen
    # Genau der reale Zustand: Cache stammt von gestern, die Datei hat sich NICHT bewegt.
    server._KILL_CACHE.update(day="1999-01-01",
                              rows=[{"gc_id": "gesternnnnnn", "ts": "1999-01-01 23:59"}])
    assert server.killed_today(tmp_path) == [], \
        "killed_today zeigt gestrige Abbrueche als heutige — Tageswechsel invalidiert nicht"


def test_kill_cache_ueberlebt_eine_geloeschte_datei(tmp_path):
    """Nach dem Befüllen verschwindet die Datei (Aufräumen/Rotation). Erwartet: leere
    Liste, kein Absturz und keine Zombie-Zeilen aus dem Cache."""
    server._KILL_CACHE.update(mtime=-1.0, rows=[])
    log = tmp_path / "killed-runs.jsonl"
    log.write_text(json.dumps({"ts": f"{time.strftime('%Y-%m-%d')} 12:00", "gc_id": "cccccccccccc",
                               "title": "T", "reason": "idle"}) + "\n")
    assert server.killed_today(tmp_path)
    log.unlink()
    assert server.killed_today(tmp_path) == []


# ---------------------------------------------------------------- JS gegen Python

def _ab(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["agent-browser", *args, "--session", SESSION],
                          capture_output=True, text=True, timeout=60)


@pytest.fixture(scope="module")
def geladene_seite():
    """Board-Seite in einem echten Browser, gegen ein Temp-Board. Nie die Live-board.md.

    Skip statt Fehlschlag ohne agent-browser: derselbe Vertrag wie test_ui_smoke.py —
    auf der headless VPS-Instanz gibt es keinen Browser, und ein rotes Kreuz dafür wäre Rauschen."""
    if not shutil.which("agent-browser"):
        pytest.skip("agent-browser nicht installiert (Browser-Tests sind Mac-only)")

    tmp = Path(tempfile.mkstemp(suffix=".md")[1])
    tmp.write_text("## T\n\n### Jetzt\n\n- [ ] Item *(2026-07-28)*\n  @gc-id: aaaaaaaaaaaa\n"
                   "\n### Bald\n\n### Geparkt\n\n# Personen\n\n# Notizen\n")
    server.Handler.board_path = tmp
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        if _ab("open", "about:blank").returncode:   # Daemon-Cold-Start-Quirk, ein Warm-up
            _ab("close")
            _ab("open", "about:blank")
        if _ab("open", url).returncode:
            pytest.skip("agent-browser installiert, startet aber nicht")
        time.sleep(2)  # das Board lädt /api/board nach
        yield _ab
    finally:
        _ab("close")
        httpd.shutdown()
        tmp.unlink(missing_ok=True)


def test_js_und_python_sind_sich_beim_resume_einig(geladene_seite):
    """Die Drift-Bremse. `willResume()` im Frontend und `session_uuid`+`session_cut` im
    Runner entscheiden dieselbe Frage — aber in zwei Sprachen, zweimal implementiert.
    Läuft die JS-Kopie weg, verspricht die Cache-Pill „hier kommst du günstig rein" für
    Items, die garantiert kalt starten, UND verzerrt die Triage-Sortierung (cacheLeftMin
    ist laut index.html ihr einziger Ranking-Input). Genau dieser Fehler war am 28.07.
    schon einmal da; hier ist er ab jetzt eine rote Zeile statt einer stillen Verzerrung."""
    faelle = [{"session": s, "thread": t} for _n, s, t in RESUME_FIXTURES]
    ausgabe = geladene_seite(
        "eval", f"JSON.stringify(({json.dumps(faelle)}).map(willResume))").stdout
    treffer = json.loads(ausgabe[ausgabe.index("["):ausgabe.rindex("]") + 1])

    assert len(treffer) == len(RESUME_FIXTURES)
    for (name, session, thread), js in zip(RESUME_FIXTURES, treffer):
        assert js == python_will_resume(session, thread), \
            f"Drift bei '{name}': JS sagt {js}, Runner sagt {python_will_resume(session, thread)}"


def test_laufender_run_zaehlt_nicht_als_warmer_cache(geladene_seite):
    """Regression zum Fix vom 28.07.: ein Item, dessen Run gerade LÄUFT, darf nicht als
    „warmer Cache" zählen — technisch mag die Session warm sein, aber der owner kann
    währenddessen nicht selbst hineinspringen. Ohne diese Regel zeigte der Zähler in der
    Fußzeile Wärme für genau die Items, bei denen sie nichts nützt."""
    jetzt = time.strftime("%Y-%m-%d %H:%M")
    item = {"id": "aaaaaaaaaaaa", "gc_last": f"~10k · {jetzt} · $0.10",
            "session": "11111111-2222-3333-4444-555555555555 · board-x",
            "thread": [{"kind": "ask"}, {"kind": "reply"}]}

    def leftmin(js_setup: str) -> int:
        out = geladene_seite("eval", f"{js_setup}; cacheLeftMin({json.dumps(item)})").stdout
        zahlen = [int(x) for x in out.split() if x.lstrip("-").isdigit()]
        assert zahlen, f"keine Zahl in agent-browser-Ausgabe: {out!r}"
        return zahlen[-1]

    assert leftmin("running = []; queued = []") > 0, \
        "frisches @gc-last ohne laufenden Run muesste warm sein — Fixture oder TTL kaputt"
    assert leftmin("running = ['aaaaaaaaaaaa']; queued = []") == 0, \
        "laufender Run zaehlt faelschlich als warmer Cache"
    assert leftmin("running = []; queued = ['aaaaaaaaaaaa']") == 0, \
        "wartender Run zaehlt faelschlich als warmer Cache"


def test_codex_bekommt_keine_claude_ttl_und_eigenen_tooltip(geladene_seite):
    """Codex bekommt eine eigene, GEMESSENE Uhr — nur eben nicht Claudes 5 Minuten.

    Bis 19.08. stand hier das Gegenteil: Codex durfte gar keine Uhr haben, weil keine TTL
    bekannt war. Sie ist es jetzt (§5k: keine Klippe bei 5 min, Trefferquote hält bis ~60
    min), also zählt die Pille 60 statt 5 Minuten herunter. Was der Vertrag WEITER verbietet,
    ist das Verwechseln der beiden Fenster — deshalb prüft der Test unten explizit, dass in
    der Codex-Pille nirgends „5-minute" steht und dass ihre Restzeit deutlich über Claudes
    Fenster liegt.

    Zweite Hälfte des Vertrags: die 60-Minuten-Pille an der KARTE bleibt trotzdem da.
    Sie beantwortet „wo war ich zuletzt dran" (12.08.) — 2.37.4 hatte sie für Codex
    mit ausgeblendet, weil sie am selben Namen hing wie die Cache-Ökonomie."""
    jetzt = time.strftime("%Y-%m-%d %H:%M")
    item = {"id": "cccccccccccc", "gc_last": f"~173k · {jetzt}",
            "session": "019ff158-1a77-7e23-a685-366b4e0f391b · board-x · codex",
            "thread": [{"kind": "ask"}, {"kind": "reply"}],
            "cache_observation": {"cross_run_input_tokens": 31174,
                                  "cross_run_cache_read": 26112,
                                  "cross_run_cache_hit_pct": 84}}
    script = f"""
      (() => {{
        running = []; queued = [];
        const it = {json.dumps(item)};
        const p = cacheWinPill(it);
        const card = cacheBadge(it);
        return JSON.stringify({{
          runner: itemRunner(it), rank: cacheLeftMin(it),
          text: p && p.textContent, title: p && p.title,
          expiry: p && p.dataset.exp || null,
          karte: !!card, karteTitle: card && card.title
        }});
      }})()
    """
    out = geladene_seite("eval", script).stdout
    # agent-browser quoted komplexe JS-Rückgaben noch einmal als JSON-String.
    encoded = out[out.index('"'):out.rindex('"') + 1]
    data = json.loads(json.loads(encoded))
    # rank bleibt 0: die Cache-OEKONOMIE (Triage-Sortierung, "Caches warm"-Zaehler) laesst
    # Codex weiter aussen vor — bewusste Produktentscheidung, s. Kommentar an cacheExpiry().
    assert data["runner"] == "codex" and data["rank"] == 0
    # Die Uhr laeuft jetzt, aber im 60- und nicht im 5-Minuten-Fenster.
    assert data["expiry"], "Codex-Pille hat keine Ablaufzeit mehr bekommen"
    rest_min = (int(data["expiry"]) - time.time() * 1000) / 60000
    assert 50 < rest_min <= 60, f"Codex-Restzeit {rest_min:.1f} min liegt nicht im 60er-Fenster"
    assert data["text"].startswith("Codex cache "), data["text"]
    assert "min" in data["text"] and "last 84%" in data["text"], data["text"]
    # Der eigentliche Vertrag: Codex darf NIE Claudes Fenster fuer sich behaupten. Das
    # VERNEINEN ("has no 5-minute cliff") ist ausdruecklich erlaubt und sogar der Punkt —
    # geprueft werden deshalb die beiden Saetze, mit denen die Claude-Pille ihr eigenes
    # 5-min-Fenster behauptet, nicht die blosse Zeichenfolge "5-minute".
    for claude_satz in ("5-minute cache window", "5-minute window since"):
        assert claude_satz not in data["title"], data["title"]
    assert data["karte"], "Codex-Item hat keine Aktualitaets-Pille an der Karte"
    assert "last worked" in data["karteTitle"], data["karteTitle"]
    assert "Cache" not in data["karteTitle"].split("\n")[0], \
        "Karten-Tooltip verkauft die Aktualitaet bei Codex als Cache-Aussage"


def test_codex_uhr_wird_durch_fremde_laeufe_relativiert(geladene_seite):
    """Bei Codex ist nicht die Uhr die Grenze, sondern die Verdraengung.

    §5k: Codex teilt einen Prefix-Cache ueber Prozesse hinweg. Lief in der Luecke eine
    ANDERE Codex-Session, faellt die starke Trefferquote im 60-120-min-Fenster von 79 %
    auf 30 %. Eine Pille, die nur die Restzeit zeigt, waere also zu optimistisch — der
    Zaehler `codex_runs_since` (server.annotate_cross_run_cache) muss sichtbar werden UND
    die Pille optisch umschalten. Ohne diesen Test kann der Zaehler serverseitig wegfallen,
    ohne dass irgendetwas rot wird."""
    jetzt = time.strftime("%Y-%m-%d %H:%M")
    basis = {"id": "cccccccccccc", "gc_last": f"~173k · {jetzt}",
             "session": "019ff158-1a77-7e23-a685-366b4e0f391b · board-x · codex",
             "thread": [{"kind": "ask"}, {"kind": "reply"}]}
    ruhig = {**basis, "cache_observation": {"cross_run_input_tokens": 31174,
                                            "cross_run_cache_read": 26112,
                                            "cross_run_cache_hit_pct": 84,
                                            "codex_runs_since": 0}}
    gedraengt = {**basis, "cache_observation": {**ruhig["cache_observation"],
                                                "codex_runs_since": 3}}
    script = f"""
      (() => {{
        running = []; queued = [];
        const mach = it => {{ const p = cacheWinPill(it);
          return {{text: p.textContent, title: p.title, pressed: p.classList.contains("pressed")}}; }};
        return JSON.stringify({{ruhig: mach({json.dumps(ruhig)}), gedraengt: mach({json.dumps(gedraengt)})}});
      }})()
    """
    out = geladene_seite("eval", script).stdout
    encoded = out[out.index('"'):out.rindex('"') + 1]
    data = json.loads(json.loads(encoded))
    # Ohne fremde Laeufe: reine Uhr, keine Warnung.
    assert not data["ruhig"]["pressed"]
    assert "other run" not in data["ruhig"]["text"], data["ruhig"]["text"]
    # Mit fremden Laeufen: Zahl im Text, Warnfarbe, Begruendung im Tooltip.
    assert data["gedraengt"]["pressed"], "verdraengte Codex-Pille schaltet nicht optisch um"
    assert "3 other runs" in data["gedraengt"]["text"], data["gedraengt"]["text"]
    assert "eviction" in data["gedraengt"]["title"].lower(), data["gedraengt"]["title"]
    # Die Restzeit selbst bleibt in beiden Faellen dieselbe — verdraengt heisst nicht abgelaufen.
    assert data["ruhig"]["text"].split(" · ")[0] == data["gedraengt"]["text"].split(" · ")[0]


def test_claude_pill_zeigt_gemessenen_cross_run_treffer(geladene_seite):
    """Die Uhr sagt voraus, „zuletzt X %" misst — beides gehoert in dieselbe Pille.

    13.08.: der owner will sehen, wie viel der naechste Absende-Vorgang tatsaechlich aus dem
    Cache reaktiviert hat, nicht die Within-Run-Quote. Ohne Messung darf die Uhr nicht
    stillschweigend eine Zahl erfinden — deshalb der zweite Teil des Vertrags."""
    jetzt = time.strftime("%Y-%m-%d %H:%M")
    basis = {"id": "dddddddddddd", "gc_last": f"~85k · {jetzt}",
             "session": "019ff158-1a77-7e23-a685-366b4e0f391b · board-x",
             "thread": [{"kind": "ask"}, {"kind": "reply"}]}
    mit = dict(basis, cache_observation={"cross_run_input_tokens": 55_007,
                                         "cross_run_cache_read": 17_617,
                                         "cross_run_cache_hit_pct": 32,
                                         "context_source": "claude-turn1"})
    script = f"""
      (() => {{
        running = []; queued = [];
        const a = cacheWinPill({json.dumps(mit)}), b = cacheWinPill({json.dumps(basis)});
        return JSON.stringify({{mitText: a && a.textContent, mitTitle: a && a.title,
                                ohneText: b && b.textContent}});
      }})()
    """
    out = geladene_seite("eval", script).stdout
    encoded = out[out.index('"'):out.rindex('"') + 1]
    data = json.loads(json.loads(encoded))
    assert data["mitText"].startswith("Cache ") and data["mitText"].endswith("· last 32%"), \
        data["mitText"]
    assert "17,617" in data["mitTitle"] and "55,007" in data["mitTitle"], data["mitTitle"]
    assert "last" not in (data["ohneText"] or ""), \
        "Pille erfindet einen Cache-Treffer ohne Messung"
