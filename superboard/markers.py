"""Strings, die in `board.md` STEHEN und wieder GELESEN werden — Datenformat, keine UI.

Diese Strings sehen aus wie deutscher Anzeigetext, sind aber Protokoll. Wer sie
umformuliert, übersetzt oder "schöner macht", kappt Verweise in bestehenden Boards:
ein Sidecar-Link, den die Regex nicht mehr trifft, ist ein Faden-Turn, dessen
Volltext niemand mehr findet.

Aufgefallen bei der Vorbereitung der Ausgründung (2026-08-07): der Plan hielt
`@gc:` für das einzige persistierte Format. Falsch — `→ volle Antwort:` wurde an
DREI Stellen unabhängig geparst (`sidecar.py`, `sweep.py`, `index.html`), und
`kompaktiert…` steuert, ob der Runner den vollen oder den gekürzten Kontrakt
schickt. Eine naive Übersetzung der UI hätte beides zerrissen.

**Regel: was hier steht, wird nicht übersetzt und nicht umformuliert.** Neue
Protokoll-Strings gehören hierher, nicht an ihre Verwendungsstelle. Die einzige
zulässige Änderung ist eine, die alte Daten weiter liest (also: Regex erweitern,
nie ersetzen) — siehe REF_RE, das aus genau diesem Grund zwei Labels kennt.

Bewusst NICHT hier: reiner Anzeigetext, der nie zurückgelesen wird (Button-
Beschriftungen, Statuszeilen, Kommentare). Der darf und soll übersetzt werden.
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------- Faden-Turns
# Kind -> Tag, die EINE Quelle der Wahrheit fürs Zurückschreiben eines Faden-Turns.
# Vorher hielt sweep.py eine eigene Kopie dieser Map (`_GC_TAG`) — die kannte "sys" nicht
# und riss ab dem 27.07. jede Nacht den kompletten Sweep mit KeyError ab (nichts archiviert,
# board.md wuchs auf 250 KB). Ein neuer Kind darf nur noch HIER entstehen; wer serialisiert,
# importiert diese Map (sweep.gc_tag fällt zusätzlich generisch zurück statt zu crashen).
GC_TAG = {"ask": "@gc:", "reply": "@gc-re:", "done": "@gc-done:", "sys": "@gc-sys:"}

# ---------------------------------------------------------------- Sidecar-Verweise
# New boards emit English labels. The legacy German labels remain readable forever:
# these strings are persisted in board.md, so changing the writer without widening the
# reader would orphan the externalized turn bodies in existing workspaces.
REF_LABEL = {"ask": "full text", "reply": "full reply", "done": "full text"}

# Typisierte Referenz, NUR am Zeilenende: "… → volle Antwort: inbox/gc-threads/<datei>.md"
REF_RE = re.compile(
    r"→ (?:full reply|full text|volle Antwort|voller Text): "
    r"inbox/gc-threads/([A-Za-z0-9._-]+\.md)\s*$"
)

# Dieselbe Referenz ohne Zeilenende-Anker — für sweep.py, wo "kommt der Pfad überhaupt
# vor?" reicht (Umschreiben auf den Archivpfad).
SIDECAR_REF_RE = re.compile(r"inbox/gc-threads/([A-Za-z0-9._-]+\.md)")

# ---------------------------------------------------------------- Steuersignale
# `@gc-last:` beginnt nach einem Board-Compact mit diesem Präfix; gc_runner prüft es per
# startswith() und schickt dann einmalig den VOLLEN Kontrakt statt der Kurzfassung.
# Steuerfluss, kein Text.
COMPACTED_PREFIX = "kompaktiert"

# Präfix der ersten Antwortzeile, wenn ein Run interaktiven Login braucht. Die Board-UI
# erkennt daran den ⧉-Kopieren-Button für den Handoff-Befehl.
HANDOFF_PREFIX = "🔑 CLI-Handoff nötig:"
