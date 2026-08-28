# Baseline config repair fixture

The main checkout's built `js/mathjax.js` was not runnable as the classic
script emitted by `AqtWebView.stdHtml()`: it contains a top-level ES-module
`import`, and the referenced `render-cache` output is absent. The adjacent
fixture is a benchmark-only IIFE bundle of the same checked-out source, made
with:

```sh
/Users/sark/code/anki/node_modules/.bin/esbuild ts/mathjax/index.ts \
  --bundle --minify --format=iife \
  --outfile=bench/fixtures/mathjax-config-bundled.js
```

Use it explicitly with `--config bench/fixtures/mathjax-config-bundled.js`.
The benchmark records this override in its JSON output.

SHA-256: `96e0846fc89ef8db5ac2c04c5064eb7ac332df234e5015f15499c623c705d000`.
