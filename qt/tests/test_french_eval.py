# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
from __future__ import annotations

import importlib.util
import json
import sys
from dataclasses import asdict
from pathlib import Path
from threading import Event

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "addons"))
from chinese_frontier.french.core import Candidate, Knowledge, Target  # noqa: E402
from chinese_frontier.french.generation import (  # noqa: E402
    REVIEW,
    SCHEMA,
    Draft,
    candidate_hash,
)
from chinese_frontier.french.lexicon import Form, Lexicon  # noqa: E402

spec = importlib.util.spec_from_file_location(
    "run_french_eval", ROOT / "tools" / "frontier_eval" / "run_french_eval.py"
)
ev = importlib.util.module_from_spec(spec)
sys.modules["run_french_eval"] = ev
spec.loader.exec_module(ev)


def dictionary():
    return Lexicon(
        [
            Form("je", "je", "PRO"),
            Form("le", "le", "ART"),
            Form("train", "train", "NOM"),
            Form("prends", "prendre", "VER", "ind:pre:1:s"),
            Form("regarde", "regarder", "VER", "ind:pre:1:s"),
            Form("parle", "parler", "VER", "ind:pre:1:s"),
            Form("emprunte", "emprunter", "VER", "ind:pre:1:s"),
            Form("emprunter", "emprunter", "VER", "inf"),
            Form("empruntais", "emprunter", "VER", "ind:imp:1:s"),
        ],
        [],
    )


def learned(*surfaces):
    lex = dictionary()
    k = Knowledge()
    for surface in surfaces:
        k.learn(surface, lex, 1, "private card text for " + surface)
    return k


KNOWN = ("je", "le", "train", "regarde", "parle")


def candidate(**changes):
    c = Candidate(
        "Je emprunte le train.",
        "emprunte",
        "I borrow the train.",
        "borrow",
        "word",
        "",
        (),
        ("ind:pre:1:s",),
        (),
    )
    return Candidate(**{**asdict(c), **changes})


def target():
    return Target("emprunter", "VER", "borrow", "vocabulary", "dictionary", 1, 10)


def review(c, t, approved=True, alternatives=None):
    return {
        "key": t.key,
        "fingerprint": candidate_hash(c),
        "approved": approved,
        "reason": "checked",
        "search_summary": "checked takes versus borrows",
        "context_checks": [
            {
                "example": 0,
                "sufficient": True,
                "clues": ["le train"],
                "reason": "French situational clue.",
            }
        ],
        "alternatives": alternatives
        or [
            {
                "example": 0,
                "answer": "prends",
                "fits_context": False,
                "fits_cloze": False,
                "reason": "different meaning",
            }
        ],
    }


class Scripted:
    """Answers writer calls with `writer` and audit calls with `audit`."""

    def __init__(self, writer=None, audit=None, error=None):
        self.writer = writer
        self.audit = audit
        self.error = error
        self.prompts = []

    def check_login(self):
        pass

    def request(self, prompt, schema):
        self.prompts.append(schema)
        if self.error:
            raise self.error
        reply = self.writer if schema is SCHEMA else self.audit
        return json.loads(json.dumps(reply() if callable(reply) else reply))


def proposals(t, *cands, reason=""):
    return {
        "proposals": [
            {"key": t.key, "reason": reason, "candidates": [asdict(c) for c in cands]}
        ]
    }


def gen_case(t=None, **extra):
    t = t or target()
    return {
        "id": "fr-gen-test",
        "kind": "generate",
        "group": "g",
        "target": asdict(t),
        "label": "kept",
        "why": "test",
        "evidence": [],
        **extra,
    }


def run(case, client, known=None):
    return ev.run_case(
        case,
        1,
        known or learned(*KNOWN),
        dictionary(),
        {"provider": "claude"},
        Event(),
        ev.CountingClient(client, ev.Counter()),
    )


# Outcome classification


def test_approved_case_passes_every_check():
    c, t = candidate(), target()
    row = run(gen_case(), Scripted(proposals(t, c), {"reviews": [review(c, t)]}))
    assert row["outcome"] == "approved" and row["pass"]
    assert all(row["checks"].values())
    assert row["calls_made"] == 2
    assert row["draft"]["fields"]["Text"] == "Je {{c1::emprunte}} le train."


def test_refusal_is_a_failure():
    t = target()
    row = run(gen_case(), Scripted(proposals(t, reason="No oath context.")))
    assert row["outcome"] == "refused" and not row["pass"]
    assert row["failure_reason"] == "writer refused"
    assert row["calls_made"] == 3


def test_validation_rejection_bucket_drops_the_variable_tail():
    c, t = candidate(), target()
    row = run(gen_case(), Scripted(proposals(t, c)), learned("je", "regarde", "parle"))
    assert row["outcome"] == "validation_rejected"
    assert row["failure_reason"] == "Unlearned context"


def test_audit_rejection():
    c, t = candidate(), target()
    row = run(
        gen_case(),
        Scripted(proposals(t, c), {"reviews": [review(c, t, approved=False)]}),
    )
    assert row["outcome"] == "audit_rejected" and not row["pass"]
    assert row["failure_reason"] == "audit rejected"


