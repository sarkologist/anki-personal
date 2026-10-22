# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import importlib.util
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))

spec = importlib.util.spec_from_file_location(
    "controller", Path(__file__).parents[1] / "cargo_deny_cloud.py"
)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class Gates(unittest.TestCase):
    def test_repository_api_route_has_no_trailing_slash(self):
        from unittest.mock import Mock, patch

        with patch.object(m, "command", return_value=Mock(stdout="{}")) as call:
            m.api("")
            self.assertEqual(call.call_args.args[2], f"repos/{m.REPO}")
            m.api("branches/main/protection")
            self.assertEqual(
                call.call_args.args[2], f"repos/{m.REPO}/branches/main/protection"
            )

    def test_dependency_scope(self):
        m.validate_changes([("M", "100644", "Cargo.lock")])
        for path in [
            "Cargo.toml",
            "rslib/Cargo.toml",
            "cargo/licenses.json",
            ".deny.toml",
            ".github/workflows/ci.yml",
            "build.rs",
            ".cargo/config.toml",
        ]:
            with self.assertRaises(ValueError):
                m.validate_changes([("M", "100644", path)])
        for status, mode in [("A", "100644"), ("D", "000000"), ("M", "120000")]:
            with self.assertRaises(ValueError):
                m.validate_changes([(status, mode, "Cargo.toml")])

    def test_review_bound_to_commit_and_base(self):
        report = {
            "head": "a" * 40,
            "base": "b" * 40,
            "verdict": "approve",
            "findings": [],
        }
        self.assertTrue(m.validate_review(report, "a" * 40, "b" * 40))
        for change in [
            {"head": "c" * 40},
            {"base": "c" * 40},
            {"verdict": "unknown"},
            {"findings": ["issue"]},
        ]:
            with self.assertRaises(ValueError):
                m.validate_review(report | change, "a" * 40, "b" * 40)
        self.assertFalse(
            m.validate_review(
                report | {"verdict": "request_changes", "findings": ["bad update"]},
                "a" * 40,
                "b" * 40,
            )
        )

    def test_ci_requires_every_job_success(self):
        jobs = [
            {"name": n, "status": "completed", "conclusion": "success"}
            for n in m.REQUIRED_JOBS
        ]
        self.assertTrue(m.ci_passes(jobs))
        self.assertFalse(m.ci_passes(jobs[:-1]))
        for conclusion in ["failure", "skipped", "neutral", "cancelled", None]:
            self.assertFalse(
                m.ci_passes(jobs[:-1] + [jobs[-1] | {"conclusion": conclusion}])
            )

    def test_trigger_rejects_forks_and_bot_loop(self):
        run = {
            "head_repository": {"full_name": m.REPO},
            "head_branch": "topic",
            "event": "pull_request",
            "conclusion": "failure",
        }
        self.assertTrue(m.eligible_run(run))
        self.assertFalse(
            m.eligible_run(run | {"head_repository": {"full_name": "stranger/fork"}})
        )
        for branch in ("claude/cargo-deny-123", "codex/cargo-deny-123"):
            self.assertFalse(m.eligible_run(run | {"head_branch": branch}))
        self.assertFalse(m.eligible_run(run | {"conclusion": "success"}))


class Protection(unittest.TestCase):
    def test_strict_protection_is_required(self):
        protection = {
            "required_status_checks": {
                "strict": True,
                "contexts": sorted(m.REQUIRED_JOBS),
            },
            "enforce_admins": {"enabled": True},
        }
        m.validate_protection(protection)
        for item in [
            {},
            protection | {"enforce_admins": {"enabled": False}},
            protection
            | {
                "required_status_checks": {
                    "strict": False,
                    "contexts": sorted(m.REQUIRED_JOBS),
                }
            },
            protection
            | {"required_status_checks": {"strict": True, "contexts": ["cargo-deny"]}},
        ]:
            with self.assertRaises(ValueError):
                m.validate_protection(item)


