# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
"""Development eval for the French Frontier card generator.

Runs real cases through `generate` with deterministic graders. Results are
written for a public repository: they never contain audit evidence, note
dependencies or any other text from the user's own cards.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from collections import Counter as Tally
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "addons"))

from chinese_frontier.core import ValidationError  # noqa: E402
from chinese_frontier.french.core import (  # noqa: E402
    Candidate,
    Knowledge,
    Target,
    validate,
)
from chinese_frontier.french.generation import (  # noqa: E402
    SCHEMA,
    Draft,
    competing_alternatives,
    generate,
)
from chinese_frontier.french.lexicon import Lexicon  # noqa: E402
from chinese_frontier.providers import GenerationStopped  # noqa: E402

HERE = Path(__file__).resolve().parent
PROFILE = Path.home() / "Library/Application Support/Anki2"
DEFAULT_COLLECTION = PROFILE / "User 1/collection.anki2"
DEFAULT_DICTIONARY = PROFILE / "addons21/chinese_frontier/user_files/french-dictionary"
# Outcomes that say nothing about the generator: excluded from denominators
# and retried on resume.
UNRUN = ("not_run", "cancelled")


# Clients


class Counter:
    """Real CLI calls across all workers."""

    def __init__(self) -> None:
        self.value = 0
        self.lock = threading.Lock()

    def add(self) -> None:
        with self.lock:
            self.value += 1


class CountingClient:
    def __init__(self, inner: Any, counter: Counter) -> None:
        self.inner = inner
        self.counter = counter
        self.count = 0
        self.last_real = True

    def check_login(self) -> None:
        # Login is checked once before the run; generate() asks every case.
        pass

    def request(self, prompt: str, schema: dict[str, Any]) -> Any:
        self.last_real = True
        self.count += 1
        self.counter.add()
        return self.inner.request(prompt, schema)


class ReplayClient:
    """Answers the writer with a stored candidate; audits go to the real CLI."""

    def __init__(
        self, inner: CountingClient, target: Target, candidate: dict[str, Any]
    ) -> None:
        self.inner = inner
        self.target = target
        self.candidate = candidate
        self.replayed = False
        self.last_real = True

    @property
    def count(self) -> int:
        return self.inner.count

    def check_login(self) -> None:
        pass

    def request(self, prompt: str, schema: dict[str, Any]) -> Any:
        if schema is SCHEMA or schema == SCHEMA:
            self.last_real = False
            if not self.replayed:
                self.replayed = True
                candidates = [json.loads(json.dumps(self.candidate))]
                return {
                    "proposals": [
                        {"key": self.target.key, "reason": "", "candidates": candidates}
                    ]
                }
            return {
                "proposals": [
                    {
                        "key": self.target.key,
                        "reason": "replayed candidate already judged",
                        "candidates": [],
                    }
                ]
            }
        self.last_real = True
        return self.inner.request(prompt, schema)


# Grading


def card_fields(draft: Draft, known: Knowledge, lexicon: Lexicon) -> dict[str, str]:
    """The visible note fields, as admit() builds them."""
    from chinese_frontier.french.collection import card_kind, extra_field

    c, t = draft.candidate, draft.target
    valid = validate(c, t, known, lexicon)
    return {
        "Text": valid.text,
        "Translation": valid.translation,
        "Cue": html.escape(c.cue),
        "Target": html.escape(t.lemma),
        "Meaning": html.escape(c.meaning),
        "Extra": extra_field(c.explanation, t.kind, valid.form_key, c.mode, lexicon),
        "CardKind": card_kind(c.mode, t.kind),
    }


def substitutions(review: dict[str, Any], c: Candidate) -> list[dict[str, Any]]:
    """Competing answers, without the intended answer or legacy commentary."""
    return [
        a
        for a in competing_alternatives(review, c)
        if isinstance(a, dict)
        and isinstance(a.get("answer"), str)
        and not any(ch in a["answer"] for ch in ":;/{}<>\n\r")
    ]


def grade(
    draft: Draft, known: Knowledge, lexicon: Lexicon
) -> tuple[dict[str, bool], int, dict[str, str] | None]:
    from chinese_frontier.french.collection import card_kind

    c, t = draft.candidate, draft.target
    try:
        fields = card_fields(draft, known, lexicon)
    except (ValidationError, KeyError, TypeError):
        fields = None
    text = fields["Text"] if fields else ""
    translation = fields["Translation"] if fields else ""
    clozes = re.findall(r"\{\{c1::(.*?)\}\}", text)
    review = draft.audit.get("review") or {}
    # On a whole-word vocabulary blank every substitution that fits the
    # context fits the blank: only a stem or an infinitive cue can exclude it.
    strict = t.kind == "vocabulary" and c.mode in ("word", "contrast")
    alternatives = substitutions(review, c)
    fitting = [
        a
        for a in alternatives
        if a.get("fits_context") is True and (strict or a.get("fits_cloze") is True)
    ]
    disagreements = (
        sum(a.get("fits_cloze") != a.get("fits_context") for a in alternatives)
        if strict
        else 0
    )
    checks = {
        "validates": fields is not None,
        "one_cloze_per_example": text.count("{{c1::")
        == (2 if c.mode == "contrast" else 1),
        "translation_present": bool(re.sub(r"<[^>]*>", "", translation).strip())
        and "{{" not in translation,
        "meaning_present": bool(c.meaning.strip()),
        "card_kind_matches": bool(fields)
        and fields["CardKind"] == card_kind(c.mode, t.kind),
        # extra_field() promises tables only for word and stem conjugation
        # cards; they render as lists, so look for the rendered paradigm.
        "conjugation_table_present": t.kind != "conjugation"
        or c.mode == "contrast"
        or bool(fields and "<li>" in fields["Extra"]),
        "stem_hides_two_letters": c.mode != "stem"
        or (len(clozes) == 1 and len(html.unescape(clozes[0])) >= 2),
        "no_fitting_alternative": not fitting,
    }
    return checks, disagreements, fields


# Running


def bucket(reason: str) -> str:
    return reason.split(":", 1)[0].strip()


def classify(decisions: list[dict[str, Any]], kind: str) -> tuple[str, str | None]:
    if kind == "audit":
        # Replayed writer refusals after the verdict are not the writer's.
        decisions = [d for d in decisions if d["stage"] != "writer"] or decisions
    if not decisions:
        return "no_proposal", "no proposal"
    last = decisions[-1]
    if last["stage"] == "writer":
        return "refused", "writer refused"
    if last["stage"] == "validation":
        return "validation_rejected", bucket(last["reason"])
    return "audit_rejected", "audit rejected"


def review_record(review: dict[str, Any]) -> dict[str, Any]:
    return {
        k: review.get(k)
        for k in (
            "approved",
            "reason",
            "search_summary",
            "context_checks",
            "alternatives",
        )
    }


def run_case(
    case: dict[str, Any],
    run: int,
    known: Knowledge,
    lexicon: Lexicon,
    config: dict[str, Any],
    stop: threading.Event,
    client: CountingClient,
) -> dict[str, Any]:
    target = Target(**case["target"])
    kind = case["kind"]
    if kind == "audit":
        client = ReplayClient(client, target, case["candidate"])
    decisions: list[dict[str, Any]] = []
    calls: list[dict[str, Any]] = []

    def collect(event: dict[str, Any]) -> None:
        if event.get("type") == "decision":
            decisions.append(
                {k: event.get(k) for k in ("stage", "outcome", "mode", "reason")}
            )
        elif event.get("type") == "call" and event.get("finished"):
            calls.append(
                {
                    k: event.get(k)
                    for k in (
                        "phase",
                        "attempt",
                        "status",
                        "elapsed_seconds",
                        "prompt_chars",
                        "response_chars",
                        "error",
                    )
                }
                | {"real": client.last_real}
            )

    started = time.monotonic()
    drafts: list[Draft] = []
    error = None
    outcome = reason = None
    try:
        drafts, _ = generate(
            [target],
            known,
            lexicon,
            config,
            stop,
            client,
            observe=collect,
            progress=collect,
            lessons={},
        )
    except GenerationStopped:
        outcome, reason = "cancelled", "cancelled"
    except TimeoutError as exc:
        outcome, reason, error = "timeout", "timeout", str(exc)
    except (RuntimeError, ValueError) as exc:
        outcome, reason, error = "error", type(exc).__name__, str(exc)
    except Exception:  # noqa: BLE001
        outcome, reason, error = "crash", "crash", traceback.format_exc()
    checks: dict[str, bool] = {}
    disagreements = 0
    record = None
    if drafts:
        outcome, reason = "approved", None
        draft = drafts[0]
        checks, disagreements, fields = grade(draft, known, lexicon)
        record = {
            "candidate": json.loads(json.dumps(asdict(draft.candidate))),
            "review": review_record(draft.audit.get("review") or {}),
            "fields": fields,
        }
        failed = [name for name, ok in checks.items() if not ok]
        if failed:
            reason = "check:" + failed[0]
    elif outcome is None:
        outcome, reason = classify(decisions, kind)
    passed = outcome == "approved" and all(checks.values())
    row = {
        "case_id": case["id"],
        "run": run,
        "kind": kind,
        "group": case.get("group"),
        "label": case.get("label"),
        "why": case.get("why", ""),
        "target": case["target"],
        "outcome": outcome,
        "pass": passed,
        "checks": checks,
        "fits_cloze_disagreements": disagreements,
        "failure_reason": None if passed else reason,
        "decisions": decisions,
        "calls": calls,
        "calls_made": client.count,
        "elapsed_seconds": round(time.monotonic() - started, 2),
        "draft": record,
        "error": error[:4000] if error else None,
    }
    if kind == "audit":
        if case.get("label") == "rejected":
            rejected = outcome in (
                "validation_rejected",
                "audit_rejected",
                "refused",
                "no_proposal",
            ) or (outcome == "approved" and not checks["no_fitting_alternative"])
            row["verdict_matches_label"] = rejected
            row["failure_reason"] = None if rejected else reason or "audit approved"
        else:
            row["verdict_matches_label"] = passed
        row["pass"] = row["verdict_matches_label"]
    return row


class ResultWriter:
    def __init__(self, folder: Path) -> None:
        self.folder = folder
        folder.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.done = {
            (r["case_id"], r["run"])
            for r in read_rows(folder)
            if r["outcome"] not in UNRUN
        }

    def write(self, row: dict[str, Any]) -> None:
        public = {k: v for k, v in row.items() if k != "evidence"}
        with self.lock:
            with (self.folder / "results.jsonl").open("a", encoding="utf8") as f:
                f.write(json.dumps(public, ensure_ascii=False) + "\n")
            with (self.folder / "calls.jsonl").open("a", encoding="utf8") as f:
                for call in row.get("calls", []):
                    entry = {"case_id": row["case_id"], "run": row["run"], **call}
                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def read_rows(folder: Path) -> list[dict[str, Any]]:
    path = folder / "results.jsonl"
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def not_run(case: dict[str, Any], run: int) -> dict[str, Any]:
    return {
        "case_id": case["id"],
        "run": run,
        "kind": case["kind"],
        "group": case.get("group"),
        "label": case.get("label"),
        "why": case.get("why", ""),
        "target": case["target"],
        "outcome": "not_run",
        "pass": False,
        "checks": {},
        "fits_cloze_disagreements": 0,
        "failure_reason": "budget",
        "decisions": [],
        "calls": [],
        "calls_made": 0,
        "elapsed_seconds": 0,
        "draft": None,
        "error": None,
    }


def run_all(
    cases: list[dict[str, Any]],
    runs: int,
    known: Knowledge,
    lexicon: Lexicon,
    config: dict[str, Any],
    writer: ResultWriter,
    *,
    workers: int,
    max_calls: int,
    client_factory: Callable[[threading.Event], Any],
) -> Counter:
    stop = threading.Event()
    counter = Counter()
    local = threading.local()
    # Run-major order, so a budget cut leaves complete earlier passes.
    jobs = [
        (case, run)
        for run in range(1, runs + 1)
        for case in cases
        if (case["id"], run) not in writer.done
    ]

    def work(case: dict[str, Any], run: int) -> None:
        if stop.is_set():
            return
        if counter.value >= max_calls:
            writer.write(not_run(case, run))
            return
        if not hasattr(local, "client"):
            local.client = client_factory(stop)
        row = run_case(
            case,
            run,
            known,
            lexicon,
            config,
            stop,
            CountingClient(local.client, counter),
        )
        writer.write(row)
        print(
            f"{row['case_id']} run {run}: {row['outcome']}"
            f" {'pass' if row['pass'] else 'FAIL ' + str(row['failure_reason'])}"
            f" ({row['calls_made']} calls, {row['elapsed_seconds']:.0f}s;"
            f" total {counter.value})",
            flush=True,
        )

    pool = ThreadPoolExecutor(max_workers=max(1, workers))
    futures = [pool.submit(work, case, run) for case, run in jobs]
    try:
        for future in futures:
            while True:
                try:
                    future.result(timeout=1)
                    break
                except TimeoutError:
                    continue
    except KeyboardInterrupt:
        print("Interrupted: stopping model calls", file=sys.stderr)
        stop.set()
        pool.shutdown(wait=True, cancel_futures=True)
        raise
    pool.shutdown(wait=True)
    return counter


# Knowledge


def save_knowledge(path: Path, known: Knowledge, day: int) -> None:
    data = {
        "version": known.version,
        "day": day,
        "counts": knowledge_counts(known),
        "surfaces": known.surfaces,
        "forms": sorted(known.forms),
        "meanings": [list(x) for x in sorted(known.meanings)],
        "constructions": sorted(known.constructions),
        "identities": {
            k: [list(x) for x in sorted(v)] for k, v in sorted(known.identities.items())
        },
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf8")


def read_knowledge(path: Path) -> tuple[Knowledge, dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf8"))
    known = Knowledge(
        surfaces=data["surfaces"],
        forms=set(data["forms"]),
        meanings={tuple(x) for x in data["meanings"]},
        constructions=set(data["constructions"]),
        identities={k: {tuple(x) for x in v} for k, v in data["identities"].items()},
    )
    if known.version != data["version"]:
        raise ValueError(
            f"{path} was written under a different knowledge policy; delete it"
        )
    return known, {"version": known.version, "day": data["day"]}


def knowledge_counts(known: Knowledge) -> dict[str, int]:
    return {
        "surfaces": len(known.surfaces),
        "forms": len(known.forms),
        "meanings": len(known.meanings),
        "constructions": len(known.constructions),
    }


def snapshot_collection(path: Path, lexicon: Lexicon) -> tuple[Knowledge, int]:
    """Read knowledge from a copy; the profile collection is never opened."""
    from chinese_frontier.french.collection import SETTINGS, snapshot

    from anki.collection import Collection

    folder = Path(tempfile.mkdtemp(prefix="frontier-eval-"))
    try:
        copy = folder / "collection.anki2"
        for suffix in ("", "-wal", "-shm"):
            source = path.with_name(path.name + suffix)
            if source.exists():
                shutil.copy2(source, folder / (copy.name + suffix))
        if not copy.exists():
            raise FileNotFoundError(path)
        col = Collection(str(copy))
        try:
            settings = col.get_config(SETTINGS, {})
            if not settings.get("deck_id"):
                raise ValueError("French Frontier has no deck_id in this collection")
            current = snapshot(col, settings, {}, lexicon)
            return current.known, current.day
        finally:
            col.close()
    finally:
        shutil.rmtree(folder, ignore_errors=True)


# Summary


def ratio(a: float, b: float) -> float | None:
    return a / b if b else None


def latest(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The last record of each case-run; resumes may rerun unrun ones."""
    by_pair: dict[tuple[str, int], dict[str, Any]] = {}
    for r in rows:
        by_pair[(r["case_id"], r["run"])] = r
    return list(by_pair.values())


