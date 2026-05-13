# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from __future__ import annotations

import html
from html.parser import HTMLParser
from urllib.parse import urlparse

ALLOWED_TAGS = {
    "a",
    "b",
    "blockquote",
    "br",
    "code",
    "dd",
    "del",
    "details",
    "div",
    "dl",
    "dt",
    "em",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "hr",
    "i",
    "kbd",
    "li",
    "ol",
    "p",
    "pre",
    "s",
    "span",
    "strong",
    "sub",
    "summary",
    "sup",
    "table",
    "tbody",
    "td",
    "th",
    "thead",
    "tr",
    "u",
    "ul",
}
VOID_TAGS = {"br", "hr"}
SKIP_CONTENT_TAGS = {"script", "style", "iframe", "object", "embed", "link", "meta"}
SAFE_LINK_SCHEMES = {"http", "https", "mailto"}


class HtmlSanitizer(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self._parts: list[str] = []
        self._skip_stack: list[str] = []

    def sanitize(self, value: str) -> str:
        self.feed(value)
        self.close()
        return "".join(self._parts)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in SKIP_CONTENT_TAGS:
            self._skip_stack.append(tag)
            return
        if self._skipping():
            return
        if tag not in ALLOWED_TAGS:
            self._parts.append(html.escape(self.get_starttag_text() or f"<{tag}>"))
            return
        if tag in VOID_TAGS:
            self._parts.append(f"<{tag}>")
            return
        self._parts.append(f"<{tag}{self._safe_attrs(tag, attrs)}>")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in SKIP_CONTENT_TAGS or self._skipping():
            return
        if tag not in ALLOWED_TAGS:
            self._parts.append(html.escape(self.get_starttag_text() or f"<{tag}/>"))
            return
        if tag in VOID_TAGS:
            self._parts.append(f"<{tag}>")
        else:
            self._parts.append(f"<{tag}{self._safe_attrs(tag, attrs)}></{tag}>")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self._skip_stack:
            if tag == self._skip_stack[-1]:
                self._skip_stack.pop()
            return
        if tag in ALLOWED_TAGS and tag not in VOID_TAGS:
            self._parts.append(f"</{tag}>")
        elif tag not in SKIP_CONTENT_TAGS:
            self._parts.append(html.escape(f"</{tag}>"))

    def handle_data(self, data: str) -> None:
        if not self._skipping():
            self._parts.append(html.escape(data, quote=False))

    def handle_entityref(self, name: str) -> None:
        if not self._skipping():
            self._parts.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        if not self._skipping():
            self._parts.append(f"&#{name};")

    def handle_comment(self, data: str) -> None:
        return

    def _skipping(self) -> bool:
        return bool(self._skip_stack)

    def _safe_attrs(self, tag: str, attrs: list[tuple[str, str | None]]) -> str:
        safe: list[tuple[str, str]] = []
        if tag == "a":
            href = _safe_href(dict(attrs).get("href"))
            if href:
                safe.append(("href", href))
                safe.append(("rel", "noopener noreferrer"))
            title = dict(attrs).get("title")
            if title:
                safe.append(("title", title))
        return "".join(
            f' {key}="{html.escape(value, quote=True)}"' for key, value in safe
        )


def sanitize_html(value: str) -> str:
    return HtmlSanitizer().sanitize(value)


def _safe_href(value: str | None) -> str:
    if value is None:
        return ""
    stripped = value.strip()
    scheme = urlparse(stripped).scheme.lower()
    if scheme in SAFE_LINK_SCHEMES:
        return stripped
    return ""
