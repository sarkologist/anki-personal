# MathJax cold-load benchmark

Run from the repository root, passing either Anki's built web root or its
`js/` directory:

```sh
bench/run.sh /Users/sark/code/anki/out/qt/_aqt/data/web 5 > bench/baseline.json
```

Set `CHROME=/path/to/a/Chromium-family/binary` to override browser discovery.
Each iteration launches a new browser process with a fresh profile, serves
assets over loopback HTTP with `Cache-Control: no-store`, and measures both a
math and no-math first card. The page POSTs its results back to the harness
server (Chrome's `--virtual-time-budget`/`--dump-dom` mode deadlocks on
dynamically injected scripts, which the lazy cases rely on). No npm or Python
packages are needed.

`--cases` selects a comma-separated subset of `math`, `nomath`, `math-lazy`,
`nomath-lazy` (default `math,nomath`). The `*-lazy` cases replicate the lazy
reviewer flow shipped on this branch: no MathJax script tags; the page runs the
detection regex from `ts/reviewer/mathjax.ts` and, on a match, injects the
config and component scripts dynamically before typesetting. `nomath-lazy`
therefore measures the new no-math fast path (no MathJax fetch at all).

The JSON contains per-run observations and median/min/max summaries. Script
wall times cover fetch + parse + evaluation; Resource Timing fetch durations
and the residual wall time are also reported separately. Startup is measured
from navigation start through `MathJax.startup.promise`, and includes component
loads. First-card time is measured from navigation; `typeset_ms` isolates the
reviewer's `typesetClear()` + `typesetPromise([qa])` step. Byte totals come from
Navigation and Resource Timing `transferSize`; body bytes are retained as a
separate protocol-overhead-free check.

When an execution sandbox prohibits both TCP binds and Chrome subprocesses,
the explicit QtWebEngine fallback can still validate the timing logic:

```sh
bench/run-qt-cold.sh /Users/sark/code/anki/out/qt/_aqt/data/web 5
```

It uses a fresh process, no-cache profile, and fresh page per observation, but
a custom URL scheme instead of HTTP; its JSON labels the driver and reports
served body bytes only.

The page duplicates the reviewer's config/main classic-script order and its
MathJax await/typeset sequence. It omits QtWebEngine, the Qt bridge, reviewer
application code/CSS, card media, and add-ons; consequently it is a controlled
Chromium comparison benchmark, not a complete reviewer paint benchmark.

`--config FILE` replaces only `js/mathjax.js`; it is intended for diagnosing or
benchmarking a rebuilt config when the artifact tree's config is missing or
invalid. The JSON records the override path.
