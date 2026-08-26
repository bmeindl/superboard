"""Run-Receipt: maschinelles Fakten-Protokoll pro Board-Run.

Warum (21./22.07., Item a99928929814): im Faden steht, was der Agent *behauptet*.
Was er *getan* hat — welche Dateien sich geändert haben, was der Permission-Classifier
geblockt hat, woran ein Lauf gestorben ist — stand nirgends. Das hier schreibt der
RUNNER, nicht der Agent: der Agent kann es weder schönen noch vergessen.

Bens Bedingungen an dieses Feature, wörtlich vom 21.07. — und wie sie eingelöst sind:

* „müsste komplett ein separates Modul sein"  → dieses Modul. Es importiert NICHTS aus
  gc_runner/server; der Kern erreicht es nur lazy über ``receipt_hook.py``.
* „beeinflusst den Agenten in keiner Weise"   → kein Prompt-Text, keine Kontrakt-Regel,
  kein Board-Schreibzugriff. Der Agent erfährt nicht, dass es existiert.
* „wenn es da ist, ist es da, wenn nicht, nicht" → ``receipt_hook.py`` degradiert eine
  fehlende oder kaputte Implementierung auf No-op. Ein Receipt darf NIE einen Run kosten;
  die UI zeigt den Menüeintrag nur, wenn eine Datei existiert.
* „vielleicht machen wir es und dann löschen wir es wieder" → Rückbau = ENABLED=False,
  oder: diese Datei + `inbox/gc-receipts/` löschen. Adapter, Prompt und Runner bleiben
  ohne weitere Änderung lauffähig.

Bewusst NICHT drin (Blatt 22.07. Q2=A): Testausführung/Checks. Das ist das eigene Ticket
„Testing-Nachweis AI-first" (faee694e916e) — der Owner wollte dort ausdrücklich einen billigen
Post-Reviewer statt starrer Pfad→Check-Mappings („einmal irgendwas falsch und dann hakt
das nicht ab. Das ist alles Fehlerpotenzial").
"""

from __future__ import annotations

import json
import time
import uuid
from datetime import datetime
from pathlib import Path

import git_state as _git_state
import paths as _p

GC_ROOT = _p.GC_ROOT
RECEIPT_DIR = _p.RECEIPTS

# Ein Schalter, ein Rückbau. False = es entstehen keine neuen Dateien, sonst ändert sich nichts.
ENABLED = True

# Retention wie beim Prompt-Mitschnitt: das Receipt von Lauf 17 eines Items interessiert
# niemanden mehr. Die Dateien sind gitignored (Run-Telemetrie, kein Wissen) — sonst
# rauschten ~30 neue Dateien pro Tag durch jeden Commit.
KEEP_PER_ITEM = 5

# Obergrenze für gelistete Dateien/Aktionen. Großzügig: die alte 6 war Bens Beschwerde
# am 23.07. („nicht alle angefassten Dateien drin, nur die ersten paar"). Ein Cap bleibt
# trotzdem — ein Massen-Rename darf das Receipt nicht in eine Textwand verwandeln.
LIST_MAX = 40


def _git(*args: str) -> str:
    """Kompatibilitäts-Wrapper; die Kernimplementierung lebt in git_state.py."""
    _git_state.GC_ROOT = GC_ROOT
    return _git_state._git(*args)


def git_head() -> str:
    """SHA vor dem Lauf."""
    _git_state.GC_ROOT = GC_ROOT
    return _git_state.git_head()


def _porcelain() -> list[str]:
    _git_state.GC_ROOT = GC_ROOT
    return _git_state._porcelain()


def git_facts(status_max: int = 15, commits: int = 5) -> dict:
    """Der Arbeitsbaum in Kurzform — Rohstoff für den Git-Block im Board-Prompt.

    Bewusst gekappt: in dieses Repo schreiben mehrere Board-Sessions parallel, `git status`
    hat regelmäßig 60+ Zeilen, fast alle fremd (dieselbe Unschärfe wie in git_delta). Der
    Rest wird im Prompt als „… +N weitere" ausgewiesen statt still zu verschwinden.
    """
    _git_state.GC_ROOT = GC_ROOT
    return _git_state.git_facts(status_max, commits)


def snapshot() -> dict:
    """Zustand VOR dem Lauf: SHA *und* die bereits offenen Dateien.

    Warum auch die dirty-Liste (23.07.): vorher maß das Receipt `git status` nur
    NACH dem Lauf. In einem Repo, in das mehrere Board-Sessions parallel schreiben,
    sind das 70+ Dateien — fast alle fremd. Die Liste war deshalb gleichzeitig zu lang
    (→ auf 6 abgeschnitten) und zu unspezifisch (→ die 6 waren meist nicht mal die
    Dateien dieses Runs). Mit dem Vorher-Stand lässt sich der Anteil DIESES Runs
    herausrechnen und dafür vollständig zeigen.
    """
    _git_state.GC_ROOT = GC_ROOT
    return _git_state.snapshot()


