# Cargo-deny repair with Claude

`cargo-deny-cloud.yml` listens for completed CI runs and checks whether the
`cargo-deny` job actually failed. Same-repository PR failures and main failures
start a repair against current main. Fork PRs and repair-branch failures cannot
start privileged work. Failures unique to an unrelated PR require manual repair;
this workflow does not import that PR's changes into main.

## Two workflows, two sets of credentials

A GitHub Actions job is one machine: everything running in it can read every secret
given to it. The repair is therefore split so the agent and the merge credentials
never share a job.

- **Controller** (`cargo-deny-cloud.yml`, `.github/scripts/cargo_deny_cloud.py`)
  holds the GitHub App key, which can push, open PRs, and merge. It never runs the
  agent, a build, or any code from the candidate change.
- **Agent** (`cargo-deny-agent.yml`) runs one Claude Code task per dispatch, in
  `repair` or `review` mode, against a given commit. Its only secret is
  `CLAUDE_CODE_OAUTH_TOKEN`; its `GITHUB_TOKEN` is read-only. It uploads a diff of
  Cargo.lock (repair) or the review report (review) as the `agent-patch` artifact,
  kept for one day.

The controller dispatches the agent with its job `GITHUB_TOKEN` (`actions: write`),
finds the run by a unique request ID in its run name, waits, and downloads the
patch. The patch is untrusted input to the gates below.

Claude cannot run anything. It runs with `--restricted --safe-mode`, the Read, Grep,
and Glob tools only, file access confined to the checkout, and `dontAsk` permissions,
and it answers in JSON against a fixed schema (`cargo_deny_agent.py schema`). Steps
without the token do the rest: one saves `cargo deny check advisories` output and
`git diff base head` under `.agent-evidence/` for Claude to read; another
(`cargo_deny_agent.py apply`) validates the answer, runs
`cargo update --package <name>@<from> --precise <to>` for each proposed upgrade or
writes the review report, and refuses to produce a diff if anything else changed.
So no crate code and no repository settings run beside the Claude token, and Claude's
raw output never reaches the public log. Builds and tests run in normal PR CI, which
must pass before merge.

## Gates

The controller applies the returned diff, opens a PR, enables the full macOS CI job,
and dispatches a **separate agent run** for adversarial review. The review must
identify the exact head and base commits. Only modifications to the existing
Cargo.lock are accepted. A deterministic gate allows stable forward patch upgrades of
existing crates.io packages, preserving package names, sources, counts, and dependency
edges apart from version references. Unchanged package versions must retain their
checksums. Manifest or license metadata changes, new dependencies, larger upgrades,
and non-registry updates require manual review. The gate compares every round against
the original main lockfile, preventing cumulative drift. This limits scope; it does
not prove a new crate release is trustworthy. Policy changes, new files, deleted
files, symlinks, source changes, and workflow changes stop automation for manual
attention.

There are at most three fix/review rounds, one hour per agent run, two hours per CI wait,
and six hours for the overall Actions job. Missing/malformed reports, failed
validation, stale commits, timeouts, and exhausted rounds leave the PR unmerged.
A merge requires all CI jobs to succeed, including minilints, format, cargo-deny,
and the full macOS check. The merge API receives the reviewed head SHA. A changed
main branch stops the merge; the controller also requires strict up-to-date branch
protection with all four required CI checks and admin enforcement, so GitHub closes
the base-update race. Tokens with a ruleset or branch-protection bypass must not be
used.

## Credentials and activation

Create these encrypted Actions secrets in **sarkologist/anki-personal**:

- `CLAUDE_CODE_OAUTH_TOKEN`: run `claude setup-token` (Pro, Max, Team, or
  Enterprise plan) and store the printed token. It lasts one year and bills runs to
  that subscription, sharing its usage limits. Regenerate it before expiry or after
  revoking it. Do not paste it into issues, prompts, logs, or repository files.
- `CARGO_DENY_APP_PRIVATE_KEY`: a PEM private key for a private GitHub App
  installed only on `sarkologist/anki-personal`. Set the App ID or client ID in
  repository variable `CARGO_DENY_APP_ID`. Grant Contents, Pull requests, and
  Issues read/write; Actions and Administration read-only. Metadata read is
  implicit. No workflow-write permission, organization permissions, webhook,
  user authorization, or branch-protection bypass is needed.

The controller signs a short-lived RS256 JWT with OpenSSL and requests installation
tokens restricted to this repository and those exact permissions. It renews tokens
with ten minutes remaining before each GitHub/Git operation, so multi-hour agent
runs do not leave it using an expired token. Old tokens are revoked after renewal and
the last token is revoked on normal exit. A revocation failure is reported without
response bodies; token expiry still limits lifetime. The private key is stored in a
temporary 0700 directory/0600 file, removed on normal exit; GitHub-hosted runner
teardown handles abrupt termination. Credentials are stripped from subprocess
environments.

The App private key remains a long-lived credential. Restrict the installation,
protect that secret, rotate keys when needed, and never give this App a bypass.

Dispatch the controller with `auth_only=true` to verify repository access, strict
protection reads, and a real token renewal/revocation without starting a repair.
This check does not exercise the Claude token or PR creation/merge permissions;
validate those on the next real repair.

Add `codex-cargo-deny[bot]@users.noreply.github.com` (the App's commit identity) to
the existing comma-separated `CONTRIBUTORS_BYPASS_EMAILS` repository variable,
preserving any existing entries.

Protect main with strict required checks named `minilints`, `format`, `cargo-deny`,
and `check (macos)`, and enable enforcement for administrators. The controller
fails closed if these protections are missing.

Use the controller's manual **Run workflow** action for a supervised run. Inspect the
Actions summary for agent run links and the repair PR. Review failures or exhausted
rounds require human intervention; no automatic retry resets the round budget of an
existing repair PR. The controller cancels an agent run that exceeds its hour; if the
controller itself is killed, check for a leftover agent run before retrying.

## Controller tests

Run `python3 -m unittest discover -s .github/scripts/tests`.
The controller and both workflows must be reviewed on main; privileged `workflow_run`
execution never checks out arbitrary failed-PR branches or executes their scripts.
