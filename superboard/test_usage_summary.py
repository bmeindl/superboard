"""Tests für die Sub-Agenten-Kosten-Sicht in `_usage_summary()`.

Pinnt die am 2026-08-07 GEMESSENE Invariante des claude-CLI-Envelopes fest (kontrollierter
Zweimodell-Lauf, claude 2.1.x): Top-Level `usage` zählt nur den Haupt-Agenten, `modelUsage`
zählt Haupt-Agent + Subs. Daran hängt die ganze Kosten-Aufschlüsselung — kippt das Format
still, sollen die Tests laut werden statt dass wieder Monate lang 27 % blind mitlaufen.

Die Zahlen unten sind nicht erfunden: sie stammen 1:1 aus dem Mess-Lauf (Opus-Haupt-Agent,
ein Sonnet-Sub). Nicht „aufräumen".
"""

from __future__ import annotations

import gc_runner as g

# Aus dem Mess-Lauf 2026-08-07 (ein Sonnet-Sub unter einem Opus-Haupt-Agenten).
ENV = {
    "type": "result", "total_cost_usd": 0.25855375, "duration_ms": 42000, "num_turns": 3,
    "usage": {"input_tokens": 4, "output_tokens": 215,
              "cache_read_input_tokens": 29018, "cache_creation_input_tokens": 29336},
    "modelUsage": {
        "claude-opus-5": {"inputTokens": 4, "outputTokens": 215, "cacheReadInputTokens": 29018,
                          "cacheCreationInputTokens": 29336, "costUSD": 0.2033},
        "claude-sonnet-5": {"inputTokens": 2, "outputTokens": 23, "cacheReadInputTokens": 0,
                            "cacheCreationInputTokens": 14653, "costUSD": 0.0553},
    },
}


def test_sub_tokens_sind_die_differenz_und_exakt():
    """Der Sub taucht als Differenz Σ(modelUsage) − Top-Level auf — genau seine Zahlen."""
    u = g._usage_summary(ENV, "claude-opus-5")
    assert u["sub_tokens"] == {"in": 2, "out": 23, "cr": 0, "cw": 14653}


def test_alte_felder_bleiben_haupt_agent_only():
    """Rückwärtskompatibilität: bestehende Felder dürfen sich NICHT verändern, sonst sind
    alte Log-Zeilen nicht mehr mit neuen vergleichbar."""
    u = g._usage_summary(ENV, "claude-opus-5")
    assert (u["input_tokens"], u["output_tokens"], u["cache_read"], u["cache_creation"]) == \
           (4, 215, 29018, 29336)


def test_kosten_pro_modell_summieren_sich_auf_die_gesamtkosten():
    """Die gemessene Invariante: Σ costUSD über modelUsage == total_cost_usd."""
    u = g._usage_summary(ENV, "claude-opus-5")
    assert abs(sum(u["cost_by_model"].values()) - u["cost_usd"]) < 0.01
    assert u["sub_cost_usd_min"] == 0.0553


def test_ohne_hauptmodell_keine_kostenaussage():
    """Kein Hauptmodell bekannt (altes Einzelobjekt-Format) → lieber None als geraten.
    Die Token-Differenz bleibt trotzdem gültig, die braucht das Hauptmodell nicht."""
    u = g._usage_summary(ENV)
    assert u["sub_cost_usd_min"] is None
    assert u["sub_tokens"]["out"] == 23


def test_widerspruechliche_zaehlwege_behaupten_lieber_nichts():
    """Wäre Top-Level größer als Σ(modelUsage) (Formatbruch), wäre die Differenz negativ.
    Dann lieber leer loggen als eine falsche Zahl in die Auswertung schreiben."""
    kaputt = {**ENV, "usage": {**ENV["usage"], "output_tokens": 99999}}
    assert g._usage_summary(kaputt, "claude-opus-5")["sub_tokens"] == {}


def test_einzelmodell_lauf_hat_keine_subkosten():
    """Kein Sub → Differenz null, Untergrenze null. Der häufigste Fall darf nicht rauschen."""
    solo = {"type": "result", "total_cost_usd": 0.2033,
            "usage": ENV["usage"],
            "modelUsage": {"claude-opus-5": ENV["modelUsage"]["claude-opus-5"]}}
    u = g._usage_summary(solo, "claude-opus-5")
    assert u["sub_tokens"] == {"in": 0, "out": 0, "cr": 0, "cw": 0}
    assert u["sub_cost_usd_min"] == 0


def test_envelope_liest_hauptmodell_aus_dem_init_ereignis():
    """Das Hauptmodell steht nur im ersten Ereignis — ohne das ist die Kostenteilung blind."""
    strom = ('{"type":"system","subtype":"init","session_id":"abc","model":"claude-opus-5"}\n'
             '{"type":"result","subtype":"success","session_id":"abc","result":"ok",'
             '"total_cost_usd":0.1,"usage":{},"modelUsage":{}}\n')
    env, sid, haupt = g._envelope(strom)
    assert (sid, haupt) == ("abc", "claude-opus-5")
    assert env is not None and env["type"] == "result"