def git_delta(before: str | dict) -> dict:
    """Was ist seit `before` im Arbeitsbaum passiert?

    `before` ist ein snapshot()-Dict (neu) oder ein blanker SHA-String (alt: dann fehlt
    die Vorher/Nachher-Zuordnung der offenen Dateien und es bleibt bei der Rohliste).

    Ehrlichkeitsgrenze, die im Receipt auch so dasteht: dieses Repo wird von mehreren
    Sessions gleichzeitig beschrieben. Zwischen zwei SHAs können Commits FREMDER Läufe
    liegen. Deshalb steht im Receipt die Commit-Zeile mit Zeitstempel und nicht die
    Behauptung „das hat dieser Run getan".
    """
    _git_state.GC_ROOT = GC_ROOT
    return _git_state.git_delta(before)


def _dirty_path(line: str) -> str:
    """`git status --porcelain`-Zeile → Pfad. Robust gegen die gestrippte erste Zeile."""
    return _git_state._dirty_path(line)


def _fmt_duration(ms: float | int | None) -> str:
    if not ms:
        return "—"
    s = int(ms / 1000)
    return f"{s // 60}m {s % 60}s" if s >= 60 else f"{s}s"


def _fmt_facts(gc_id: str, title: str, out: dict, delta: dict, started: float) -> str:
    """Fakten → Markdown. Kompakt (Bens Sorge am 21.07.: „das ist dann so viel Daten")."""
    u = out.get("usage_summary") or {}
    ok = out.get("ok")
    lines = [f"# Run receipt — {title or '(untitled)'}", "",
             f"*`{gc_id}` · {datetime.fromtimestamp(started).strftime('%Y-%m-%d %H:%M')} · "
             f"duration {_fmt_duration(u.get('duration_ms'))}*", ""]

    # `raw_error` ist bei einem Fehler-Envelope nur die Diagnose-Hülle
    # („is_error=True subtype=success") — der GRUND steht im `result` („You've hit your
    # session limit"). Ohne ihn liest sich ein Rate-Limit im Receipt wie ein Absturz, und
    # die Fehler-Retro rätselt später am falschen Ende (gemessen 06.08., 3. Lauf).
    reason = " · ".join(t for t in (str(out.get("raw_error") or "").strip(),
                                   " ".join(str(out.get("reply") or "").split())[:120]) if t)
    result = "ok" if ok else f"❌ aborted ({reason or 'unknown'})"
    lines.append(f"- **Result:** {result}")

    effort = []
    if isinstance(u.get("cost_usd"), (int, float)):
        effort.append(f"${u['cost_usd']:.2f}")
    if out.get("context_tokens"):
        effort.append(f"~{round(out['context_tokens'] / 1000)}k context")
    if u.get("num_turns"):
        effort.append(f"{u['num_turns']} turns")
    if u.get("cache_hit_pct") is not None:
        effort.append(f"cache {u['cache_hit_pct']}%")
    if u.get("models"):
        effort.append(", ".join(u["models"]))
    if effort:
        lines.append(f"- **Effort:** {' · '.join(effort)}")

    context = out.get("thread_context") or {}
    if context.get("enabled"):
        selected = context.get("selected") or []
        prompt_state = "in prompt" if context.get("in_prompt") else "no prompt match"
        elapsed = int(context.get("ms", 0) or 0) + int(context.get("rerank_ms", 0) or 0)
        detail = (f"{len(selected)}/{context.get('candidates', 0)} {prompt_state} · "
                  f"{context.get('backend', 'unknown')} · {elapsed} ms")
        if context.get("error"):
            detail += f" · fallback: {str(context['error'])[:180]}"
        lines.append(f"- **Earlier-thread context:** {detail}")

    commits = delta.get("commits") or []
    if commits:
        lines.append(f"- **Commits since run start:** {len(commits)}")
        lines += [f"  - `{c}`" for c in commits[:10]]
        if len(commits) > 10:
            lines.append(f"  - … {len(commits) - 10} more")
    else:
        lines.append("- **Commits since run start:** none")

    if delta.get("diffstat"):
        # lstrip je Zeile: _git() strippt die Gesamtausgabe, wodurch NUR die erste
        # Diffstat-Zeile ihr führendes Leerzeichen verliert und die Spalte ausfranst.
        # Die |-Ausrichtung steckt in der Auffüllung NACH dem Dateinamen, bleibt also heil.
        lines += ["- **Changed (committed):**", "", "  ```",
                  *(f"  {ln.lstrip()}" for ln in delta["diffstat"].splitlines()), "  ```"]

    # NICHT d[3:]: _git() strippt die Gesamtausgabe, die erste porcelain-Zeile
    # verliert dabei ihr führendes Leerzeichen (" M pfad" → "M pfad") und ein
    # fixer Offset schnitte ihr den ersten Buchstaben ab ("nbox/board.md").
    dirty, new = delta.get("dirty") or [], delta.get("dirty_new")
    if new is not None:
        # Vorher/Nachher bekannt → nur der Anteil DIESES Runs wird gelistet, dafür ganz.
        if new:
            lines.append(f"- **Uncommitted, new since run start:** {len(new)} file(s)")
            lines += [f"  - `{p}`" for p in new[:LIST_MAX]]
            if len(new) > LIST_MAX:
                lines.append(f"  - … {len(new) - LIST_MAX} more")
        else:
            lines.append("- **Uncommitted, new since run start:** none")
        if delta.get("dirty_pre"):
            lines.append(f"- **Already open before the run:** {delta['dirty_pre']} file(s) "
                         "— pre-existing state or parallel sessions, intentionally not listed")
    elif dirty:
        # Alt-Pfad (Receipt aus einem SHA-String): keine Zuordnung möglich, Rohliste.
        lines.append(f"- **Uncommitted:** {len(dirty)} file(s) — "
                     + ", ".join(_dirty_path(d) for d in dirty[:LIST_MAX])
                     + (f" … {len(dirty) - LIST_MAX} more" if len(dirty) > LIST_MAX else ""))

    denials = out.get("denials") or []
    if denials:
        # Im Faden steht heute nur die ANZAHL. Hier die Details — das ist der Punkt,
        # an dem man sieht, ob eine Regel nervt oder ob der Agent etwas versucht hat,
        # das er nicht sollte.
        lines.append(f"- **Blocked:** {len(denials)} action(s)")
        for d in denials[:LIST_MAX]:
            name = d.get("tool_name") or d.get("tool") or "?" if isinstance(d, dict) else str(d)
            arg = ""
            if isinstance(d, dict):
                ti = d.get("tool_input") or {}
                arg = str(ti.get("command") or ti.get("file_path") or "")[:120]
            lines.append(f"  - `{name}`{f' — {arg}' if arg else ''}")
        if len(denials) > LIST_MAX:  # vorher still abgeschnitten — ein stiller Cut ist im
            lines.append(f"  - … {len(denials) - LIST_MAX} more")  # Fakten-Protokoll ein Bug

    if out.get("session_id"):
        lines.append(f"- **Session:** `{out['session_id']}`")

    lines += ["", "---", "",
              "*Measured by the runner, not reported by the agent. Limitation: multiple "
              "board sessions write to this repository in parallel, so commits and "
              "uncommitted files may come from another concurrent run.*"]
    return "\n".join(lines) + "\n"