def test_timeout_and_cli_error():
    row = run(gen_case(), Scripted(error=TimeoutError("claude generation timed out")))
    assert row["outcome"] == "timeout" and row["failure_reason"] == "timeout"
    assert row["calls"][0]["status"] == "timeout" and row["calls"][0]["real"]
    row = run(gen_case(), Scripted(error=RuntimeError("claude exited with 1")))
    assert row["outcome"] == "error" and row["failure_reason"] == "RuntimeError"
    assert row["error"] and not row["pass"]


def test_unexpected_exception_is_a_crash_with_traceback():
    row = run(gen_case(), Scripted(error=KeyError("boom")))
    assert row["outcome"] == "crash" and "Traceback" in row["error"]


# Format checks


def draft(c, t, alternatives=None):
    return Draft(t, c, {"review": review(c, t, alternatives=alternatives)})


def test_one_cloze_and_stem_length():
    known, lex, t = learned(*KNOWN), dictionary(), target()
    checks, _, fields = ev.grade(draft(candidate(), t), known, lex)
    assert checks["one_cloze_per_example"] and checks["stem_hides_two_letters"]
    stem = candidate(mode="stem", explanation="empr")
    checks, _, fields = ev.grade(draft(stem, t), known, lex)
    assert fields["Text"] == "Je empr{{c1::unte}} le train."
    assert checks["stem_hides_two_letters"] and checks["one_cloze_per_example"]


def test_short_stem_fails_validation_and_stem_check():
    known, lex, t = learned(*KNOWN), dictionary(), target()
    stem = candidate(mode="stem", explanation="emprunt")
    checks, _, _ = ev.grade(draft(stem, t), known, lex)
    assert not checks["validates"] and not checks["stem_hides_two_letters"]


def test_conjugation_requires_a_table():
    lex = dictionary()
    known = learned(*KNOWN, "emprunte")
    form = lex.analyses("empruntais")[0]
    t = Target("emprunter", "VER", "established", "conjugation", "x", 1, 1, form.key)
    c = candidate(
        sentence="Je empruntais le train.",
        answer="empruntais",
        translation="I used to borrow the train.",
        cue="emprunter",
        grammar=(),
    )
    checks, _, fields = ev.grade(draft(c, t), known, lex)
    assert checks["validates"]
    assert not checks["conjugation_table_present"]
    lex.paradigms = {"emprunter": {"ind:imp:1:s": ["empruntais"]}}
    checks, _, fields = ev.grade(draft(c, t), known, lex)
    assert "<li>" in fields["Extra"] and checks["conjugation_table_present"]
    # The infinitive cue is part of fits_cloze for conjugation targets.
    excused = [
        {
            "example": 0,
            "answer": "empruntai",
            "fits_context": True,
            "fits_cloze": False,
            "reason": "tense is fixed by the cue",
        }
    ]
    checks, disagreements, _ = ev.grade(draft(c, t, excused), known, lex)
    assert checks["no_fitting_alternative"] and disagreements == 0


def test_whole_word_vocabulary_blank_ignores_fits_cloze_excuse():
    known, lex, c, t = learned(*KNOWN), dictionary(), candidate(), target()
    fitting = [
        {
            "example": 0,
            "answer": "prends",
            "fits_context": True,
            "fits_cloze": False,
            "reason": "the translation says borrow",
        },
        {
            "example": 0,
            "answer": "emprunte",
            "fits_context": True,
            "fits_cloze": True,
            "reason": "the intended answer itself",
        },
    ]
    checks, disagreements, _ = ev.grade(draft(c, t, fitting), known, lex)
    assert not checks["no_fitting_alternative"]
    assert disagreements == 1
    # A visible stem is a real cue, so the reviewer's fits_cloze stands.
    stem = candidate(mode="stem", explanation="empr")
    checks, disagreements, _ = ev.grade(draft(stem, t, fitting), known, lex)
    assert checks["no_fitting_alternative"] and disagreements == 0


def test_fitting_alternative_fails_an_approved_case():
    c, t = candidate(), target()
    alternatives = [
        {
            "example": 0,
            "answer": "prends",
            "fits_context": True,
            "fits_cloze": False,
            "reason": "excused by the translation",
        }
    ]
    row = run(
        gen_case(),
        Scripted(
            proposals(t, c), {"reviews": [review(c, t, alternatives=alternatives)]}
        ),
    )
    assert row["outcome"] == "approved" and not row["pass"]
    assert row["failure_reason"] == "check:no_fitting_alternative"
    assert row["fits_cloze_disagreements"] == 1


# Audit replay


def audit_case(label, c=None):
    t = target()
    return {
        **gen_case(t),
        "id": "fr-audit-test",
        "kind": "audit",
        "label": label,
        "candidate": json.loads(json.dumps(asdict(c or candidate()))),
    }


