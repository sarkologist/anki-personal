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
cloud agent. Only modifications to the existing Cargo.lock are accepted. A deterministic gate
allows stable forward patch upgrades of existing crates.io packages, preserving
package names, sources, counts, and dependency edges apart from version references.
Unchanged package versions must retain their checksums. Manifest or license metadata
changes, new dependencies, larger upgrades, and non-registry updates require manual review.
The gate compares every round against the original main lockfile, preventing cumulative drift.
This limits scope; it does not prove a new crate release is trustworthy. Policy changes, new files, deleted files, symlinks, source changes, and
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
- `CODEX_REPAIR_APP_PRIVATE_KEY`: a PEM private key for a private GitHub App
  installed only on `sarkologist/anki-personal`. Set the App ID or client ID in
  repository variable `CODEX_REPAIR_APP_ID`. Grant Contents, Pull requests, and
  Issues read/write; Actions and Administration read-only. Metadata read is
  implicit. No workflow-write permission, organization permissions, webhook,
  user authorization, or branch-protection bypass is needed.

The trusted controller signs a short-lived RS256 JWT with OpenSSL and requests
installation tokens restricted to this repository and those exact permissions.
It renews tokens with ten minutes remaining before each GitHub/Git operation,
so multi-hour cloud tasks do not leave it using an expired token. Old tokens are
revoked after renewal and the last token is revoked on normal exit. A revocation
failure is reported without response bodies; token expiry still limits lifetime.
The private key is stored in a temporary 0700 directory/0600 file, removed on
normal exit; GitHub-hosted runner teardown handles abrupt termination. GitHub
credentials are stripped from Codex subprocess environments. Candidate code is
never run on the controller runner.

The App private key remains a long-lived credential. Restrict the installation,
protect that secret, rotate keys when needed, and never give this App a bypass.
An App does not isolate the personal ChatGPT account used by the Codex login.

For migration, install the App and save its variable/key before merging the
workflow change. There is no PAT fallback. Dispatch the workflow with
`auth_only=true` to verify repository access, strict protection reads, and a real
token renewal/revocation without launching a cloud task. This check does not
prove PR creation/merge permissions; validate those on the next real repair.
After successful replacement validation, revoke the dedicated PAT in GitHub's
personal token settings and remove `CODEX_REPAIR_GITHUB_TOKEN`. Removing a
repository secret alone does not revoke its underlying token.

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
