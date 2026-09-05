"""Der Markdown-Renderer des Boards, ausgefuehrt statt gelesen.

WARUM: `mdInline`/`mdBlocks` in `index.html` sind der EINZIGE Renderer aller
Anzeigeflaechen (ARCHITEKTUR.md, Invariante 23) — und sie sind handgeschrieben.
Aufgefallen an Dokumenten mit `_**Lede**_`: der Viewer zeigte rohe Unterstriche,
waehrend gaengige Editoren sie sauber rendern. Ein Test,
der nur den Quelltext greppt, haette das nie gesehen: die Fehler stecken im
Zusammenspiel der Regex-Regeln, nicht in ihrer Anwesenheit.

WIE: Die vier Funktionen werden aus `index.html` geschnitten und unter `node`
ausgefuehrt. Kein Build, keine Abhaengigkeit — faellt `node` aus, wird geskippt.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

HERE = Path(__file__).parent
INDEX = HERE / "index.html"

# Reihenfolge = Abhaengigkeitsreihenfolge im Original.
SLICES = [
    ("function esc(s)", "function mdUrl"),
    ("function mdUrl(u)", "function mdInline"),
    ("function mdInline(s)", "// Repo-relative Markdown-Links"),
    ("function mdCells", "function mdToHtmlDoc"),
]


def _renderer_js() -> str:
    src = INDEX.read_text(encoding="utf-8")
    parts = []
    for start, end in SLICES:
        i = src.index(start)
        parts.append(src[i:src.index(end, i)])
    return "\n".join(parts)


@pytest.fixture(scope="module")
def render():
    if not shutil.which("node"):
        pytest.skip("node nicht verfuegbar — der Renderer laesst sich nicht ausfuehren")
    js = _renderer_js()

    def _render(markdown: str) -> str:
        script = (
            js
            + "\nconst input = JSON.parse(process.argv[1]);"
            + "\nprocess.stdout.write(mdBlocks(input));"
        )
        out = subprocess.run(
            ["node", "-e", script, json.dumps(markdown)],
            capture_output=True, text=True, timeout=30,
        )
        assert out.returncode == 0, out.stderr
        return out.stdout

    return _render


def test_underscore_emphasis(render):
    """`_kursiv_` und `__fett__` — unsere Drafts schreiben die Lede so."""
    assert render("_hello_") == "<p><em>hello</em></p>"
    assert "<strong>hello</strong>" in render("__hello__")


def test_intraword_underscores_stay_text(render):
    """snake_case und Dateinamen sind keine Emphase."""
    out = render("see file_name_here.md and run_the_thing")
    assert "<em>" not in out and "file_name_here.md" in out


def test_emphasis_closes_across_a_soft_line_break(render):
    """Ein `**…**`, das erst zwei Zeilen spaeter schliesst, ist trotzdem fett."""
    out = render("_**Guest-student onboarding took\nshape end to end** — live._")
    assert "_" not in out
    assert "<strong>" in out and "<em>" in out
    assert "<br>" in out, "der harte Umbruch der .md-Datei bleibt sichtbar"


def test_link_targets_are_not_emphasised(render):
    """Die `_`-Regel darf nicht in href/src hineingreifen."""
    out = render("[x](https://a.b/foo_bar_baz.md)")
    assert 'href="https://a.b/foo_bar_baz.md"' in out and "<em>" not in out
    two = render("[a](https://x.y) and [b](https://z.y)")
    assert two.count('target="_blank"') == 2


def test_code_spans_keep_their_underscores(render):
    out = render("`a_b_c` and _x_")
    assert "<code>a_b_c</code>" in out and "<em>x</em>" in out


def test_yaml_front_matter_is_metadata_not_a_rule(render):
    """`---` in Zeile 1 ist Frontmatter, keine Trennlinie."""
    out = render("---\ntitle: x\ntags: [a]\n---\n\n# Head")
    assert out.startswith('<pre class="fm">')
    assert "<hr>" not in out
    assert "<h1>Head</h1>" in out


def test_horizontal_rule_still_works(render):
    """Gegenprobe: mitten im Text bleibt `---` eine Trennlinie."""
    assert "<hr>" in render("a\n\n---\n\nb")


def test_tables_and_lists_survive(render):
    """Regressionsanker fuer die Bloecke, an denen wir NICHT geschraubt haben."""
    assert "<table>" in render("| a | b |\n|---|---|\n| 1 | 2 |")
    assert "<ul>" in render("- one\n- two")
