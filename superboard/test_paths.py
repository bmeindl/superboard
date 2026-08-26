"""Deckt die Lücke ab, die der Pfad-Refactor geschlossen hat.

Vorher leiteten neun Nicht-Test-Stellen den Repo-Root unabhängig her, und KEIN Test
prüfte, ob sie übereinstimmen. Eine falsche Tiefe wäre lautlos durchgegangen —
Sidecars und Receipts landen woanders, ohne dass irgendwas kracht.

Zweiter Zweck: festhalten, dass die Env-Overrides greifen. Sie sind der Hebel, mit
dem Tests künftig von den ECHTEN Daten weggelenkt werden (heute schreiben Teile der
Suite noch ins produktive inbox/gc-threads/).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _in_subprocess(code: str, env: dict[str, str] | None = None) -> str:
    """Modul-Konstanten werden beim Import ausgewertet — Env-Varianten müssen
    deshalb in einem frischen Prozess laufen, nicht per monkeypatch."""
    e = dict(os.environ)
    e.update(env or {})
    out = subprocess.run([sys.executable, "-c", code], cwd=HERE, env=e,
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return out.stdout.strip()


def test_alle_module_sehen_denselben_root() -> None:
    got = _in_subprocess(
        "import json, server, gc_runner, sidecar, receipt, retro_scan\n"
        "print(json.dumps([str(server.GC_ROOT), str(gc_runner.GC_ROOT), str(sidecar.GC_ROOT),"
        " str(receipt.GC_ROOT), str(retro_scan.ROOT)]))")
    roots = json.loads(got)
    assert len(set(roots)) == 1, f"Root driftet auseinander: {roots}"


def test_alle_module_sehen_dasselbe_board_und_dieselben_faeden() -> None:
    got = _in_subprocess(
        "import json, server, sweep, board_lint, retro_scan, dev_radar, migrate_diet,"
        " sidecar, gc_runner\n"
        "print(json.dumps({'board': [str(server.DEFAULT_BOARD), str(sweep.BOARD),"
        " str(board_lint.DEFAULT_BOARD), str(retro_scan.BOARD), str(dev_radar.DEFAULT_BOARD),"
        " str(migrate_diet.BOARD)], 'threads': [str(sidecar.SIDECAR_DIR),"
        " str(gc_runner.SIDECAR_DIR), str(sweep.SIDECAR_DIR)]}))")
    d = json.loads(got)
    assert len(set(d["board"])) == 1, f"Board-Pfad driftet: {d['board']}"
    assert len(set(d["threads"])) == 1, f"Sidecar-Ordner driftet: {d['threads']}"


def test_default_ist_das_arbeitsverzeichnis() -> None:
    """OSS-Kontrakt: ohne Env ist der Workspace das Arbeitsverzeichnis des
    Prozesses — `superboard` bedient das Board des Ordners, in dem es startet.
    (_in_subprocess läuft mit cwd=HERE, also muss genau HERE herauskommen.)"""
    got = _in_subprocess("import paths; print(paths.GC_ROOT)")
    assert Path(got) == HERE
    got = _in_subprocess("import paths; print(paths.BOARD)")
    assert Path(got) == HERE / "inbox" / "board.md"


def test_env_lenkt_root_und_daten_um(tmp_path: Path) -> None:
    """Der eigentliche Gewinn: Tests und Wegwerf-Instanzen kommen von den echten
    Daten weg, ohne dass irgendwo ein Pfad hartkodiert bleibt."""
    root, data = tmp_path / "repo", tmp_path / "daten"
    got = _in_subprocess(
        "import json, paths, gc_runner, sidecar\n"
        "print(json.dumps([str(paths.GC_ROOT), str(paths.BOARD), str(sidecar.SIDECAR_DIR),"
        " str(gc_runner.JOURNAL_DIR), str(gc_runner.USAGE_LOG)]))",
        {"GC_ROOT": str(root), "GC_DATA": str(data)})
    gc_root, board, threads, journal, usage = (Path(p) for p in json.loads(got))
    assert gc_root == root.resolve()
    assert board == root.resolve() / "inbox" / "board.md"
    assert threads == root.resolve() / "inbox" / "gc-threads"
    assert journal == data.resolve() / "journal"
    assert usage == data.resolve() / "usage-log.jsonl"


def test_instance_files_live_in_the_workspace(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    got = _in_subprocess(
        "import json, paths, server, config, contract\n"
        "print(json.dumps([str(paths.ACTIONS), str(server.ACTIONS_FILE),"
        " str(paths.RITUALS), str(server.RITUALE_FILE), str(config.CONFIG_PATH),"
        " str(contract.INSTANCE_CONTRACT_PATH)]))",
        {"GC_ROOT": str(root)},
    )
    actions, server_actions, rituals, server_rituals, config, contract = (
        Path(path) for path in json.loads(got)
    )
    expected = root.resolve()
    assert actions == server_actions == expected / "actions.json"
    assert rituals == server_rituals == expected / "rituals.json"
    assert config == expected / "board.config.json"
    assert contract == expected / "board.contract.md"


def test_leere_env_zaehlt_als_nicht_gesetzt() -> None:
    got = _in_subprocess("import paths; print(paths.GC_ROOT)", {"GC_ROOT": "  "})
    assert Path(got) == HERE


# ------------------------------------------------------------------ Instanz-Config

def test_owner_kommt_aus_der_config(tmp_path: Path) -> None:
    cfg = tmp_path / "board.config.json"
    cfg.write_text(json.dumps({"owner": {"name": "Ada"}}), encoding="utf-8")
    got = _in_subprocess(
        f"import pathlib, config\nconfig.CONFIG_PATH = pathlib.Path({str(cfg)!r})\n"
        "print(config._load()['owner']['name'])")
    assert got == "Ada"


def test_owner_per_env_uebersteuerbar() -> None:
    assert _in_subprocess("import config; print(config.OWNER)", {"GC_OWNER": "Ada"}) == "Ada"


def test_ohne_config_generischer_default(tmp_path: Path) -> None:
    """Fremde Maschine, keine board.config.json: das Board muss trotzdem starten
    und einen neutralen Namen benutzen — das ist die Zusage fürs spätere OSS-Repo."""
    got = _in_subprocess(
        "import pathlib, config\n"
        "config.CONFIG_PATH = pathlib.Path('/nicht/vorhanden.json')\n"
        "c = config._load(); print(c['owner']['name'])")
    assert got == "You"


def test_kaputte_config_ist_nicht_fatal(tmp_path: Path) -> None:
    bad = tmp_path / "board.config.json"
    bad.write_text("{ das ist kein json", encoding="utf-8")
    got = _in_subprocess(
        f"import pathlib, config\nconfig.CONFIG_PATH = pathlib.Path({str(bad)!r})\n"
        "print(config._load()['owner']['name'])")
    assert got == "You"


def test_config_top_level_must_be_an_object_but_is_never_fatal(tmp_path: Path) -> None:
    bad = tmp_path / "board.config.json"
    for value in (True, [], "oops", None):
        bad.write_text(json.dumps(value), encoding="utf-8")
        got = _in_subprocess(
            f"import pathlib, config\nconfig.CONFIG_PATH = pathlib.Path({str(bad)!r})\n"
            "print(config._load()['owner']['name'])"
        )
        assert got == "You"


def test_night_pause_is_off_without_explicit_workspace_opt_in(tmp_path: Path) -> None:
    got = _in_subprocess(
        "import config; print(config.NIGHT_PAUSE_ENABLED)",
        {"GC_ROOT": str(tmp_path)},
    )
    assert got == "False"


def test_night_pause_requires_a_literal_true(tmp_path: Path) -> None:
    config_path = tmp_path / "board.config.json"
    cases = (
        ({"enabled": True}, "True"),
        ({"enabled": False}, "False"),
        ({"enabled": "true"}, "False"),
        (True, "False"),
        (None, "False"),
    )
    for value, expected in cases:
        config_path.write_text(json.dumps({"night_pause": value}), encoding="utf-8")
        got = _in_subprocess(
            "import config; print(config.NIGHT_PAUSE_ENABLED)",
            {"GC_ROOT": str(tmp_path)},
        )
        assert got == expected


def test_owner_ist_kein_parse_ziel() -> None:
    """Die Annahme, auf der Phase 2 steht: der Rollenname wird nur gerendert, nie
    gelesen. Bricht das, braucht ein Umbenennen plötzlich eine Datenmigration."""
    quellen = [(HERE / n).read_text(encoding="utf-8")
               for n in ("server.py", "gc_runner.py", "sidecar.py", "sweep.py", "board_lint.py")]
    for text in quellen:
        for zeile in text.splitlines():
            if "re.compile" in zeile or "startswith(" in zeile:
                assert "Ada" not in zeile, f"Rollenname in einem Parse-Ausdruck: {zeile.strip()}"


# ------------------------------------------------------------------ Freeze-Liste

def test_marker_sind_unveraendert() -> None:
    """markers.py ist Datenformat. Ändert sich hier etwas, sind bestehende
    board.md-Zeilen betroffen — dieser Test ist die Bremse davor."""
    import markers
    assert markers.GC_TAG == {"ask": "@gc:", "reply": "@gc-re:",
                              "done": "@gc-done:", "sys": "@gc-sys:"}
    assert markers.COMPACTED_PREFIX == "kompaktiert"
    assert markers.HANDOFF_PREFIX == "🔑 CLI-Handoff nötig:"
    # New English labels and both legacy German labels remain readable.
    for label in ("full reply", "full text", "volle Antwort", "voller Text"):
        zeile = f"Kurzfassung … → {label}: inbox/gc-threads/abc123-20260807-101112-9f9f.md"
        assert markers.REF_RE.search(zeile), label
        assert markers.SIDECAR_REF_RE.search(zeile), label


def test_nur_eine_definition_der_marker() -> None:
    """Vorher lagen dieselben Strings in drei Dateien. Wer sie erneut lokal
    definiert, hebelt die Freeze-Liste aus."""
    for name in ("sidecar.py", "sweep.py", "server.py"):
        text = (HERE / name).read_text(encoding="utf-8")
        assert 'REF_RE = re.compile' not in text, f"{name} hält wieder eine eigene Kopie"
        assert 'GC_TAG = {' not in text, f"{name} hält wieder eine eigene GC_TAG-Kopie"