def summarize(
    rows: list[dict[str, Any]], runs: int, run_info: dict[str, Any] | None = None
) -> dict[str, Any]:
    rows = latest(rows)
    attempted = [r for r in rows if r["outcome"] not in UNRUN]
    cases: dict[str, dict[str, Any]] = {}
    for r in sorted(rows, key=lambda r: (r["case_id"], r["run"])):
        case = cases.setdefault(
            r["case_id"],
            {
                "id": r["case_id"],
                "kind": r["kind"],
                "group": r.get("group"),
                "label": r.get("label"),
                "why": r.get("why", ""),
                "runs": [],
                "last_failure_reason": None,
            },
        )
        if r["outcome"] in UNRUN:
            continue
        case["runs"].append(
            {"run": r["run"], "pass": r["pass"], "outcome": r["outcome"]}
        )
        if not r["pass"]:
            case["last_failure_reason"] = r.get("failure_reason")
    for case in cases.values():
        case["missing_runs"] = max(0, runs - len(case["runs"]))
        case["pass^k"] = len(case["runs"]) >= runs and all(
            x["pass"] for x in case["runs"]
        )
        case["pass@k"] = any(x["pass"] for x in case["runs"])

    def metrics(kind: str) -> dict[str, Any]:
        done = [r for r in attempted if r["kind"] == kind]
        mine = [c for c in cases.values() if c["kind"] == kind]
        return {
            "cases": len(mine),
            "attempted": len(done),
            "approved": sum(r["outcome"] == "approved" for r in done),
            "yield": ratio(sum(r["outcome"] == "approved" for r in done), len(done)),
            "pass@1": ratio(sum(r["pass"] for r in done), len(done)),
            "pass^k": ratio(sum(c["pass^k"] for c in mine), len(mine)),
            "pass@k": ratio(sum(c["pass@k"] for c in mine), len(mine)),
        }

    groups: dict[str, dict[str, Any]] = {}
    for r in attempted:
        g = groups.setdefault(
            f"{r['kind']}/{r.get('group')}",
            {"attempted": 0, "approved": 0, "passed": 0},
        )
        g["attempted"] += 1
        g["approved"] += r["outcome"] == "approved"
        g["passed"] += bool(r["pass"])
    for g in groups.values():
        g["yield"] = ratio(g["approved"], g["attempted"])
        g["pass@1"] = ratio(g["passed"], g["attempted"])
    calls = [c for r in rows for c in r.get("calls", [])]
    real = [c for c in calls if c.get("real", True)]
    return {
        "runs": runs,
        "cases": len(cases),
        "case_runs": len(rows),
        "attempted": len(attempted),
        "not_run": sum(r["outcome"] == "not_run" for r in rows),
        "cancelled": sum(r["outcome"] == "cancelled" for r in rows),
        "calls": {
            "real": sum(r.get("calls_made", 0) for r in rows),
            "replayed": len(calls) - len(real),
            "by_phase": dict(Tally(c.get("phase") for c in real)),
            "by_status": dict(Tally(c.get("status") for c in real)),
        },
        "elapsed_seconds": {
            "case_runs": round(sum(r.get("elapsed_seconds") or 0 for r in rows), 1),
            "wall": (run_info or {}).get("wall_seconds"),
        },
        "generate": metrics("generate"),
        "audit": metrics("audit"),
        "outcomes": dict(Tally(r["outcome"] for r in attempted)),
        "failure_reasons": dict(
            Tally(
                r.get("failure_reason") for r in attempted if not r["pass"]
            ).most_common()
        ),
        "fits_cloze_disagreements": sum(
            r.get("fits_cloze_disagreements", 0) for r in attempted
        ),
        "groups": dict(sorted(groups.items())),
        "incomplete_cases": sorted(
            c["id"] for c in cases.values() if c["missing_runs"]
        ),
        "per_case": list(cases.values()),
    }


