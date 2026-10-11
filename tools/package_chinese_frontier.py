# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Build an installable add-on; optionally install it in an existing addons21 folder."""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "addons" / "chinese_frontier"


def package(output: Path) -> list[Path]:
    files = sorted(
        path
        for path in SOURCE.iterdir()
        if path.suffix in (".py", ".json", ".md") and path.name != "meta.json"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        for path in files:
            archive.write(path, path.name)
    return files


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "out" / "addons" / "chinese_frontier.ankiaddon",
    )
    parser.add_argument(
        "--install-to", type=Path, help="Existing addons21 folder; Anki needs a restart"
    )
    parser.add_argument(
        "--deck",
        default="",
        help="Automatically configure this deck in --collection only",
    )
    parser.add_argument(
        "--collection",
        type=Path,
        help="Exact collection path for first-run configuration",
    )
    args = parser.parse_args()
    if bool(args.deck) != bool(args.collection) or (args.deck and not args.install_to):
        parser.error(
            "--deck and --collection must be supplied together with --install-to"
        )
    files = package(args.output)
    print(f"Packaged {args.output}")
    if args.install_to:
        if args.install_to.name != "addons21" or not args.install_to.is_dir():
            parser.error("--install-to must be an existing addons21 folder")
        target = args.install_to / "chinese_frontier"
        target.mkdir(exist_ok=True)
        for path in files:
            shutil.copy2(path, target / path.name)
        if args.deck:
            metadata = target / "meta.json"
            data = (
                json.loads(metadata.read_text(encoding="utf-8"))
                if metadata.exists()
                else {}
            )
            config = data.setdefault("config", {})
            config.update(
                {
                    "initial_deck_name": args.deck,
                    "initial_collection_path": str(args.collection.resolve()),
                }
            )
            metadata.write_text(json.dumps(data, indent=2), encoding="utf-8")
        print(f"Installed {target}; restart Anki to load it.")


if __name__ == "__main__":
    main()
