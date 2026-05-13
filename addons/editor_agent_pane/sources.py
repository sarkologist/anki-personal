# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

TEXT_EXTENSIONS = {
    ".c",
    ".cc",
    ".cpp",
    ".css",
    ".go",
    ".h",
    ".hpp",
    ".html",
    ".java",
    ".js",
    ".json",
    ".jsx",
    ".kt",
    ".lua",
    ".md",
    ".markdown",
    ".py",
    ".rb",
    ".rs",
    ".rst",
    ".scss",
    ".sh",
    ".svelte",
    ".tex",
    ".toml",
    ".ts",
    ".tsx",
    ".txt",
    ".xml",
    ".yaml",
    ".yml",
}

IGNORED_DIRS = {
    ".cache",
    ".git",
    ".hg",
    ".mypy_cache",
    ".ruff_cache",
    ".svn",
    ".venv",
    "dist",
    "node_modules",
    "out",
    "target",
    "venv",
}


class SourceAccessError(ValueError):
    """Raised when a requested source path is outside the allowed folder."""


@dataclass(frozen=True)
class SearchHit:
    path: str
    line: int
    snippet: str


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def resolve_project_root(project_root: str | os.PathLike[str]) -> Path:
    if not str(project_root).strip():
        raise SourceAccessError("Project folder is empty.")
    root = Path(project_root).expanduser()
    try:
        resolved = root.resolve(strict=True)
    except FileNotFoundError as exc:
        raise SourceAccessError("Project folder does not exist.") from exc
    if not resolved.is_dir():
        raise SourceAccessError("Project folder is not a directory.")
    return resolved


def resolve_source_path(
    project_root: str | os.PathLike[str],
    relative_path: str | os.PathLike[str],
) -> Path:
    root = resolve_project_root(project_root)
    requested = Path(relative_path)
    if requested.is_absolute():
        raise SourceAccessError("Source paths must be relative to the project folder.")
    if any(part in {"", ".", ".."} for part in requested.parts):
        raise SourceAccessError("Source path must not contain traversal segments.")

    try:
        candidate = (root / requested).resolve(strict=True)
    except FileNotFoundError as exc:
        raise SourceAccessError("Source file does not exist.") from exc

    if not _is_relative_to(candidate, root):
        raise SourceAccessError("Source path escapes the project folder.")
    if not candidate.is_file():
        raise SourceAccessError("Source path is not a file.")
    return candidate


def _looks_textual(path: Path) -> bool:
    return path.suffix.lower() in TEXT_EXTENSIONS


def _decode_text(path: Path, max_bytes: int) -> str:
    size = path.stat().st_size
    if size > max_bytes:
        raise SourceAccessError("Source file is too large.")

    data = path.read_bytes()
    if b"\x00" in data:
        raise SourceAccessError("Source file appears to be binary.")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SourceAccessError("Source file is not valid UTF-8 text.") from exc


def read_source_file(
    project_root: str | os.PathLike[str],
    relative_path: str | os.PathLike[str],
    *,
    max_bytes: int = 65536,
) -> str:
    path = resolve_source_path(project_root, relative_path)
    if not _looks_textual(path):
        raise SourceAccessError("Source file extension is not enabled for v1.")
    return _decode_text(path, max_bytes=max_bytes)


def _iter_source_files(root: Path) -> tuple[Path, ...]:
    files: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(
            name
            for name in dirnames
            if name not in IGNORED_DIRS and not name.startswith(".")
        )
        current = Path(dirpath)
        for filename in sorted(filenames):
            if filename.startswith("."):
                continue
            path = current / filename
            if _looks_textual(path):
                files.append(path)
    return tuple(files)


def search_source_files(
    project_root: str | os.PathLike[str],
    query: str,
    *,
    max_results: int = 8,
    max_files: int = 400,
    max_file_bytes: int = 65536,
) -> list[SearchHit]:
    root = resolve_project_root(project_root)
    needle = query.strip().lower()
    if not needle:
        return []

    hits: list[SearchHit] = []
    scanned = 0
    for path in _iter_source_files(root):
        if scanned >= max_files or len(hits) >= max_results:
            break
        try:
            resolved = path.resolve(strict=True)
        except FileNotFoundError:
            continue
        if not _is_relative_to(resolved, root) or not resolved.is_file():
            continue

        scanned += 1
        try:
            text = _decode_text(resolved, max_bytes=max_file_bytes)
        except SourceAccessError:
            continue

        relative = resolved.relative_to(root).as_posix()
        for line_number, line in enumerate(text.splitlines(), start=1):
            if needle in line.lower():
                hits.append(
                    SearchHit(
                        path=relative,
                        line=line_number,
                        snippet=line.strip()[:500],
                    )
                )
                if len(hits) >= max_results:
                    break

    return hits
