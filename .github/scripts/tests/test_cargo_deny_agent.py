# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import importlib.util
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))

spec = importlib.util.spec_from_file_location(
    "agent", Path(__file__).parents[1] / "cargo_deny_agent.py"
)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

REGISTRY = "registry+https://github.com/rust-lang/crates.io-index"
LOCK = {
    "version": 4,
    "package": [
        {"name": "rustls", "version": "0.23.1", "source": REGISTRY},
        {"name": "local", "version": "1.0.0"},
    ],
}


def output(structured, **change):
    return {
        "is_error": False,
        "subtype": "success",
        "structured_output": structured,
    } | change


class Upgrades(unittest.TestCase):
    def test_valid_upgrade_accepted(self):
        result = output(
            {"upgrades": [{"name": "rustls", "from": "0.23.1", "to": "0.23.4"}]}
        )
        self.assertEqual(m.upgrades(result, LOCK), [("rustls", "0.23.1", "0.23.4")])

    def test_unsafe_requests_rejected(self):
        for upgrade in [
            {"name": "rustls --config x", "from": "0.23.1", "to": "0.23.4"},
            {"name": "rustls", "from": "0.23.1", "to": "0.23.4 --config x"},
            {"name": "rustls", "from": "0.23.1", "to": "0.23.1"},
            {"name": "rustls", "from": "0.23.0", "to": "0.23.4"},
            {"name": "local", "from": "1.0.0", "to": "1.0.1"},
            {"name": "missing", "from": "1.0.0", "to": "1.0.1"},
            {"name": "rustls", "from": "0.23.1", "to": "-0.23.4"},
        ]:
            with self.subTest(upgrade=upgrade), self.assertRaises(ValueError):
                m.upgrades(output({"upgrades": [upgrade]}), LOCK)

    def test_failed_or_malformed_run_rejected(self):
        good = {"upgrades": [{"name": "rustls", "from": "0.23.1", "to": "0.23.4"}]}
        for result in [
            output(good, is_error=True),
            output(good, subtype="error_max_turns"),
            output(None),
            output({"upgrades": []}),
            output({"upgrades": [good["upgrades"][0]] * 51}),
        ]:
            with self.subTest(result=result), self.assertRaises(ValueError):
                m.upgrades(result, LOCK)


OLD_SUM = "a" * 64
NEW_SUM = "b" * 64
LOCK_TEXT = f"""\
version = 4

[[package]]
name = "app"
version = "1.0.0"
dependencies = [
 "getopts",
 "md 0.13.4",
 "md 0.8.0",
]

[[package]]
name = "getopts"
version = "0.2.24"
source = "{REGISTRY}"
checksum = "{"c" * 64}"

[[package]]
name = "md"
version = "0.8.0"
source = "{REGISTRY}"
checksum = "{"d" * 64}"

[[package]]
name = "md"
version = "0.13.4"
source = "{REGISTRY}"
checksum = "{OLD_SUM}"
dependencies = [
 "getopts",
]
"""


class Bump(unittest.TestCase):
    def test_only_target_entry_and_its_references_change(self):
        text = m.bump(LOCK_TEXT, "md", "0.13.4", "0.13.5", NEW_SUM)
        self.assertEqual(
            text,
            LOCK_TEXT.replace('"md 0.13.4"', '"md 0.13.5"').replace(
                f'version = "0.13.4"\nsource = "{REGISTRY}"\nchecksum = "{OLD_SUM}"',
                f'version = "0.13.5"\nsource = "{REGISTRY}"\nchecksum = "{NEW_SUM}"',
            ),
        )

    def test_bare_references_kept_for_single_version(self):
        text = m.bump(LOCK_TEXT, "getopts", "0.2.24", "0.2.25", NEW_SUM)
        self.assertIn(' "getopts",\n', text)
        self.assertIn('version = "0.2.25"', text)
        self.assertNotIn("0.2.24", text)

    def test_existing_target_version_rejected(self):
        with self.assertRaises(ValueError):
            m.bump(LOCK_TEXT, "md", "0.8.0", "0.13.4", NEW_SUM)

    def test_missing_entry_rejected(self):
        with self.assertRaises(ValueError):
            m.bump(LOCK_TEXT, "md", "0.9.0", "0.9.1", NEW_SUM)


