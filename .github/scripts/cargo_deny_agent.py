# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Turns Claude's structured answer into a diff, in a step without the Claude token.

Claude itself only reads files. Every command runs here, from validated values.
"""

import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path

REGISTRY = "registry+https://github.com/rust-lang/crates.io-index"
REPORT = ".claude-cargo-deny-review.json"
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
VERSION = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")
MAX_UPGRADES = 50
MAX_FINDINGS = 10
MAX_FINDING_CHARS = 1500

SCHEMAS = {
    "repair": {
        "type": "object",
        "properties": {
            "upgrades": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "from": {"type": "string"},
                        "to": {"type": "string"},
                    },
                    "required": ["name", "from", "to"],
                    "additionalProperties": False,
                },
            },
            "explanation": {"type": "string"},
        },
        "required": ["upgrades", "explanation"],
        "additionalProperties": False,
    },
    "review": {
        "type": "object",
        "properties": {
            "verdict": {"enum": ["approve", "request_changes"]},
            "findings": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["verdict", "findings"],
        "additionalProperties": False,
    },
}


def schema(mode):
    return json.dumps(SCHEMAS[mode])


def structured(result):
    if result.get("is_error") or result.get("subtype") != "success":
        raise ValueError("Claude run did not finish successfully")
    answer = result.get("structured_output")
    if not isinstance(answer, dict):
        raise ValueError("Claude returned no structured answer")
    return answer


def upgrades(result, lock):
    requested = structured(result).get("upgrades")
    if not isinstance(requested, list) or not 0 < len(requested) <= MAX_UPGRADES:
        raise ValueError("Claude proposed no upgrades, or too many")
    registry = {
        (p["name"], p["version"])
        for p in lock["package"]
        if p.get("source") == REGISTRY
    }
    result = []
    for upgrade in requested:
        name, old, new = upgrade.get("name"), upgrade.get("from"), upgrade.get("to")
        if not all(isinstance(v, str) for v in (name, old, new)):
            raise ValueError("Malformed upgrade")
        if not NAME.fullmatch(name) or not VERSION.fullmatch(old):
            raise ValueError(f"Invalid package: {name!r} {old!r}")
        if not VERSION.fullmatch(new) or new == old:
            raise ValueError(f"Invalid target version for {name}: {new!r}")
        if (name, old) not in registry:
            raise ValueError(f"{name} {old} is not a crates.io package in Cargo.lock")
        result.append((name, old, new))
    return result


def review_report(result, head, base):
    answer = structured(result)
    if answer.get("verdict") not in {"approve", "request_changes"}:
        raise ValueError("Invalid review verdict")
    findings = answer.get("findings")
    if not isinstance(findings, list) or not all(isinstance(f, str) for f in findings):
        raise ValueError("Invalid review findings")
    findings = [f[:MAX_FINDING_CHARS] for f in findings[:MAX_FINDINGS]]
    return {
        "head": head,
        "base": base,
        "verdict": answer["verdict"],
        "findings": findings,
    }


def run(*args):
    subprocess.run(args, check=True)


def apply(mode, result_path, head, base, patch_path):
    result = json.loads(Path(result_path).read_text())
    if mode == "repair":
        expected = "Cargo.lock"
        for name, old, new in upgrades(
            result, tomllib.loads(Path(expected).read_text())
        ):
            print(f"Upgrading {name} {old} -> {new}", flush=True)
            run("cargo", "update", "--package", f"{name}@{old}", "--precise", new)
    else:
        expected = REPORT
        report = review_report(result, head, base)
        print(f"Review verdict: {report['verdict']}", flush=True)
        Path(expected).write_text(json.dumps(report, indent=2) + "\n")
    changed = subprocess.run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    if [line[3:] for line in changed] != [expected]:
        raise ValueError(f"Expected only {expected} to change, got {changed}")
    run("git", "add", "--", expected)
    with open(patch_path, "w") as stream:
        subprocess.run(
            ["git", "diff", "--cached", "--binary", "--", expected],
            check=True,
            stdout=stream,
        )


if __name__ == "__main__":
    if sys.argv[1] == "schema":
        print(schema(sys.argv[2]))
    else:
        apply(*sys.argv[2:])
