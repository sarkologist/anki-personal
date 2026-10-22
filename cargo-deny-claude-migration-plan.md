# cargo-deny repair: Codex Web → Claude subscription

Branch: `claude/cargo-deny-subscription`. Design and credentials: docs/cargo-deny-cloud.md.

- [x] Agent workflow (Claude read-only + JSON answer; commands in token-free steps)
- [x] Controller + tests
- [x] Controller workflow, docs
- [ ] Review, PR, merge
- [ ] User: `claude setup-token` → secret `CLAUDE_CODE_OAUTH_TOKEN`; delete
      `CODEX_CLOUD_AUTH_JSON`; revoke the PAT, then delete `CODEX_REPAIR_GITHUB_TOKEN`
- [ ] First supervised run (manual dispatch of cargo-deny-cloud.yml)
