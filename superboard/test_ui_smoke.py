#!/usr/bin/env python3
"""UI-Smoke-Test fürs todo-board-Frontend (index.html) — bewusst MINIMAL.

Prüft die „Seite ist kaputt"-Klasse plus ein paar kleine Interaktionsverträge, die
schon einmal sichtbar falsch waren (2026-07-16, Faden 7b4f4f4ed851: „nicht die
Komplexität erhöhen … ändern ja noch die ganze Zeit einiges"). Die acht Checks sind
Invarianten, die JEDEN UI-Umbau überleben:

    1. Seite lädt (HTTP + Navigation ok)
    2. keine uncaught JS-Errors (agent-browser `errors`)
    3. Board-Daten gerendert (Thema aus dem Temp-Board erscheint im DOM)
    4. Version im DOM (beweist, dass /api/board durchkam)
    5. GC-Faden ist ein tastaturbedienbarer Dialog und gibt den Fokus zurück
    6. Beschriftete relative UND absolute Repo-Links öffnen denselben sicheren Viewer
       wie ein nackter Datei-Pfad (nicht einen kaputten HTTP-Dateisystempfad)
    7. Die CREW-Kachel öffnet auch beim Klick auf ein Kind und schließt außerhalb
    8. Im Faden ist Agent die gefüllte Primäraktion; Save speichert nur einen Turn

Werkzeug: agent-browser-CLI (ist auf dem Mac eh installiert — KEIN pip-playwright,
keine neue Dependency). Fehlt agent-browser (headless VPS instance): Skip mit Exit 0.
Ist es installiert, startet aber trotz drei Warm-up-Versuchen nicht: echter Fehler statt falschem Grün.
Läuft NIE gegen die Live-board.md — eigener Server auf ephemerem Port, Temp-Board.
Eigene Browser-Session `board-smoke`, wird am Ende geschlossen.

    python3 test_ui_smoke.py        # exit 0 = grün/skip, 1 = Fehler
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import urlopen

sys.path.insert(0, str(Path(__file__).resolve().parent))
import server  # noqa: E402

SESSION = "board-smoke"
def _abs_repo_file() -> Path:
    """A file that really exists under GC_ROOT, for the absolute-path viewer check.

    GC_ROOT follows the working directory, so WHICH file is there depends on where
    the suite was started from — hardcoding the repo README makes the check fail
    with a 404 that says nothing about the viewer. Pick the first candidate that is
    actually present and actually under GC_ROOT."""
    root = str(server.GC_ROOT)
    here = Path(__file__).resolve().parent
    for candidate in (server.GC_ROOT / "README.md", here / "ARCHITEKTUR.md",
                      here / "CHANGELOG.md"):
        if candidate.is_file() and str(candidate).startswith(root):
            return candidate
    raise RuntimeError(f"no readable file under GC_ROOT ({root}) for the viewer check")


ABS_README = _abs_repo_file()
SYNTH = ("## SmokeThema\n\n### Jetzt\n\n- [ ] Smoke-Item *(2026-07-10)*\n"
         "  @gc-id: aaaaaaaaaaaa\n"
         "  @gc: Smoke-Frage\n"
         "  @gc-re: [Open README](README.md) · "
         f"[Absolute README]({ABS_README}) · Raw absolute: {ABS_README}\n\n"
         "### Bald\n\n### Geparkt\n\n"
         "## My to-dos\n\n### Jetzt\n\n### Bald\n\n### Geparkt\n\n"
         "# Personen\n\n# Notizen\n")

FAILS: list[str] = []


def check(name: str, cond: bool) -> None:
    print(("  OK  " if cond else " FAIL ") + name)
    if not cond:
        FAILS.append(name)


def ab(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["agent-browser", *args, "--session", SESSION],
                          capture_output=True, text=True, timeout=60)


def main() -> int:
    FAILS.clear()  # main() kann aus pytest und als Skript im selben Prozess mehrfach laufen
    if not shutil.which("agent-browser"):
        print("SKIP: agent-browser nicht installiert (Smoke ist Mac-only)")
        return 0

    fd, tmp = tempfile.mkstemp(suffix=".md")
    Path(tmp).write_text(SYNTH)
    server.Handler.board_path = Path(tmp)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{httpd.server_address[1]}"
    browser_opened = False
    try:
        # Daemon-Cold-Start-Quirk: erster open nach Stopp schlägt gern fehl → Warm-up-Retry.
        # Der frühere Sofort-Retry reichte nicht: er lief in dieselbe halb hochgefahrene
        # Instanz und scheiterte identisch (falsches 🔴 im nightly Health-Check am 18.08.,
        # 24.08. und 01.09.). Zwischen den Versuchen deshalb erst die Session abräumen und
        # dem Daemon Zeit geben — von Hand reproduziert: nach open+close ist der Smoke grün.
        for versuch in range(3):
            opened = ab("open", "about:blank")
            if opened.returncode == 0:
                break
            ab("close")
            time.sleep(1.5 * (versuch + 1))
        if opened.returncode != 0:
            check("smoke: installierter agent-browser startet", False)
            print(f"       {opened.stderr.strip()[:240]}")
            return 1
        browser_opened = True
        ab("errors", "--clear")

        nav = ab("open", url)
        check("smoke: Seite lädt", nav.returncode == 0)
        ab("wait", "1200")

        errs = ab("errors").stdout.strip()
        check("smoke: keine uncaught JS-Errors", errs == "")
        if errs:
            print("       " + errs.replace("\n", "\n       "))

        # Seit 0.10.0 ist Cockpit der Default-Tab — die Themen-Matrix lebt im To-dos-View.
        # Erst umschalten, dann prüfen (die Invariante bleibt: Board-Daten kommen im DOM an).
        ab("eval", "document.querySelector('[data-view=todos]').click()")
        ab("wait", "300")
        body = ab("get", "text", "body").stdout
        check("smoke: Board-Daten gerendert (Thema sichtbar)", "SmokeThema" in body)
        ver = ab("eval", "document.body.innerText.match(/Build \\d+\\.\\d+\\.\\d+/)?.[0] || ''").stdout
        check("smoke: Version im DOM (API /api/board kam durch)", f"Build {server.VERSION}" in ver)

        # Consecutive manual saves must preserve the browser-assigned IDs. Previously
        # the first card stayed ID-less in this tab; the next save could then treat the
        # server's copy as a second card during conflict recovery.
        add_manual = """
          (() => {
            const input = [...document.querySelectorAll('.adder')].find(a => {
              const loc = JSON.parse(a.dataset.loc || '{}');
              return loc.kind === 't' && loc.ti === 1 && loc.col === 'Jetzt';
            });
            if (!input) return false;
            input.value = %s;
            input.dispatchEvent(new KeyboardEvent('keydown', {
              key: 'Enter', bubbles: true, cancelable: true
            }));
            return true;
          })()
        """
        first_add = ab("eval", add_manual % json.dumps("First normal task"))
        ab("wait", "700")
        second_add = ab("eval", add_manual % json.dumps("Second normal task"))
        ab("wait", "700")
        saved = server.parse_board(Path(tmp).read_text())
        normal = next(t for t in saved["themes"] if t["name"] == "My to-dos")
        cards = normal["cols"]["Jetzt"]
        check("smoke: two manual cards keep two stable IDs",
              first_add.returncode == second_add.returncode == 0
              and [card["title"] for card in cards] == ["First normal task", "Second normal task"]
              and len({card["id"] for card in cards}) == 2
              and all(re.fullmatch(r"[0-9a-f]{12}", card["id"]) for card in cards))

        # Absolut ist nur Schreibkomfort: derselbe default-deny Viewer muss die Repo-Datei
        # liefern und eine existierende Datei AUSSERHALB des Repos weiter ablehnen.
        with urlopen(url + "/repo-file/" + quote(str(ABS_README), safe=""), timeout=5) as res:
            check("smoke: absoluter Repo-Pfad wird ausgeliefert", res.status == 200 and len(res.read()) > 100)
        outside_blocked = False
        try:
            urlopen(url + "/repo-file/" + quote(tmp, safe=""), timeout=5)
        except HTTPError as exc:
            outside_blocked = exc.code == 404
        check("smoke: absoluter Pfad ausserhalb des Repos bleibt gesperrt", outside_blocked)

        # CREW rendert beim Oeffnen seinen Inhalt neu. Der Klick muss deshalb an der
        # Komponentengrenze enden: sonst sieht der document-Closer einen abgetrennten
        # Target und klappt das gerade geoeffnete Menu sofort wieder zu.
        crew = ab("eval", """
          (() => {
            const child = document.querySelector('.gc-crewbox');
            child.click();
            const opened = document.querySelector('.gc-crewmenu')?.classList.contains('show');
            document.body.click();
            const closed = !document.querySelector('.gc-crewmenu')?.classList.contains('show');
            return JSON.stringify({opened, closed});
          })()
        """).stdout
        try:
            crew_result = json.loads(json.loads(crew))
        except (json.JSONDecodeError, TypeError):
            crew_result = {}
        check("smoke: CREW-Kindflaeche oeffnet, Aussenklick schliesst", crew_result == {
            "opened": True, "closed": True,
        })

        # Ritual-Gate stilllegen, BEVOR der Dialog geprüft wird (06.08.): ist gerade ein
        # Ritual überfällig, blockt `openGcOverlay()` absichtlich jeden Faden („erst füttern
        # oder snoozen") — dann öffnet der Dialog nicht und der Test war rot, obwohl nichts
        # kaputt ist. Das Gate hängt an echter Uhrzeit aus `rituale.json`, der Test war also
        # tageszeitabhängig. Hier wird nur der Client-Zustand entschärft, keine Datei angefasst.
        ab("eval", "gateSilenced = true; rituale.forEach(r => r.status = 'ok'); teardownGate();")

        # Der GC-Faden ist ein echter modaler Dialog: Screenreader bekommen die Semantik
        # und Escape bringt den Tastaturfokus zum auslösenden Element zurück.
        a11y = ab("eval", """
          (() => {
          const opener = document.querySelector('.pill.for-owner');
          const openerSemantic = opener?.getAttribute('role') === 'button' && opener?.tabIndex === 0;
          opener.focus();
          opener.dispatchEvent(new KeyboardEvent('keydown', {
            key: 'Enter', bubbles: true, cancelable: true
          }));
          const dialog = document.querySelector('.gc-overlay');
          const semantic = dialog?.getAttribute('role') === 'dialog'
            && dialog?.getAttribute('aria-modal') === 'true'
            && dialog?.getAttribute('aria-labelledby') === 'gc-overlay-title'
            && document.getElementById('gc-overlay-title')?.textContent === 'Smoke-Item';
          const repoLink = dialog?.querySelector(
            'a[href="/repo-file/README.md"]'
          );
          const repoLinkStyled = repoLink?.dataset.repoViewer === 'rendered'
            && typeof repoLink.onclick === 'function'
            && !repoLink.hasAttribute('target');
          const absoluteRepoLink = Array.from(dialog?.querySelectorAll('a') || [])
            .find(a => a.textContent === 'Absolute README');
          const absoluteRepoLinkStyled = absoluteRepoLink?.getAttribute('href')
              ?.startsWith('/repo-file/%2F')
            && absoluteRepoLink?.dataset.repoViewer === 'rendered'
            && typeof absoluteRepoLink.onclick === 'function'
            && !absoluteRepoLink.hasAttribute('target');
          // Welche Datei _abs_repo_file() gewaehlt hat, haengt an der Installation — der
          // Test prueft die BEHANDLUNG eines rohen absoluten Pfades, nicht den Repo-Aufbau.
          const rawAbsoluteRepoLink = Array.from(dialog?.querySelectorAll('a.pathlink') || [])
            .find(a => a.textContent.startsWith('/') && a.textContent.endsWith('.md'));
          const rawAbsoluteRepoLinkStyled = rawAbsoluteRepoLink?.getAttribute('href')
              ?.startsWith('/repo-file/%2F')
            && rawAbsoluteRepoLink?.dataset.repoViewer === 'rendered'
            && typeof rawAbsoluteRepoLink.onclick === 'function'
            && !rawAbsoluteRepoLink.hasAttribute('target');
          const send = dialog?.querySelector('.gc-actions .send');
          const run = dialog?.querySelector('.gc-actions .run-btn');
          const resolveBg = token => {
            const probe = document.createElement('span');
            probe.style.background = `var(${token})`;
            document.body.appendChild(probe);
            const value = getComputedStyle(probe).backgroundColor;
            probe.remove();
            return value;
          };
          const actionHierarchy = send?.textContent === 'Save'
            && run?.textContent === '▶ Agent'
            && getComputedStyle(send).backgroundColor === resolveBg('--surface')
            && getComputedStyle(run).backgroundColor === resolveBg('--accent-active');
          dialog.querySelector('textarea').dispatchEvent(new KeyboardEvent('keydown', {
            key: 'Escape', bubbles: true, cancelable: true
          }));
          return JSON.stringify({openerSemantic, semantic, repoLinkStyled, absoluteRepoLinkStyled,
            rawAbsoluteRepoLinkStyled,
            actionHierarchy,
            closed: !document.querySelector('.gc-overlay')});
          })()
        """).stdout
        ab("wait", "50")
        restored = ab("eval", "document.activeElement === document.querySelector('.pill.for-owner')").stdout
        try:
            a11y_result = json.loads(json.loads(a11y))
            restored_focus = json.loads(restored)
        except (json.JSONDecodeError, TypeError):
            a11y_result, restored_focus = {}, False
        a11y_ok = (a11y_result == {"openerSemantic": True, "semantic": True,
                                   "repoLinkStyled": True, "absoluteRepoLinkStyled": True,
                                   "rawAbsoluteRepoLinkStyled": True,
                                   "actionHierarchy": True,
                                   "closed": True}
                   and restored_focus is True)
        check("smoke: GC-Dialog + formatierter Repo-Link + Fokus-Rückgabe", a11y_ok)
        if not a11y_ok:
            print("       semantics=" + a11y.strip().replace("\n", " "))
            print("       active=" + restored.strip().replace("\n", " "))
    finally:
        if browser_opened:
            ab("close")
        httpd.shutdown()
        Path(tmp).unlink(missing_ok=True)

    print()
    print("SMOKE GRÜN" if not FAILS else f"{len(FAILS)} FEHLGESCHLAGEN: {FAILS}")
    return 1 if FAILS else 0


def test_cockpit_add_button_points_at_a_real_zone() -> None:
    """Das + im Zonenkopf ist HTML (`data-zone`), die Zonenliste ist JS (ACTION_ZONES).
    Ein Tippfehler auf der einen Seite waere ein stumm toter Knopf — im Browser sichtbar
    erst, wenn jemand draufklickt. Billiger Statik-Check statt Browser-Fall.
    """
    src = (Path(__file__).resolve().parent / "index.html").read_text(encoding="utf-8")
    zones = set(re.findall(r'\{ key: "([^"]+)",\s+box: "#cz-', src))
    used = set(re.findall(r'class="cz-add" data-zone="([^"]+)"', src))
    assert zones, "ACTION_ZONES nicht gefunden — Regex passt nicht mehr zum Code"
    assert used, "kein + im Cockpit-Zonenkopf gefunden"
    assert used <= zones, f"cz-add zeigt auf unbekannte Zone(n): {sorted(used - zones)}"


def test_ui_smoke() -> None:
    """pytest-Einstieg. Ohne den sammelt pytest aus dieser Datei 0 Tests — die Datei
    hatte nur main(), lief also NIRGENDS automatisch, obwohl README/CHANGELOG sie als
    Teil des nightly Guards führten (Entscheidung 22.07.: einhängen).

    Skip statt Fehlschlag, wenn agent-browser fehlt: der Smoke ist Mac-only, auf
    einer headless VPS-Instanz gibt es keinen Browser. Ist das Binary vorhanden, muss
    der Smoke dagegen wirklich laufen — ein kaputter Daemon darf nicht als Grün durchgehen.
    """
    import pytest  # lazy — der Skriptpfad oben soll ohne pytest laufen

    if not shutil.which("agent-browser"):
        pytest.skip("agent-browser nicht installiert (Smoke ist Mac-only)")
    rc = main()
    assert not FAILS, "UI-Smoke rot:\n  - " + "\n  - ".join(FAILS)
    assert rc == 0, f"UI-Smoke exit {rc}"


if __name__ == "__main__":
    raise SystemExit(main())