class PatchIntegration(unittest.TestCase):
    def test_real_git_patch_and_mode_rejection(self):
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            m.git("init", directory)
            m.git("config", "user.name", "Test", cwd=directory)
            m.git("config", "user.email", "test@example.invalid", cwd=directory)
            lock = Path(directory, "Cargo.lock")
            lock.write_text("version = 3\n")
            m.git("add", "Cargo.lock", cwd=directory)
            m.git(
                "-c", "core.hooksPath=/dev/null", "commit", "-m", "base", cwd=directory
            )
            lock.write_text("version = 4\n")
            patch = m.command("git", "diff", cwd=directory).stdout
            m.git("restore", "Cargo.lock", cwd=directory)
            m.apply_patch(directory, patch)
            self.assertEqual(
                m.staged_changes(directory), [("M", "100644", "Cargo.lock")]
            )
            m.validate_changes(m.staged_changes(directory))
            m.git("update-index", "--chmod=+x", "Cargo.lock", cwd=directory)
            with self.assertRaises(ValueError):
                m.validate_changes(m.staged_changes(directory))

    def test_read_only_review_rejects_implementation_diff(self):
        import tempfile
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as directory:
            m.git("init", directory)
            m.git("config", "user.name", "Test", cwd=directory)
            m.git("config", "user.email", "test@example.invalid", cwd=directory)
            lock = Path(directory, "Cargo.lock")
            lock.write_text("version = 3\n")
            m.git("add", ".", cwd=directory)
            m.git(
                "-c", "core.hooksPath=/dev/null", "commit", "-m", "base", cwd=directory
            )
            head = m.git("rev-parse", "HEAD", cwd=directory)
            lock.write_text("version = 4\n")
            diff = m.command("git", "diff", cwd=directory).stdout
            m.git("restore", "Cargo.lock", cwd=directory)
            with patch.object(m, "agent", return_value=diff):
                with self.assertRaises(ValueError):
                    m.review("test", head, head, directory)


class LockPolicy(unittest.TestCase):
    def lock(self, version="1.2.3", source=None, checksum="a" * 64):
        return {
            "version": 4,
            "package": [
                {
                    "name": "example",
                    "version": version,
                    "source": source
                    or "registry+https://github.com/rust-lang/crates.io-index",
                    "checksum": checksum,
                }
            ],
        }

    def test_patch_upgrade_allowed(self):
        m.validate_lock(self.lock(), self.lock("1.2.4", checksum="b" * 64))

    def test_unsafe_changes_rejected(self):
        import copy

        original = self.lock()
        candidates = [
            self.lock("1.2.2"),
            self.lock("1.3.0"),
            self.lock("1.2.4-beta.1"),
            self.lock(source="git+https://evil.invalid/repo"),
            self.lock(checksum="b" * 64),
        ]
        for key, value in [
            ("name", "new-package"),
            ("replace", "evil"),
            ("checksum", "invalid"),
        ]:
            changed = copy.deepcopy(original)
            changed["package"][0][key] = value
            candidates.append(changed)
        changed = copy.deepcopy(original)
        changed["package"].append(dict(changed["package"][0]))
        candidates.append(changed)
        for candidate in candidates:
            with self.subTest(candidate=candidate), self.assertRaises(ValueError):
                m.validate_lock(original, candidate)

    def test_nonregistry_packages_cannot_change(self):
        before = {"version": 4, "package": [{"name": "local", "version": "1.0.0"}]}
        after = {"version": 4, "package": [{"name": "local", "version": "1.0.1"}]}
        with self.assertRaises(ValueError):
            m.validate_lock(before, after)

    def test_version_references_follow_patch_updates(self):
        before = self.lock()
        before["package"].append(
            {"name": "local", "version": "1.0.0", "dependencies": ["example 1.2.3"]}
        )
        after = self.lock("1.2.4", checksum="b" * 64)
        after["package"].append(
            {"name": "local", "version": "1.0.0", "dependencies": ["example 1.2.4"]}
        )
        m.validate_lock(before, after)
        after["package"][1]["dependencies"] = []
        with self.assertRaises(ValueError):
            m.validate_lock(before, after)


class CredentialBoundary(unittest.TestCase):
    def test_only_github_commands_receive_installation_token(self):
        from unittest.mock import Mock, patch

        auth = Mock()
        auth.token.return_value = "installation-token"
        with patch.object(m, "AUTH", auth), patch.object(m.subprocess, "run") as run:
            run.return_value.returncode = 0
            with patch.dict(
                m.os.environ,
                {"GH_TOKEN": "personal", "CODEX_REPAIR_APP_PRIVATE_KEY": "private"},
            ):
                m.command("python3", "--version")
                self.assertNotIn("GH_TOKEN", run.call_args.kwargs["env"])
                self.assertNotIn(
                    "CODEX_REPAIR_APP_PRIVATE_KEY", run.call_args.kwargs["env"]
                )
                auth.token.assert_not_called()
                for executable in ("gh", "git"):
                    m.command(executable, "--version")
                    self.assertEqual(
                        run.call_args.kwargs["env"]["GH_TOKEN"], "installation-token"
                    )
                self.assertEqual(auth.token.call_count, 2)

    def test_dispatch_token_overrides_installation_token(self):
        from unittest.mock import Mock, patch

        auth = Mock()
        with patch.object(m, "AUTH", auth), patch.object(m.subprocess, "run") as run:
            run.return_value.returncode = 0
            m.command("gh", "api", "x", token="dispatch-token")
            self.assertEqual(run.call_args.kwargs["env"]["GH_TOKEN"], "dispatch-token")
            auth.token.assert_not_called()