def test_replay_answers_the_writer_once_and_delegates_the_audit():
    c, t = candidate(), target()
    inner = Scripted(audit={"reviews": [review(c, t, approved=False)]})
    counter = ev.Counter()
    client = ev.ReplayClient(ev.CountingClient(inner, counter), t, asdict(c))
    first = client.request("w", SCHEMA)
    assert first["proposals"][0]["candidates"] == [json.loads(json.dumps(asdict(c)))]
    assert not client.last_real
    assert client.request("a", REVIEW)["reviews"][0]["approved"] is False
    assert client.last_real
    assert client.request("w", SCHEMA)["proposals"][0]["candidates"] == []
    assert inner.prompts == [REVIEW] and counter.value == 1


def test_audit_label_agreement():
    c, t = candidate(), target()
    rejecting = Scripted(audit={"reviews": [review(c, t, approved=False)]})
    approving = Scripted(audit={"reviews": [review(c, t)]})
    row = run(audit_case("rejected"), rejecting)
    assert row["outcome"] == "audit_rejected"
    assert row["verdict_matches_label"] and row["pass"]
    assert row["calls_made"] == 1
    assert [x["real"] for x in row["calls"]] == [False, True, False, False]
    row = run(audit_case("kept"), approving)
    assert row["outcome"] == "approved" and row["pass"] and row["calls_made"] == 1
    row = run(audit_case("rejected"), approving)
    assert not row["verdict_matches_label"] and not row["pass"]
    row = run(audit_case("kept"), rejecting)
    assert not row["pass"]


def test_fitting_alternative_counts_as_a_rejection_for_audit_cases():
    c, t = candidate(), target()
    fitting = [
        {
            "example": 0,
            "answer": "prends",
            "fits_context": True,
            "fits_cloze": False,
            "reason": "excused",
        }
    ]
    client = Scripted(audit={"reviews": [review(c, t, alternatives=fitting)]})
    row = run(audit_case("rejected"), client)
    assert row["outcome"] == "approved" and row["pass"]


# Privacy


def test_results_never_contain_evidence_or_knowledge_context(tmp_path):
    c, t = candidate(), target()
    row = run(gen_case(), Scripted(proposals(t, c), {"reviews": [review(c, t)]}))
    writer = ev.ResultWriter(tmp_path)
    writer.write(row)
    text = (tmp_path / "results.jsonl").read_text()
    assert "private card text" not in text
    assert '"evidence"' not in text and "Dependencies" not in text
    assert json.loads(text)["draft"]["review"]["approved"] is True


# Summary arithmetic


def row(case, run, passed, kind="generate", outcome=None):
    return {
        "case_id": case,
        "run": run,
        "kind": kind,
        "group": "g",
        "label": None,
        "why": "",
        "outcome": outcome or ("approved" if passed else "refused"),
        "pass": passed,
        "failure_reason": None if passed else "writer refused",
        "calls": [],
        "calls_made": 1,
        "elapsed_seconds": 1.0,
    }


def test_pass_metrics_and_missing_runs():
    rows = [
        row("a", 1, True),
        row("a", 2, True),
        row("b", 1, True),
        row("b", 2, False),
        row("c", 1, True),
        row("c", 2, False, outcome="not_run"),
        row("d", 1, False),
        row("d", 2, False),
    ]
    s = ev.summarize(rows, runs=2)
    g = s["generate"]
    assert g["attempted"] == 7 and s["not_run"] == 1
    assert g["pass@1"] == pytest.approx(4 / 7)
    assert g["pass^k"] == pytest.approx(1 / 4)
    assert g["pass@k"] == pytest.approx(3 / 4)
    assert g["yield"] == pytest.approx(4 / 7)
    assert s["incomplete_cases"] == ["c"]
    assert "| a |" in ev.summary_markdown(s)


def test_compare_reports_wins_and_regressions(tmp_path):
    old = ev.summarize([row("a", 1, False), row("b", 1, True), row("c", 1, True)], 1)
    new = ev.summarize([row("a", 1, True), row("b", 1, False), row("d", 1, True)], 1)
    for name, s in (("old", old), ("new", new)):
        (tmp_path / name).mkdir()
        (tmp_path / name / "summary.json").write_text(json.dumps(s))
    result, text = ev.compare(tmp_path / "old", tmp_path / "new")
    assert result["wins"] == ["a"] and result["regressions"] == ["b"]
    assert result["only_old"] == ["c"] and result["only_new"] == ["d"]
    assert (tmp_path / "new" / "compare.md").read_text() == text


# Knowledge cache


def test_knowledge_cache_round_trip(tmp_path):
    known = learned(*KNOWN, "emprunte")
    known.constructions.add("ne-pas")
    path = tmp_path / "knowledge.json"
    ev.save_knowledge(path, known, 42)
    loaded, info = ev.read_knowledge(path)
    assert loaded.version == known.version and info["day"] == 42
    assert loaded.surfaces == known.surfaces and loaded.forms == known.forms
    assert loaded.meanings == known.meanings
    assert loaded.identities == known.identities
    assert loaded.constructions == {"ne-pas"}


def test_resume_skips_recorded_case_runs(tmp_path):
    writer = ev.ResultWriter(tmp_path)
    writer.write(row("a", 1, True))
    writer.write(row("b", 1, False, outcome="not_run"))
    assert ev.ResultWriter(tmp_path).done == {("a", 1)}
