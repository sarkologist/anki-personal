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
        self.media = _Media()

    def get_config_bool(self, key: Any) -> bool:
        return self.latex_enabled


class _Media:
    def __init__(self) -> None:
        self.files: set[str] = set()
        self.writes: dict[str, bytes] = {}

    def have(self, filename: str) -> bool:
        return filename in self.files or filename in self.writes

    def write_data(self, filename: str, data: bytes) -> None:
        self.writes[filename] = data


def test_render_preview_reuses_existing_collection_media(monkeypatch) -> None:
    extracted = _ExtractedLatex(filename="latex-test.png", latex_body="$x^2$")
    col = _Collection()
    col.media.files.add(extracted.filename)

    monkeypatch.setattr(
        editor_latex_preview,
        "_extract_single_latex",
        lambda col, kind, html_text, svg: extracted,
    )

    def fail_render(**kwargs) -> bytes:
        raise AssertionError("should not render cached media")

    monkeypatch.setattr(
        editor_latex_preview,
        "_render_latex_image_bytes",
        fail_render,
    )
    monkeypatch.setattr(
        editor_latex_preview,
        "_save_latex_image_to_media",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("should not write cached media")
        ),
    )

    result = editor_latex_preview.render_legacy_latex_preview(
        col,
        {"latexPre": "header", "latexPost": "footer", "latexsvg": False},
        "inline",
        "<b>x^2</b>",
    )

    assert result.ok
    assert result.src == "latex-test.png"
    assert result.alt == "$x^2$"


def test_render_preview_returns_transient_png_data_url(monkeypatch) -> None:
    extracted = _ExtractedLatex(filename="latex-test.png", latex_body="$x^2$")
    col = _Collection()
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
        col,
        {"latexPre": "header", "latexPost": "footer", "latexsvg": False},
        "inline",
        "<b>x^2</b>",
    )

    assert result.ok
    assert result.src == "data:image/png;base64,cG5n"
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
    assert col.media.writes == {}


def test_render_preview_writes_saved_latex_to_collection_media(monkeypatch) -> None:
    extracted = _ExtractedLatex(filename="latex-test.png", latex_body="$x$")
    col = _Collection()
    save_calls = 0

    monkeypatch.setattr(
        editor_latex_preview,
        "_extract_single_latex",
        lambda col, kind, html_text, svg: extracted,
    )
    monkeypatch.setattr(
        editor_latex_preview,
        "_saved_fields_contain_latex",
        lambda col, saved_fields, filename, svg: bool(saved_fields)
        and filename == extracted.filename,
    )

    def save_latex_image_to_media(**kwargs) -> None:
        nonlocal save_calls
        save_calls += 1
        kwargs["col"].media.write_data(kwargs["extracted"].filename, b"png")

    monkeypatch.setattr(
        editor_latex_preview,
        "_save_latex_image_to_media",
        save_latex_image_to_media,
    )

    first = editor_latex_preview.render_legacy_latex_preview(
        col,
        {"latexPre": "", "latexPost": "", "latexsvg": False},
        "inline",
        "x",
        ("[$]x[/$]",),
    )
    second = editor_latex_preview.render_legacy_latex_preview(
        col,
        {"latexPre": "", "latexPost": "", "latexsvg": False},
        "inline",
        "x",
        ("[$]x[/$]",),
    )

    assert first.ok
    assert second.ok
    assert first.src == second.src == "latex-test.png"
    assert col.media.writes == {"latex-test.png": b"png"}
    assert save_calls == 1


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
    assert result.src == "data:image/svg+xml;base64,PHN2Zz48L3N2Zz4="


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
