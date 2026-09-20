# Cargo-deny repair with Codex Web

`cargo-deny-cloud.yml` listens for completed CI runs and checks whether the
`cargo-deny` job actually failed. Same-repository PR failures and main failures
start a repair against current main. Fork PRs and repair-branch failures cannot
start privileged work. Failures unique to an unrelated PR require manual repair;
this workflow does not import that PR's changes into main.

The controller submits repair tasks to the Codex Web environment
`anki-cargo-deny` (`6aafb234eb488191ad79c7c7cf35b709`). It applies returned diffs,
opens a PR, enables the full macOS CI job, and submits a **separate cloud task**
for adversarial review. The review must identify the exact head and base commits.
The runner does not execute generated code or expose GitHub credentials to the
cloud agent. Only modifications to existing Cargo manifests, Cargo.lock, and generated cargo/licenses.json are
accepted. Policy changes, new files, deleted files, symlinks, source changes, and
workflow changes stop automation for manual attention.

There are at most three fix/review rounds, two hours per cloud task or CI wait,
and six hours for the overall Actions job. Missing/malformed reports, failed
validation, stale commits, timeouts, and exhausted rounds leave the PR unmerged.
A merge requires all CI jobs to succeed, including minilints, format, cargo-deny,
and the full macOS check. The merge API receives the reviewed head SHA. A changed
main branch stops the merge; the controller also requires strict up-to-date branch protection with all four
required CI checks and admin enforcement, so GitHub closes the base-update race.
Tokens with a ruleset or branch-protection bypass must not be used.

## Credentials and activation

Create these encrypted Actions secrets in **sarkologist/anki-personal**:

- `CODEX_CLOUD_AUTH_JSON`: a dedicated ChatGPT-authenticated Codex CLI `auth.json`.
  API keys do not authenticate Codex Web. This is an account credential: use a
  dedicated account with access to this environment. Do not paste it into issues,
  task prompts, logs, or repository files. The workflow writes it to a private
  temporary directory and deletes the directory afterward.
- `CODEX_REPAIR_GITHUB_TOKEN`: preferably a fine-grained token restricted to this
  repository, with Contents and Pull requests read/write, Issues read/write
  (labels/comments), Actions read, and Administration read (to verify branch protection). It must be allowed to merge under the
  repository's branch rules. The built-in `GITHUB_TOKEN` is unsuitable because
  its PR events require manual workflow approval, and its label events do not trigger CI.

Codex session credentials can expire or rotate. A refreshed token in an ephemeral
runner is deliberately not written back into GitHub secrets; renew the secret
when authentication fails. Concurrent use of the same login elsewhere can also
require renewal. This is a limitation of using the experimental cloud CLI for
unattended Actions, not a permanent service-account API.

Add `codex-cargo-deny[bot]@users.noreply.github.com` to the existing comma-separated
`CONTRIBUTORS_BYPASS_EMAILS` repository variable, preserving any existing entries.
This registers the automation author with the repository's contributor check.

Protect main with strict required checks named `minilints`, `format`, `cargo-deny`,
and `check (macos)`, and enable enforcement for administrators. The controller
fails closed if these protections are missing.

The workflow becomes active when merged to main and credentials are installed.
Use its manual **Run workflow** action for the first supervised run. Inspect the
Actions summary for task links and the repair PR. Review failures or exhausted
rounds require human intervention; no automatic retry resets the round budget of
an existing repair PR. A timeout can leave a cloud task running: inspect/cancel
it before retrying. Cloud tasks do not have push or merge credentials.

## Cloud environment validation

The environment setup disables the unused blocked LLVM APT repository while
retaining signature checks and permits `plugins.dprint.dev`. It installs:

- `anki-cloud-run COMMAND ...`: appends only loopback hosts to existing uppercase
  and lowercase no-proxy settings, then runs the command.
- `anki-cloud-pytest [REPO]`: runs all pylib tests as an unprivileged user with the
  build's `ANKI_TEST_MODE=1`, using a copy of the required Python 3.13 runtime.

Run `anki-cloud-run ./check`, then `anki-cloud-pytest`. The root-only
`test_create_open` failure from the first command is acceptable only when the
entire unprivileged pylib suite passes and no other check fails. This does not
skip or weaken the test. The helper is tied to this repository's Python 3.13
runtime and must be updated/revalidated if that runtime changes. Do not widen
`/root` permissions. Local macOS CI runs unprivileged and needs neither helper.

The original validation passed all 342 Rust tests and all 90 pylib tests (twice).
The initial cargo-deny report identified rustls RUSTSEC-2026-0285 and yanked spin
versions. These findings are inputs for repair, not authorization to add ignores.

## Controller tests

Run `python3 -m unittest discover -s .github/scripts/tests`.
The controller and workflow must be reviewed on main; privileged `workflow_run`
execution never checks out arbitrary failed-PR branches or executes their scripts.