def pct(value: float | None) -> str:
    return "–" if value is None else f"{value:.0%}"


def cell(text: Any) -> str:
    return str(text if text is not None else "").replace("|", "\\|").replace("\n", " ")


def summary_markdown(s: dict[str, Any]) -> str:
    k = s["runs"]
    lines = [
        "# French Frontier eval",
        "",
        f"{s['cases']} cases × {k} runs: {s['attempted']} case-runs attempted, "
        f"{s['not_run']} not run (budget), {s['cancelled']} cancelled. "
        f"{s['calls']['real']} real model calls, {s['calls']['replayed']} replayed. "
        f"Case-run time {s['elapsed_seconds']['case_runs']:.0f}s"
        + (
            f", wall {s['elapsed_seconds']['wall']:.0f}s."
            if s["elapsed_seconds"].get("wall")
            else "."
        ),
        "",
        f"k = {k}.",
        "",
        "| | cases | case-runs | yield | pass@1 | pass^k | pass@k |",
        "|---|---|---|---|---|---|---|",
    ]
    for kind, title in (("generate", "Generate"), ("audit", "Audit vs label")):
        m = s[kind]
        lines.append(
            f"| {title} | {m['cases']} | {m['attempted']} | {pct(m['yield'])} | "
            f"{pct(m['pass@1'])} | {pct(m['pass^k'])} | {pct(m['pass@k'])} |"
        )
    lines += [
        "",
        "Yield: approved case-runs over attempted ones. pass@1: mean pass over "
        "case-runs. pass^k: cases passing all k runs. pass@k: cases passing "
        "at least once. Audit cases pass when the verdict matches the user's label. "
        "Timeouts, errors and refusals count as failures.",
        "",
        "## Calls",
        "",
        "| phase | calls |",
        "|---|---|",
        *(f"| {cell(p)} | {n} |" for p, n in sorted(s["calls"]["by_phase"].items())),
        "",
        "| status | calls |",
        "|---|---|",
        *(f"| {cell(p)} | {n} |" for p, n in sorted(s["calls"]["by_status"].items())),
        "",
        "## Failures",
        "",
        "| outcome | case-runs |",
        "|---|---|",
        *(f"| {cell(o)} | {n} |" for o, n in sorted(s["outcomes"].items())),
        "",
        "| reason | case-runs |",
        "|---|---|",
        *(f"| {cell(o)} | {n} |" for o, n in s["failure_reasons"].items()),
        "",
        f"Whole-word fits_cloze disagreements: {s['fits_cloze_disagreements']}.",
        "",
        "## Groups",
        "",
        "| group | case-runs | yield | pass@1 |",
        "|---|---|---|---|",
        *(
            f"| {cell(g)} | {v['attempted']} | {pct(v['yield'])} | {pct(v['pass@1'])} |"
            for g, v in s["groups"].items()
        ),
        "",
        "## Cases",
        "",
        "| case | group | label | why | runs | pass^k | last failure |",
        "|---|---|---|---|---|---|---|",
    ]
    for c in s["per_case"]:
        why = c["why"] if len(c["why"]) <= 90 else c["why"][:87] + "…"
        results = " ".join(
            ("✓" if x["pass"] else "✗") + " " + x["outcome"] for x in c["runs"]
        ) + (f" ({c['missing_runs']} missing)" if c["missing_runs"] else "")
        lines.append(
            f"| {cell(c['id'])} | {cell(c['group'])} | {cell(c['label'])} | "
            f"{cell(why)} | {cell(results)} | {'✓' if c['pass^k'] else '✗'} | "
            f"{cell(c['last_failure_reason'])} |"
        )
    if s["incomplete_cases"]:
        lines += [
            "",
            "Incomplete (count as not passing): " + ", ".join(s["incomplete_cases"]),
        ]
    return "\n".join(lines) + "\n"