class AgentRuns(unittest.TestCase):
    def run_entry(self, title, **change):
        return {
            "id": 7,
            "display_title": title,
            "path": f".github/workflows/{m.AGENT_WORKFLOW}",
            "head_branch": "main",
            "event": "workflow_dispatch",
        } | change

    def test_run_matched_by_exact_request(self):
        wanted = self.run_entry("cargo-deny agent req-1")
        runs = [self.run_entry("cargo-deny agent req-10", id=8), wanted]
        self.assertIs(m.find_run(runs, "req-1"), wanted)
        self.assertIsNone(m.find_run(runs[:1], "req-1"))

    def test_run_from_other_branch_or_event_ignored(self):
        for change in [{"head_branch": "evil"}, {"event": "push"}, {"path": "x.yml"}]:
            runs = [self.run_entry("cargo-deny agent req-1", **change)]
            self.assertIsNone(m.find_run(runs, "req-1"))

    def test_duplicate_runs_rejected(self):
        runs = [self.run_entry("cargo-deny agent req-1")] * 2
        with self.assertRaises(RuntimeError):
            m.find_run(runs, "req-1")

    def test_agent_dispatches_waits_and_downloads(self):
        from unittest.mock import patch

        diff = "diff --git a/Cargo.lock b/Cargo.lock\n"
        pending = self.run_entry("x", status="in_progress", html_url="u")
        calls = []

        def api(path, method="GET", body=None, token=None):
            calls.append((path, method, token))
            if path.endswith("/dispatches"):
                self.request = body["inputs"]["request_id"]
                self.assertEqual(body["inputs"]["base"], "b" * 40)
                return None
            if "/runs?" in path:
                return {
                    "workflow_runs": [
                        pending | {"display_title": f"cargo-deny agent {self.request}"}
                    ]
                }
            if len([c for c in calls if c[0] == path]) == 1:
                raise RuntimeError("gh api failed (exit 1)")
            return pending | {"status": "completed", "conclusion": "success"}

        def command(*args, **kwargs):
            Path(args[args.index("--dir") + 1], "agent.patch").write_text(diff)

        with (
            patch.object(m, "api", side_effect=api),
            patch.object(m, "command", side_effect=command),
            patch.object(m, "DISPATCH_TOKEN", "dispatch"),
            patch.object(m.time, "sleep"),
            patch.dict(m.os.environ, {"GITHUB_RUN_ID": "1"}),
        ):
            self.assertEqual(m.agent("repair", "a" * 40, "b" * 40, "fix"), diff)
        self.assertEqual(calls[0][2], "dispatch")
        self.assertTrue(all(token is None for _, _, token in calls[1:]))

    def test_agent_timeout_cancels_run(self):
        from unittest.mock import patch

        pending = self.run_entry("x", status="in_progress", html_url="u")
        calls = []

        def api(path, method="GET", body=None, token=None):
            calls.append((path, method, token))
            if path.endswith("/dispatches"):
                self.request = body["inputs"]["request_id"]
                return None
            if "/runs?" in path:
                return {
                    "workflow_runs": [
                        pending | {"display_title": f"cargo-deny agent {self.request}"}
                    ]
                }
            return pending

        clock = iter(range(0, 10_000_000, 1000))
        with (
            patch.object(m, "api", side_effect=api),
            patch.object(m, "DISPATCH_TOKEN", "dispatch"),
            patch.object(m.time, "sleep"),
            patch.object(m.time, "monotonic", side_effect=lambda: next(clock)),
            patch.dict(m.os.environ, {"GITHUB_RUN_ID": "1"}),
        ):
            with self.assertRaises(TimeoutError):
                m.agent("repair", "a" * 40, "b" * 40, "fix")
        self.assertEqual(calls[-1], ("actions/runs/7/cancel", "POST", "dispatch"))

    def test_patch_must_be_a_bounded_git_diff(self):
        m.validate_patch("diff --git a/Cargo.lock b/Cargo.lock\n")
        for patch in ["", "rm -rf /", "diff --git " + "x" * 2_000_000]:
            with self.assertRaises(RuntimeError):
                m.validate_patch(patch)


if __name__ == "__main__":
    unittest.main()
