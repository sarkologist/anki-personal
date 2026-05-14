# Anki

[![Build Status](https://github.com/ankitects/anki/actions/workflows/ci.yml/badge.svg)](https://github.com/ankitects/anki/actions/workflows/ci.yml)
[![Documentation](https://img.shields.io/badge/docs-dev--docs.ankiweb.net-blue)](https://dev-docs.ankiweb.net)

This repo contains the source code for the computer version of
[Anki](https://apps.ankiweb.net).

# Personal Fork Notes

This checkout is a personal Anki build, not a clean mirror of the official
`ankitects/anki` repository.

At the time this note was written (2026-05-14), `main` had diverged from
`origin/main` at `5a9b54e9380c66e26391f0827867d2a728107836` (`Briefcase
Installer (#4629)`, 2026-05-05). After fetching `origin`, the local history
summarized below spanned 67 commits through `87472c64e` (`Fix legacy LaTeX
preview caching`, 2026-05-14), while the official branch had 21 newer commits
not yet integrated into this copy. The official remote is `origin`; the
personal fork remote is `upstream`.

The local-only history is mainly focused on:

- More robust MathJax, cloze, and LaTeX editing, including clozes inside
  MathJax, colored/highlighted clozes, aligned MathJax handling, legacy LaTeX
  previews, reviewer generated-card fixes, and scrolling to the active cloze.
- Rich-text editor workflow improvements, including a per-field undo stack,
  undoable agent proposals, safer paste/clipboard behavior, priority highlight
  controls, and notetype script/CSS execution in the editor.
- A local Codex editor agent add-on under `addons/editor_agent_pane/`, with
  Codex CLI integration, sanitized activity/proposal rendering, HTML diffs,
  editable instructions, model and folder controls, note image attachments,
  run logging, stop controls, and LaTeX preview support.
- Desktop polish for this local workflow, including webview recovery, guards
  around deleted Qt objects, persisted zoom levels, zoom shortcuts, and
  card/media check shortcuts.
- Personal workflow tooling and notes, including `AGENTS.md`, `CLAUDE.md`,
  `tools/cut-stable`, local analysis docs, and build tooling exclusions for
  `.claude/`.

# About

Anki is a spaced repetition program. Please see the [website](https://apps.ankiweb.net) to learn more.

This repo contains the source code for the computer version of
[Anki](https://apps.ankiweb.net).

## Getting Started

### Contributing

Want to contribute to Anki? Check out the [Contribution Guidelines](./docs/contributing.md).

For more information on building and developing, please see [Development](./docs/development.md).

#### Contributors

The following people have contributed to Anki: [CONTRIBUTORS](./CONTRIBUTORS)

### Anki Betas

If you'd like to try development builds of Anki but don't feel comfortable
building the code, please see [Anki betas](https://betas.ankiweb.net/).

## License

Anki's license: [LICENSE](./LICENSE)