def write(gc_id: str, title: str, out: dict, git_before: str, started: float,
          receipt_dir: Path | None = None) -> Path | None:
    """Schreibt das Receipt. Gibt den Pfad zurück oder None — und wirft NIE."""
    if not ENABLED:
        return None
    try:
        d = receipt_dir or RECEIPT_DIR
        d.mkdir(parents=True, exist_ok=True)
        # Zufallssuffix wie beim Journal/Prompt-Mitschnitt: zwei Läufe DESSELBEN Items in
        # derselben Sekunde (Run-All, Auto-Retrigger) trafen sonst denselben Dateinamen und
        # das zweite Receipt überschrieb das erste still. Im Test aufgefallen, nicht gedacht.
        path = (d / f"{gc_id}-{datetime.fromtimestamp(started).strftime('%Y%m%d-%H%M%S')}"
                    f"-{uuid.uuid4().hex[:4]}.md")
        path.write_text(_fmt_facts(gc_id, title, out, git_delta(git_before), started),
                        encoding="utf-8")
        for old in receipt_files(d, gc_id)[:-KEEP_PER_ITEM]:
            old.unlink(missing_ok=True)
        return path
    except Exception:  # noqa: BLE001 — ein kaputtes Receipt darf keinen Run kosten
        return None


def receipt_files(receipt_dir: Path, gc_id: str) -> list[Path]:
    """Receipts eines Items, ÄLTESTES zuerst. Nach mtime sortiert, nicht nach Name —
    dieselbe Falle wie bei den Prompt-Mitschnitten (gc_runner.prompt_files): zwei Runs
    in derselben Sekunde ordnen sonst willkürlich, und die Retention löscht den neuesten."""
    if not receipt_dir.is_dir():
        return []
    return sorted(receipt_dir.glob(f"{gc_id}-*.md"),
                  key=lambda p: (p.stat().st_mtime, p.name))


if __name__ == "__main__":  # Handprobe: python3 receipt.py → Receipt aus Dummy-Fakten
    demo = {"ok": True, "denials": [], "context_tokens": 42000, "session_id": "demo",
            "usage_summary": {"cost_usd": 1.23, "duration_ms": 95000, "num_turns": 7,
                              "cache_hit_pct": 91, "models": ["claude-opus-4-8"]}}
    print(_fmt_facts("0" * 12, "Manual check", demo, git_delta(snapshot()), time.time()))
    print(json.dumps({"enabled": ENABLED, "dir": str(RECEIPT_DIR)}, indent=1))
