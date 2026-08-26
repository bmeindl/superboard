"""Instanz-Konfiguration: was an DIESER Installation hängt, nicht an der Mechanik.

Die Trennlinie für die spätere Ausgründung: der Code kennt Board-Mechanik (Parser,
Faden, Runner), die Config kennt die Instanz (wer der Nutzer ist, wo die Dateien
liegen). Fehlt die Config-Datei, laufen generische Defaults — das Board startet
also auch auf einer fremden Maschine ohne Vorbereitung.

Bewusst klein gehalten. Alles, was hier landet, ist eine Indirektion, die künftige
Ad-hoc-Änderungen teurer macht — und Ad-hoc-Änderbarkeit ist die Eigenschaft, die
dieses Board wertvoll macht. Faustregel: nur rein, was sich zwischen zwei
Installationen wirklich unterscheidet. Cockpit-Zonen, Spaltennamen, Schwellenwerte
gehören NICHT hierher, solange es einen Nutzer gibt — die ändert man im Code.

Quelle ist `board.config.json` im aktiven Workspace; einzelne Werte lassen sich
per Env übersteuern (`GC_OWNER`), was Tests und Wegwerf-Instanzen billig macht.
"""

from __future__ import annotations

import json
import os

import paths as _p

CONFIG_PATH = _p.CONFIG

# Generische Defaults = das Verhalten ohne jede Config-Datei.
_DEFAULTS: dict = {
    "owner": {"name": "You", "agent": "S-Agent"},
    # A personal night-rest ladder must never surprise a new workspace with a
    # blocking overlay; installations that want it opt in explicitly.
    "night_pause": {"enabled": False},
    # View-only personalization. Unknown/new topics remain visible because the
    # browser hides only these explicit names.
    "off_duty": {"hidden_topics": [], "visible_topics": []},
    # Wer bin "ich" auf GitLab/GitHub — der Dev-Radar entscheidet daran, ob ein MR auf
    # mich wartet oder auf jemand anderen. Leer = niemand ist "ich" (Radar zeigt dann
    # alles als fremd an, statt zu raten).
    "identities": {"gitlab": [], "github": []},
}


def _load() -> dict:
    """Config lesen, flach über die Defaults legen. Fehler sind nie fatal:
    ein kaputtes JSON darf das Board nicht am Starten hindern, es soll nur
    generisch laufen."""
    data = {k: dict(v) if isinstance(v, dict) else v for k, v in _DEFAULTS.items()}
    try:
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return data
    if not isinstance(raw, dict):
        return data
    for key, val in raw.items():
        if key.startswith("_"):          # "_comment"-Felder sind Doku, keine Werte
            continue
        if isinstance(val, dict) and isinstance(data.get(key), dict):
            data[key].update(val)
        else:
            data[key] = val
    return data


_CFG = _load()

# Wie der Mensch im Faden heißt — Rollen-Label in jedem Agent-Prompt und in den
# Sidecar-Kopfzeilen. KEIN Parse-Ziel: der String wird zur Laufzeit gerendert und
# nirgends wieder gelesen (verifiziert 2026-08-07), Umbenennen kostet also keine
# Migration an bestehenden Boards.
OWNER = os.environ.get("GC_OWNER", "").strip() or _CFG["owner"]["name"]
AGENT = _CFG["owner"].get("agent") or "Agent"

# {"gitlab": [...], "github": [...]} — Konten, die als "ich" zählen (dev_radar).
IDENTITIES: dict = _CFG["identities"]

# Whole late-night behavior: footer pill, reminder ladder and mandatory pause.
# Keep strict bool semantics and preserve the module's fail-soft config contract.
_NIGHT_PAUSE = _CFG.get("night_pause")
NIGHT_PAUSE_ENABLED = (
    isinstance(_NIGHT_PAUSE, dict) and _NIGHT_PAUSE.get("enabled") is True
)


def _topic_names(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [name.strip() for name in value if isinstance(name, str) and name.strip()]


_OFF_DUTY = _CFG.get("off_duty") if isinstance(_CFG.get("off_duty"), dict) else {}
OFF_DUTY_HIDDEN_TOPICS = _topic_names(_OFF_DUTY.get("hidden_topics"))
OFF_DUTY_VISIBLE_TOPICS = _topic_names(_OFF_DUTY.get("visible_topics"))