def write_summary(folder: Path) -> dict[str, Any]:
    info_path = folder / "run.json"
    info = json.loads(info_path.read_text()) if info_path.exists() else {}
    rows = read_rows(folder)
    runs = info.get("settings", {}).get("runs") or max(
        (r["run"] for r in rows), default=1
    )
    s = summarize(rows, runs, info)
    (folder / "summary.json").write_text(
        json.dumps(s, ensure_ascii=False, indent=1), encoding="utf8"
    )
    (folder / "summary.md").write_text(summary_markdown(s), encoding="utf8")
    return s


def compare(old_dir: Path, new_dir: Path) -> tuple[dict[str, Any], str]:
    old = json.loads((old_dir / "summary.json").read_text())
    new = json.loads((new_dir / "summary.json").read_text())
    before = {c["id"]: c for c in old["per_case"]}
    after = {c["id"]: c for c in new["per_case"]}
    shared = sorted(before.keys() & after.keys())
    result = {
        "wins": [i for i in shared if not before[i]["pass^k"] and after[i]["pass^k"]],
        "regressions": [
            i for i in shared if before[i]["pass^k"] and not after[i]["pass^k"]
        ],
        "unchanged": [i for i in shared if before[i]["pass^k"] == after[i]["pass^k"]],
        "only_old": sorted(before.keys() - after.keys()),
        "only_new": sorted(after.keys() - before.keys()),
        "deltas": {
            kind: {
                m: (
                    None
                    if old[kind][m] is None or new[kind][m] is None
                    else new[kind][m] - old[kind][m]
                )
                for m in ("yield", "pass@1", "pass^k", "pass@k")
            }
            for kind in ("generate", "audit")
        },
    }

    def signed(v: float | None) -> str:
        return "–" if v is None else f"{v:+.0%}"

    lines = [
        f"# Compare {old_dir.name} → {new_dir.name}",
        "",
        "| | metric | old | new | change |",
        "|---|---|---|---|---|",
    ]
    for kind in ("generate", "audit"):
        for m in ("yield", "pass@1", "pass^k", "pass@k"):
            lines.append(
                f"| {kind} | {m} | {pct(old[kind][m])} | {pct(new[kind][m])} | "
                f"{signed(result['deltas'][kind][m])} |"
            )
    for title, key in (
        ("Wins (fail → pass on pass^k)", "wins"),
        ("Regressions (pass → fail on pass^k)", "regressions"),
        ("Only in old", "only_old"),
        ("Only in new", "only_new"),
    ):
        ids = result[key]
        lines += ["", f"## {title}: {len(ids)}", ""]
        for i in ids:
            c = after.get(i) or before[i]
            reason = c.get("last_failure_reason")
            lines.append(f"- {i} ({c['group']})" + (f": {reason}" if reason else ""))
    lines += ["", f"Unchanged: {len(result['unchanged'])}."]
    text = "\n".join(lines) + "\n"
    (new_dir / "compare.md").write_text(text, encoding="utf8")
    return result, text


