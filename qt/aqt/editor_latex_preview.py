# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from __future__ import annotations

import base64
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from anki.config import Config
from anki.models import NotetypeDict
from anki.utils import call

if TYPE_CHECKING:
    from anki.collection import Collection
    from anki.latex import ExtractedLatex


@dataclass(frozen=True)
class LegacyLatexPreviewResult:
    src: str | None = None
    alt: str = ""
    svg: bool = False
    error_text: str = ""

    @property
    def ok(self) -> bool:
        return self.src is not None


def render_legacy_latex_preview(
    col: "Collection",
    notetype: NotetypeDict | dict[str, Any],
    kind: str,
    html_text: str,
    saved_fields: tuple[str, ...] = (),
) -> LegacyLatexPreviewResult:
    svg = bool(notetype.get("latexsvg", False))
    extracted = _extract_single_latex(col, kind, html_text, svg)

    if not extracted:
        return LegacyLatexPreviewResult(error_text="No LaTeX to preview")

    if col.media.have(extracted.filename):
        return LegacyLatexPreviewResult(
            src=extracted.filename,
            alt=extracted.latex_body,
            svg=svg,
        )

    if not col.get_config_bool(Config.Bool.RENDER_LATEX):
        return LegacyLatexPreviewResult(
            error_text=str(col.tr.preferences_latex_generation_disabled())
        )

    if _saved_fields_contain_latex(col, saved_fields, extracted.filename, svg):
        error = _save_latex_image_to_media(
            col=col,
            extracted=extracted,
            header=str(notetype.get("latexPre", "")),
            footer=str(notetype.get("latexPost", "")),
            svg=svg,
        )
        if error:
            return LegacyLatexPreviewResult(error_text=error)

        return LegacyLatexPreviewResult(
            src=extracted.filename,
            alt=extracted.latex_body,
            svg=svg,
        )

    try:
        image = _render_latex_image_bytes(
            col=col,
            extracted=extracted,
            header=str(notetype.get("latexPre", "")),
            footer=str(notetype.get("latexPost", "")),
            svg=svg,
        )
    except LatexPreviewError as exc:
        return LegacyLatexPreviewResult(error_text=exc.error_text)

    return LegacyLatexPreviewResult(
        src=_data_url(image, svg),
        alt=extracted.latex_body,
        svg=svg,
    )


def _extract_single_latex(
    col: "Collection",
    kind: str,
    html_text: str,
    svg: bool,
) -> "ExtractedLatex | None":
    wrapped = _wrap_legacy_latex(kind, html_text)
    output = _extract_latex(col, wrapped, svg)

    return output.latex[0] if output.latex else None


def _wrap_legacy_latex(kind: str, html_text: str) -> str:
    if kind == "display":
        return f"[$$]{html_text}[/$$]"

    return f"[$]{html_text}[/$]"


def _extract_latex(col: "Collection", text: str, svg: bool):
    from anki.latex import ExtractedLatexOutput

    proto = col._backend.extract_latex(text=text, svg=svg, expand_clozes=False)
    return ExtractedLatexOutput.from_proto(proto)


def _saved_fields_contain_latex(
    col: "Collection",
    saved_fields: tuple[str, ...],
    filename: str,
    svg: bool,
) -> bool:
    for field in saved_fields:
        if any(
            latex.filename == filename
            for latex in _extract_latex(col, field, svg).latex
        ):
            return True

    return False


def _save_latex_image_to_media(
    *,
    col: "Collection",
    extracted: "ExtractedLatex",
    header: str,
    footer: str,
    svg: bool,
) -> str | None:
    from anki.latex import save_latex_image

    return save_latex_image(col, extracted, header, footer, svg)


class LatexPreviewError(RuntimeError):
    def __init__(self, error_text: str) -> None:
        super().__init__(error_text)
        self.error_text = error_text


def _render_latex_image_bytes(
    *,
    col: "Collection",
    extracted: "ExtractedLatex",
    header: str,
    footer: str,
    svg: bool,
) -> bytes:
    from anki.latex import pngCommands, svgCommands

    latex = f"{header}\n{extracted.latex_body}\n{footer}"
    commands = svgCommands if svg else pngCommands
    extension = "svg" if svg else "png"

    with tempfile.TemporaryDirectory(prefix="anki-editor-latex-") as temp_dir:
        temp_path = Path(temp_dir)
        tex_path = temp_path / "tmp.tex"
        output_path = temp_path / f"tmp.{extension}"
        log_path = temp_path / "latex_log.txt"
        tex_path.write_text(latex, encoding="utf8")

        failed_command: str | None = None
        old_cwd = os.getcwd()
        try:
            os.chdir(temp_dir)
            with log_path.open("w", encoding="utf8") as log_file:
                for command in commands:
                    if call(command, stdout=log_file, stderr=log_file):
                        failed_command = command[0]
                        break
        finally:
            os.chdir(old_cwd)

        if failed_command:
            log = log_path.read_text(encoding="utf8", errors="replace")
            raise LatexPreviewError(
                _latex_error_text(col, failed_command, str(tex_path), log)
            )

        return output_path.read_bytes()


def _latex_error_text(
    col: "Collection",
    command: str,
    tex_path: str,
    log: str,
) -> str:
    message = f"{col.tr.media_error_executing(val=command)}\n"
    message += f"{col.tr.media_generated_file(val=tex_path)}\n"
    if log:
        message += log
    else:
        message += str(col.tr.media_have_you_installed_latex_and_dvipngdvisvgm())
    return message


def _data_url(image: bytes, svg: bool) -> str:
    mime = "image/svg+xml" if svg else "image/png"
    encoded = base64.b64encode(image).decode("ascii")
    return f"data:{mime};base64,{encoded}"
