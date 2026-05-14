# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from aqt import editor_latex_preview


@dataclass(frozen=True)
class _ExtractedLatex:
    filename: str
    latex_body: str


class _Tr:
    def preferences_latex_generation_disabled(self) -> str:
        return "disabled"


class _Collection:
    tr = _Tr()

    def __init__(self, latex_enabled: bool = True) -> None:
        self.latex_enabled = latex_enabled

    def get_config_bool(self, key: Any) -> bool:
        return self.latex_enabled


def test_render_preview_returns_png_data_url(monkeypatch) -> None:
    extracted = _ExtractedLatex(filename="latex-test.png", latex_body="$x^2$")
    calls: list[dict[str, Any]] = []

    def extract_single_latex(col, kind: str, html_text: str, svg: bool):
        calls.append(
            {
                "kind": kind,
                "html_text": html_text,
                "svg": svg,
            }
        )
        return extracted

    def render_latex_image_bytes(**kwargs) -> bytes:
        calls.append(kwargs)
        return b"png"

    monkeypatch.setattr(
        editor_latex_preview,
        "_extract_single_latex",
        extract_single_latex,
    )
    monkeypatch.setattr(
        editor_latex_preview,
        "_render_latex_image_bytes",
        render_latex_image_bytes,
    )

    result = editor_latex_preview.render_legacy_latex_preview(
        _Collection(),
        {"latexPre": "header", "latexPost": "footer", "latexsvg": False},
        "inline",
        "<b>x^2</b>",
    )

    assert result.ok
    assert result.data_url == "data:image/png;base64,cG5n"
    assert result.alt == "$x^2$"
    assert not result.svg
    assert calls[0] == {
        "kind": "inline",
        "html_text": "<b>x^2</b>",
        "svg": False,
    }
    assert calls[1]["header"] == "header"
    assert calls[1]["footer"] == "footer"
    assert calls[1]["extracted"] == extracted


def test_render_preview_returns_svg_data_url(monkeypatch) -> None:
    monkeypatch.setattr(
        editor_latex_preview,
        "_extract_single_latex",
        lambda col, kind, html_text, svg: _ExtractedLatex(
            filename="latex-test.svg",
            latex_body=r"\begin{displaymath}x\end{displaymath}",
        ),
    )
    monkeypatch.setattr(
        editor_latex_preview,
        "_render_latex_image_bytes",
        lambda **kwargs: b"<svg></svg>",
    )

    result = editor_latex_preview.render_legacy_latex_preview(
        _Collection(),
        {"latexPre": "", "latexPost": "", "latexsvg": True},
        "display",
        "x",
    )

    assert result.ok
    assert result.svg
    assert result.data_url == "data:image/svg+xml;base64,PHN2Zz48L3N2Zz4="


def test_render_preview_respects_disabled_latex_generation(monkeypatch) -> None:
    rendered = False
    monkeypatch.setattr(
        editor_latex_preview,
        "_extract_single_latex",
        lambda col, kind, html_text, svg: _ExtractedLatex(
            filename="latex-test.png",
            latex_body="$x$",
        ),
    )

    def render_latex_image_bytes(**kwargs) -> bytes:
        nonlocal rendered
        rendered = True
        return b"png"

    monkeypatch.setattr(
        editor_latex_preview,
        "_render_latex_image_bytes",
        render_latex_image_bytes,
    )

    result = editor_latex_preview.render_legacy_latex_preview(
        _Collection(latex_enabled=False),
        {"latexPre": "", "latexPost": "", "latexsvg": False},
        "inline",
        "x",
    )

    assert not result.ok
    assert result.error_text == "disabled"
    assert not rendered


def test_render_preview_handles_empty_latex(monkeypatch) -> None:
    monkeypatch.setattr(
        editor_latex_preview,
        "_extract_single_latex",
        lambda col, kind, html_text, svg: None,
    )

    result = editor_latex_preview.render_legacy_latex_preview(
        _Collection(),
        {"latexPre": "", "latexPost": "", "latexsvg": False},
        "inline",
        "",
    )

    assert not result.ok
    assert result.error_text == "No LaTeX to preview"