# CLI


def git_commit() -> dict[str, Any]:
    def git(*args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=ROOT, capture_output=True, text=True, check=False
        ).stdout.strip()

    return {
        "commit": git("rev-parse", "HEAD"),
        "dirty": bool(git("status", "--porcelain")),
    }


def select(cases: list[dict[str, Any]], only: str, group: str) -> list[dict[str, Any]]:
    if only:
        wanted = [x.strip() for x in only.split(",") if x.strip()]
        unknown = set(wanted) - {c["id"] for c in cases}
        if unknown:
            raise SystemExit("Unknown case ids: " + ", ".join(sorted(unknown)))
        cases = [c for c in cases if c["id"] in wanted]
    if group:
        cases = [c for c in cases if c["group"] == group]
    return cases


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=HERE / "french_cases.json")
    parser.add_argument("--collection", type=Path, default=DEFAULT_COLLECTION)
    parser.add_argument("--dictionary", type=Path, default=DEFAULT_DICTIONARY)
    parser.add_argument("--knowledge-cache", type=Path)
    parser.add_argument("--provider", default="claude")
    parser.add_argument("--model", default="claude-opus-5-5")
    parser.add_argument("--timeout", type=int, default=480)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--max-calls", type=int, default=600)
    parser.add_argument("--only", default="")
    parser.add_argument("--group", default="")
    parser.add_argument("--results", type=Path)
    parser.add_argument("--summarize", type=Path, metavar="DIR")
    parser.add_argument("--compare", type=Path, nargs=2, metavar=("OLD", "NEW"))
    args = parser.parse_args(argv)

    if args.compare:
        print(compare(*args.compare)[1])
        return 0
    if args.summarize:
        write_summary(args.summarize)
        print((args.summarize / "summary.md").read_text())
        return 0

    raw = args.cases.read_bytes()
    cases = select(json.loads(raw)["cases"], args.only, args.group)
    if not cases:
        raise SystemExit("No cases selected")
    if not (args.dictionary / "lexicon-v4.json").exists():
        raise SystemExit(f"No French dictionary cache in {args.dictionary}")
    lexicon = Lexicon.load(args.dictionary)
    if args.knowledge_cache and args.knowledge_cache.exists():
        known, info = read_knowledge(args.knowledge_cache)
        day, source = info["day"], "cache"
    else:
        known, day = snapshot_collection(args.collection.expanduser(), lexicon)
        source = "collection copy"
        if args.knowledge_cache:
            save_knowledge(args.knowledge_cache, known, day)

    from chinese_frontier.providers import CliGenerator

    config = {
        "provider": args.provider,
        f"{args.provider}_model": args.model,
        "timeout_seconds": args.timeout,
    }
    CliGenerator(config, threading.Event()).check_login()
    folder = args.results or (
        HERE / "results" / datetime.now().strftime("%Y-%m-%d-%H%M")
    )
    writer = ResultWriter(folder)
    info_path = folder / "run.json"
    info = json.loads(info_path.read_text()) if info_path.exists() else {}
    info.update(
        settings={
            "provider": args.provider,
            "model": args.model,
            "timeout_seconds": args.timeout,
            "runs": args.runs,
            "workers": args.workers,
            "max_calls": args.max_calls,
            "only": args.only,
            "group": args.group,
            "cases_file": str(args.cases.resolve().relative_to(ROOT))
            if args.cases.resolve().is_relative_to(ROOT)
            else args.cases.name,
        },
        cases_sha256=hashlib.sha256(raw).hexdigest(),
        knowledge={
            "version": known.version,
            "day": day,
            "source": source,
            **knowledge_counts(known),
        },
        git=git_commit(),
    )
    info.setdefault("started_at", datetime.now().isoformat(timespec="seconds"))
    info_path.write_text(json.dumps(info, indent=1))
    began = time.monotonic()
    interrupted = False
    try:
        run_all(
            cases,
            args.runs,
            known,
            lexicon,
            config,
            writer,
            workers=args.workers,
            max_calls=args.max_calls,
            client_factory=lambda stop: CliGenerator(config, stop),
        )
    except KeyboardInterrupt:
        interrupted = True
    info["finished_at"] = datetime.now().isoformat(timespec="seconds")
    info["wall_seconds"] = round(
        info.get("wall_seconds", 0) + time.monotonic() - began, 1
    )
    info["interrupted"] = interrupted
    info_path.write_text(json.dumps(info, indent=1))
    write_summary(folder)
    print((folder / "summary.md").read_text())
    print(f"Results: {folder}")
    return 130 if interrupted else 0


if __name__ == "__main__":
    sys.exit(main())
