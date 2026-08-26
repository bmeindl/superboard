"""Tests des Radar-Melders (`radar_watch.py`).

Der Melder haengt an einem Netz-Werkzeug, aber seine gefaehrlichen Entscheidungen sind
rein rechnerisch: WAS ist neu, WAS darf vergessen werden, WAS wird gemeldet. Genau die
sind hier hermetisch geprueft — `dev_radar.run` wird gefaked, es geht nie ein Paket raus.

Die drei Eigenschaften, die schuetzenswert sind:
  1. Erstlauf/Zustandsverlust meldet NICHTS, sondern lernt nur.
  2. Ein Befund ist derselbe, solange sein Ausloeser derselbe ist — Tageszahlen im Text
     duerfen den Fingerprint nicht bewegen.
  3. Ein Netzaussetzer darf den Zustand nicht leerraeumen (sonst meldet morgen alles neu).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import radar_watch  # noqa: E402


def _res(*findings, refs=None, gc_id="aaaaaaaaaaaa", title="Ein Item"):
    """Minimaler dev_radar-Rueckgabewert mit den Feldern, die der Melder liest."""
    return {"items": [{"gc_id": gc_id, "title": title,
                       "refs": refs if refs is not None else [
                           {"url": "https://x/mr/1", "ref": "proj!1",
                            "host": "gl", "resolved_by": "pin"}],
                       "findings": list(findings)}]}


def _f(type_="comment_unanswered", sev="action", text="proj!1: neuer Kommentar",
       url="https://x/mr/1", stamp="2026-08-18T10:00:00"):
    return {"type": type_, "severity": sev, "text": text, "url": url, "stamp": stamp}


def test_fingerprint_haengt_am_ausloeser_nicht_an_der_uhr():
    a = _f(text="proj!1: offen, seit 57d ohne Bewegung — Review haengt")
    b = _f(text="proj!1: offen, seit 58d ohne Bewegung — Review haengt")  # ein Tag spaeter
    assert radar_watch.fingerprint("id", a) == radar_watch.fingerprint("id", b)
    c = _f(stamp="2026-08-19T09:00:00")  # neuer Kommentar = echter Ausloeser
    assert radar_watch.fingerprint("id", a) != radar_watch.fingerprint("id", c)
    assert radar_watch.fingerprint("id1", a) != radar_watch.fingerprint("id2", a)


def test_erstlauf_meldet_nichts(tmp_path, monkeypatch):
    """Fehlt der Zustand, wird gelernt statt geflutet — auch wenn 13 Befunde anliegen."""
    monkeypatch.setattr(radar_watch, "dev_radar",
                        type("M", (), {"run": staticmethod(lambda *a, **k: _res(*[
                            _f(stamp=f"s{i}") for i in range(13)]))}))
    posted = []
    monkeypatch.setattr(radar_watch, "append_turn", lambda *a, **k: posted.append(a) or True)
    state = tmp_path / "radar-state.json"
    out = radar_watch.sweep(Path("board.md"), "Dev", use_agent=False, state_file=state)
    assert out["first"] is True and posted == []
    assert state.is_file() and len(radar_watch.load_state(state)["reported"]) == 13


def test_zweiter_lauf_meldet_nur_das_neue(tmp_path, monkeypatch):
    alt, neu = _f(stamp="alt"), _f(stamp="neu", text="proj!1: zweiter Kommentar")
    seq = [_res(alt), _res(alt, neu)]
    monkeypatch.setattr(radar_watch, "dev_radar",
                        type("M", (), {"run": staticmethod(lambda *a, **k: seq.pop(0))}))
    posted = []
    monkeypatch.setattr(radar_watch, "append_turn",
                        lambda gc_id, text, *a, **k: posted.append(text) or True)
    state = tmp_path / "radar-state.json"
    radar_watch.sweep(Path("b.md"), "Dev", use_agent=False, state_file=state)  # lernt
    out = radar_watch.sweep(Path("b.md"), "Dev", use_agent=False, state_file=state)
    assert out["deltas"] == 1 and len(posted) == 1
    assert "zweiter Kommentar" in posted[0] and posted[0].startswith("📡 Radar · proj!1")


def test_netzaussetzer_raeumt_den_zustand_nicht_leer():
    """Ohne diese Regel meldete ein einziger glab/gh-Ausfall am Folgetag ALLES erneut."""
    old = {"fp1": {"u": "https://x/mr/1"}, "fp2": {"u": "https://x/mr/2"}}
    # Sweep sah nur mr/2 (mr/1 nicht erreichbar) und fand dort nichts mehr:
    keep = radar_watch.prune(old, current={}, seen={"https://x/mr/2"})
    assert "fp1" in keep and "fp2" not in keep


def test_nur_sichere_refs_bekommen_einen_agenten(tmp_path, monkeypatch):
    geraten = [{"url": "https://x/mr/1", "ref": "proj!1", "host": "gl",
                "resolved_by": "hint:item"}]
    seq = [_res(refs=geraten), _res(_f(), refs=geraten)]
    monkeypatch.setattr(radar_watch, "dev_radar",
                        type("M", (), {"run": staticmethod(lambda *a, **k: seq.pop(0))}))
    gerufen = []
    monkeypatch.setattr(radar_watch, "judge", lambda *a, **k: gerufen.append(1) or None)
    monkeypatch.setattr(radar_watch, "append_turn", lambda *a, **k: True)
    state = tmp_path / "radar-state.json"
    radar_watch.sweep(Path("b.md"), "Dev", state_file=state)
    out = radar_watch.sweep(Path("b.md"), "Dev", state_file=state)
    assert gerufen == [] and out["deltas"] == 1  # gemeldet, aber ohne Urteil


def test_fehlgeschlagenes_urteil_schreibt_gar_nichts(tmp_path, monkeypatch):
    seq = [_res(), _res(_f())]
    monkeypatch.setattr(radar_watch, "dev_radar",
                        type("M", (), {"run": staticmethod(lambda *a, **k: seq.pop(0))}))
    monkeypatch.setattr(radar_watch, "judge", lambda *a, **k: None)  # Agent faellt aus
    posted = []
    monkeypatch.setattr(radar_watch, "append_turn", lambda *a, **k: posted.append(a) or True)
    state = tmp_path / "radar-state.json"
    radar_watch.sweep(Path("b.md"), "Dev", state_file=state)
    radar_watch.sweep(Path("b.md"), "Dev", state_file=state)
    assert posted == []  # kein Ersatztext, der wie ein Urteil aussieht


def test_gescheiterter_append_wird_nicht_als_gesehen_gelernt(tmp_path, monkeypatch):
    """Ein Befund, dessen Append fehlschlaegt (Server-Fehler, Timeout), darf nicht als
    `quiet` in den Zustand — sonst ist er fuer immer verschluckt, obwohl er nie gemeldet
    wurde."""
    seq = [_res(), _res(_f()), _res(_f())]
    monkeypatch.setattr(radar_watch, "dev_radar",
                        type("M", (), {"run": staticmethod(lambda *a, **k: seq.pop(0))}))
    monkeypatch.setattr(radar_watch, "judge", lambda *a, **k: {"verdict": "haengt"})
    ok = [False, True]   # erster Append scheitert, zweiter klappt
    versuche = []
    monkeypatch.setattr(radar_watch, "append_turn",
                        lambda *a, **k: versuche.append(a) or ok.pop(0))
    state = tmp_path / "radar-state.json"
    radar_watch.sweep(Path("b.md"), "Dev", state_file=state)      # Erstlauf: nur lernen
    erst = radar_watch.sweep(Path("b.md"), "Dev", state_file=state)
    assert erst["reported"] == [] and len(erst["unreported"]) == 1
    zweit = radar_watch.sweep(Path("b.md"), "Dev", state_file=state)
    assert len(zweit["reported"]) == 1        # derselbe Befund meldet erneut
    assert len(versuche) == 2


def test_agent_fehlschlag_meldet_beim_naechsten_sweep_erneut(tmp_path, monkeypatch):
    """Eigenschaft 3 im Datei-Kopf: „kommt beim naechsten Sweep wieder" — vorher kam er nie."""
    seq = [_res(), _res(_f()), _res(_f())]
    monkeypatch.setattr(radar_watch, "dev_radar",
                        type("M", (), {"run": staticmethod(lambda *a, **k: seq.pop(0))}))
    urteile = [None, {"verdict": "haengt"}]
    monkeypatch.setattr(radar_watch, "judge", lambda *a, **k: urteile.pop(0))
    posted = []
    monkeypatch.setattr(radar_watch, "append_turn", lambda *a, **k: posted.append(a) or True)
    state = tmp_path / "radar-state.json"
    radar_watch.sweep(Path("b.md"), "Dev", state_file=state)
    radar_watch.sweep(Path("b.md"), "Dev", state_file=state)   # Agent faellt aus
    assert posted == []
    radar_watch.sweep(Path("b.md"), "Dev", state_file=state)   # Agent da -> Nachmeldung
    assert len(posted) == 1


def test_turn_bleibt_einzeiler_unter_der_sidecar_grenze():
    d = {"label": "my-org/my-service-monorepo/service-name!343", "type": "x",
         "text": "egal", "url": "https://gitlab.com/x/-/merge_requests/343"}
    text = radar_watch.turn_text(d, {"verdict": "V " * 60, "action": "A " * 60,
                                     "quote": "Q " * 60, "draft": "D " * 200})
    assert "\n" not in text and len(text) <= radar_watch.TURN_MAX
    assert text.startswith("📡 Radar · service-name!343")
