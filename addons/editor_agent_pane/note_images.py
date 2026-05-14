# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from __future__ import annotations

import urllib.parse
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .patches import FieldSnapshot, NoteImageSnapshot

IMAGE_EXTENSIONS = {
    ".avif",
    ".gif",
    ".ico",
    ".jpeg",
    ".jpg",
    ".png",
    ".svg",
    ".webp",
}


@dataclass
class _ImageEntry:
    filename: str
    fields: list[str]
    path: Path


def collect_note_images(
    media_manager: Any,
    notetype_id: int,
    fields: Iterable[FieldSnapshot],
) -> tuple[NoteImageSnapshot, ...]:
    try:
        media_root = Path(media_manager.dir()).resolve(strict=True)
    except (OSError, RuntimeError):
        return ()

    entries: list[_ImageEntry] = []
    by_path: dict[Path, int] = {}
    for field in fields:
        try:
            filenames = media_manager.files_in_str(notetype_id, field.html)
        except Exception:
            continue

        for raw_filename in filenames:
            resolved = _resolve_local_image(media_root, raw_filename)
            if resolved is None:
                continue
            path, filename = resolved
            if path in by_path:
                entry = entries[by_path[path]]
                if field.name not in entry.fields:
                    entry.fields.append(field.name)
                continue

            by_path[path] = len(entries)
            entries.append(
                _ImageEntry(filename=filename, fields=[field.name], path=path)
            )

    return tuple(
        NoteImageSnapshot(
            attachment_index=index,
            filename=entry.filename,
            fields=tuple(entry.fields),
            path=str(entry.path),
        )
        for index, entry in enumerate(entries, start=1)
    )


def _resolve_local_image(
    media_root: Path,
    raw_filename: Any,
) -> tuple[Path, str] | None:
    if not isinstance(raw_filename, str):
        return None
    filename = urllib.parse.unquote(raw_filename).strip()
    if not filename or _has_uri_scheme(filename):
        return None

    relative_path = Path(filename)
    if (
        relative_path.is_absolute()
        or relative_path.suffix.lower() not in IMAGE_EXTENSIONS
    ):
        return None

    try:
        path = (media_root / relative_path).resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    if not _is_relative_to(path, media_root) or not path.is_file():
        return None
    try:
        with path.open("rb"):
            pass
    except OSError:
        return None

    return path, filename


def _has_uri_scheme(filename: str) -> bool:
    return bool(urllib.parse.urlsplit(filename).scheme)


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True