class IndexChecksum(unittest.TestCase):
    def entries(self, *entries):
        return "\n".join(json.dumps(e) for e in entries)

    def test_checksum_read_from_sparse_index(self):
        urls = []

        def fetch(url):
            urls.append(url)
            return self.entries(
                {"vers": "0.13.4", "cksum": OLD_SUM, "yanked": False},
                {"vers": "0.13.5", "cksum": NEW_SUM, "yanked": False},
            )

        self.assertEqual(m.index_checksum("Pulldown-cmark", "0.13.5", fetch), NEW_SUM)
        self.assertEqual(urls, ["https://index.crates.io/pu/ll/pulldown-cmark"])

    def test_short_name_paths(self):
        for name, path in [("a", "1/a"), ("ab", "2/ab"), ("abc", "3/a/abc")]:
            self.assertEqual(m.index_url(name), f"https://index.crates.io/{path}")

    def test_yanked_missing_or_malformed_rejected(self):
        for entries in [
            [{"vers": "0.13.5", "cksum": NEW_SUM, "yanked": True}],
            [{"vers": "0.13.4", "cksum": NEW_SUM, "yanked": False}],
            [{"vers": "0.13.5", "cksum": "zz", "yanked": False}],
        ]:
            with self.subTest(entries=entries), self.assertRaises(ValueError):
                m.index_checksum("md", "0.13.5", lambda _: self.entries(*entries))


class Review(unittest.TestCase):
    def test_report_bound_to_inputs(self):
        report = m.review_report(
            output({"verdict": "approve", "findings": []}), "a" * 40, "b" * 40
        )
        self.assertEqual(
            report,
            {"head": "a" * 40, "base": "b" * 40, "verdict": "approve", "findings": []},
        )

    def test_findings_bounded(self):
        report = m.review_report(
            output({"verdict": "request_changes", "findings": ["x" * 5000] * 50}),
            "a" * 40,
            "b" * 40,
        )
        self.assertLessEqual(len(json.dumps(report)), 20_000)

    def test_unknown_verdict_rejected(self):
        with self.assertRaises(ValueError):
            m.review_report(output({"verdict": "merge", "findings": []}), "a", "b")

    def test_verdict_must_agree_with_findings(self):
        for answer in [
            {"verdict": "approve", "findings": ["nit"]},
            {"verdict": "request_changes", "findings": []},
            {"verdict": "request_changes", "findings": ["  "]},
        ]:
            with self.subTest(answer=answer), self.assertRaises(ValueError):
                m.review_report(output(answer), "a", "b")
        report = m.review_report(
            output({"verdict": "request_changes", "findings": [" bad ", ""]}), "a", "b"
        )
        self.assertEqual(report["findings"], ["bad"])


class Explanation(unittest.TestCase):
    def test_explanation_shown_even_when_no_upgrades(self):
        import io
        from contextlib import redirect_stdout

        result = output({"upgrades": [], "explanation": "needs a minor bump"})
        stream = io.StringIO()
        with redirect_stdout(stream), self.assertRaises(ValueError):
            m.upgrades(result, LOCK)
        self.assertIn("needs a minor bump", stream.getvalue())


class FailureSummary(unittest.TestCase):
    def test_error_fields_shown_answer_hidden(self):
        error = {
            "is_error": True,
            "subtype": "success",
            "api_error_status": 401,
            "num_turns": 1,
            "result": "Invalid bearer token",
        }
        text = m.failure_summary(json.dumps(error))
        self.assertIn("401", text)
        self.assertIn("Invalid bearer token", text)
        answered = output({"upgrades": []}) | {"result": "private reasoning"}
        self.assertNotIn("private reasoning", m.failure_summary(json.dumps(answered)))

    def test_error_cannot_start_workflow_command_line(self):
        error = {"is_error": True, "result": "x\n::stop-commands::abc"}
        for line in m.failure_summary(json.dumps(error)).splitlines():
            self.assertFalse(line.startswith("::"))

    def test_non_json_output(self):
        self.assertIn("not JSON", m.failure_summary("Error: unknown option"))
        self.assertIn("no output", m.failure_summary(""))


class Schemas(unittest.TestCase):
    def test_schemas_are_json(self):
        for mode in ("repair", "review"):
            self.assertEqual(json.loads(m.schema(mode))["type"], "object")


if __name__ == "__main__":
    unittest.main()
