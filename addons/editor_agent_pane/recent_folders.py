# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from __future__ import annotations

from typing import Any

MAX_RECENT_PROJECT_FOLDERS = 10
NO_PROJECT_FOLDER_LABEL = "Don't work in a folder"


def _clean_project_folder(folder: Any) -> str | None:
    if not isinstance(folder, str):
        return None
    folder = folder.strip()
    if folder == NO_PROJECT_FOLDER_LABEL:
        return None
    return folder or None


def recent_project_folders(saved_folders: Any) -> list[str]:
    if not isinstance(saved_folders, list):
        return []

    folders: list[str] = []
    for folder in saved_folders:
        cleaned = _clean_project_folder(folder)
        if cleaned is None or cleaned in folders:
            continue
        folders.append(cleaned)
        if len(folders) == MAX_RECENT_PROJECT_FOLDERS:
            break
    return folders


def remember_project_folder(project_folder: str, saved_folders: Any) -> list[str]:
    folders = recent_project_folders(saved_folders)
    current = _clean_project_folder(project_folder)
    if current is None:
        return folders
    if current in folders:
        folders.remove(current)
    return [current, *folders][:MAX_RECENT_PROJECT_FOLDERS]


def project_folder_choices(project_folder: str, saved_folders: Any) -> list[str]:
    return [
        NO_PROJECT_FOLDER_LABEL,
        *remember_project_folder(project_folder, saved_folders),
    ]
