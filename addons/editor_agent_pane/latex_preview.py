# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from __future__ import annotations

import base64
import html
import os
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .sanitize import sanitize_html, sanitize_html_for_agent_preview


@dataclass(frozen=True)
class PreviewExtractedLatex:
    filename: str
    latex_body: str


@dataclass(frozen=True)
class PreviewExtractedLatexOutput:
    html: str
    latex: tuple[PreviewExtractedLatex, ...]


class LatexPreviewError(RuntimeError):
    def __init__(self, message_html: str) -> None:
        super().__init__(message_html)
        self.message_html = message_html


ExtractLatexFn = Callable[[str, bool], PreviewExtractedLatexOutput]
RenderLatexImageFn = Callable[[PreviewExtractedLatex, str, str, bool], bytes]
LatexEnabledFn = Callable[[], bool]


class LegacyLatexPreviewRenderer:
    def __init__(
        self,
        *,
        col: Any,
        notetype: dict[str, Any],
        extract_latex: ExtractLatexFn | None = None,
        render_latex_image: RenderLatexImageFn | None = None,
        latex_enabled: bool | LatexEnabledFn | None = None,
    ) -> None:
        self._col = col
        self._notetype = notetype
        self._extract_latex = extract_latex or self._extract_latex_with_backend
        self._render_latex_image = render_latex_image or self._render_latex_image_bytes
        self._latex_enabled = latex_enabled
        self._rendered_data_urls: dict[str, str] = {}

    def render(self, html_text: str) -> str:
        svg = bool(self._notetype.get("latexsvg", False))
        output = self._extract_latex(html_text, svg)
        if not output.latex:
            return sanitize_html(output.html)

        if not self._latex_generation_enabled():
            return self._fallback_with_errors(
                html_text, [self._latex_generation_disabled_message()]
            )

        errors: list[str] = []
        header = str(self._notetype.get("latexPre", ""))
        footer = str(self._notetype.get("latexPost", ""))
        for extracted in output.latex:
            if extracted.filename in self._rendered_data_urls:
                continue
            try:
                image = self._render_latex_image(extracted, header, footer, svg)
            except LatexPreviewError as exc:
                errors.append(exc.message_html)
                continue
            except Exception as exc:
                errors.append(str(exc))
                continue
            self._rendered_data_urls[extracted.filename] = _data_url(image, svg)

        if errors:
            return self._fallback_with_errors(html_text, errors)

        return sanitize_html_for_agent_preview(output.html, self._rendered_data_urls)

    def _extract_latex_with_backend(
        self, html_text: str, svg: bool
    ) -> PreviewExtractedLatexOutput:
        from anki.latex import ExtractedLatexOutput

        proto = self._col._backend.extract_latex(
            text=html_text, svg=svg, expand_clozes=False
        )
        output = ExtractedLatexOutput.from_proto(proto)
        return PreviewExtractedLatexOutput(
            html=output.html,
            latex=tuple(
                PreviewExtractedLatex(
                    filename=extracted.filename,
                    latex_body=extracted.latex_body,
                )
                for extracted in output.latex
            ),
        )

    def _latex_generation_enabled(self) -> bool:
        if isinstance(self._latex_enabled, bool):
            return self._latex_enabled
        if self._latex_enabled is not None:
            return self._latex_enabled()

        from anki.config import Config

        return bool(self._col.get_config_bool(Config.Bool.RENDER_LATEX))

    def _latex_generation_disabled_message(self) -> str:
        return str(self._col.tr.preferences_latex_generation_disabled())

    def _render_latex_image_bytes(
        self,
        extracted: PreviewExtractedLatex,
        header: str,
        footer: str,
        svg: bool,
    ) -> bytes:
        from anki.latex import pngCommands, svgCommands
        from anki.utils import call

        latex = f"{header}\n{extracted.latex_body}\n{footer}"
        commands = svgCommands if svg else pngCommands
        extension = "svg" if svg else "png"

        with tempfile.TemporaryDirectory(prefix="anki-agent-latex-") as temp_dir:
            tex_path = Path(temp_dir) / "tmp.tex"
            output_path = Path(temp_dir) / f"tmp.{extension}"
            log_path = Path(temp_dir) / "latex_log.txt"
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
                    self._latex_error_message(failed_command, str(tex_path), log)
                )
            return output_path.read_bytes()

    def _latex_error_message(self, command: str, tex_path: str, log: str) -> str:
        message = f"{self._col.tr.media_error_executing(val=command)}<br>"
        message += f"{self._col.tr.media_generated_file(val=tex_path)}<br>"
        if log:
            message += f"<pre>{html.escape(log)}</pre>"
        else:
            message += str(self._col.tr.media_have_you_installed_latex_and_dvipngdvisvgm())
        return message

    def _fallback_with_errors(self, html_text: str, errors: list[str]) -> str:
        rendered_errors = "".join(
            f'<div class="agent-latex-error">{sanitize_html(error)}</div>'
            for error in errors
        )
        return sanitize_html(html_text) + rendered_errors


def _data_url(image: bytes, svg: bool) -> str:
    mime = "image/svg+xml" if svg else "image/png"
    encoded = base64.b64encode(image).decode("ascii")
    return f"data:{mime};base64,{encoded}"
