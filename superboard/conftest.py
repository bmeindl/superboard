"""Pytest-Fixtures der Board-Suite.

Warum es diese Datei gibt (verifiziert 2026-07-22):
`check()` in test_server.py sammelt Fehlschlaege nur in der Liste FAILS und wirft
NICHT. Rot wurde die Suite bisher ausschliesslich ueber den Sentinel
`test_zzz_alle_checks_gruen` am Dateiende. Der greift aber nur beim VOLLLAUF —
jede Auswahl deselektiert ihn:

    pytest test_server.py -k "wesen"        -> 2 passed, 53 deselected
    pytest test_server.py::test_gc_append_hardening

Ein roter check() meldete in genau diesen Laeufen "passed". Und so ruft ein Agent
einen Test auf, wenn er gezielt an einer Stelle arbeitet — also genau dann, wenn
das Signal am meisten zaehlt.

Der Hook unten haengt die FAILS-Auswertung an JEDEN Test: was ein Test an neuen
Fehlschlaegen erzeugt, macht genau diesen Test rot. Bewusst NICHT geloest, indem
check() selbst wirft — ein raise mitten in einer Testfunktion wuerde die
Aufraeumarbeit in deren try/finally-Bloecken ueberspringen (Server-Shutdown,
Zurueckbiegen der Modul-Pfade) und die Suite haengen lassen bzw. Live-Pfade
umgebogen zurueckhalten. Der Kontrollfluss der 300+ check()-Aufrufe bleibt exakt
wie er ist; nur die Meldung wird ehrlich.

Bewusst ein Hook auf die call-Phase und keine autouse-Fixture: eine Fixture kann
erst im teardown urteilen, das meldet dann "1 passed, 1 error" statt schlicht
"1 failed" — beim Aufraeumen einer roten Suite ist das genau die Unschaerfe, die
man nicht will.
"""

import pytest


@pytest.fixture(autouse=True)
def _kein_externer_context_reranker(monkeypatch):
    """Tests starten nie versehentlich Haiku/Luna; Integrationsfälle opten explizit ein."""
    monkeypatch.setenv("GC_THREAD_CONTEXT", "0")


@pytest.fixture(autouse=True)
def _kill_log_ins_tmp(tmp_path, monkeypatch):
    """Kein Test schreibt ins ECHTE Kill-Log (verifiziert 2026-07-29).

    `test_interrupt_und_weiter` stoppt einen echten Run über /api/gc-stop; der
    Kill-Pfad ruft `gc_runner.log_kill()` — und das schrieb in das produktive
    `journal/killed-runs.jsonl` samt Stromkopie nach `journal/killed/`. Ergebnis:
    19 Zeilen „Offener Faden (von dir gestoppt, 0 min)" aus Testläufen, die im
    Board als echte Abbruch-Notiz erschienen. Ein Test darf die laufende Instanz
    nicht bemalen.

    KILL_LOG.parent trägt auch das `killed/`-Verzeichnis — das Umbiegen der einen
    Konstante verlegt beides. Autouse statt per Test: der nächste Kill-Pfad, den
    jemand testet, ist damit von vornherein isoliert.

    ABER: das hier ist nur noch der Rückfallschirm für die übrigen Testmodule.
    conftest.py lädt AUSSCHLIESSLICH pytest — `python3 test_server.py` und ein
    direkter Funktionsaufruf laufen daran vorbei und schrieben weiter ins Live-Log
    (verifiziert 2026-07-30, 19 Fantasie-Zeilen im Board). Die maßgebliche
    Umbiegung steht deshalb seitdem auf Modulebene in test_server.py, zusammen mit
    den vier anderen; dort gilt sie für beide Startwege.
    """
    import gc_runner
    monkeypatch.setattr(gc_runner, "KILL_LOG", tmp_path / "journal" / "killed-runs.jsonl")




@pytest.hookimpl(wrapper=True)
def pytest_runtest_call(item):
    """Rote check()-Aufrufe lassen den Test scheitern, der sie ausgeloest hat.

    Bewusst an die FAILS-Liste des JEWEILIGEN Testmoduls gebunden statt hart an
    test_server (Sol-Review 22.07.): der Hook laeuft sonst auch fuer Testdateien,
    die dieses Modul gar nicht benutzen, und importiert es dort nur, um eine
    fremde globale Liste zu lesen. Module ohne FAILS werden schlicht durchgereicht.
    """
    fails = getattr(item.module, "FAILS", None)
    if not isinstance(fails, list):
        return (yield)

    vorher = len(fails)
    ergebnis = yield  # scheitert der Test selbst, fliegt das hier unveraendert durch
    neu = fails[vorher:]
    if neu:
        raise AssertionError(
            "check() ist in diesem Test fehlgeschlagen:\n  - " + "\n  - ".join(neu)
        )
    return ergebnis
