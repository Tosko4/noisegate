# Noisegate

Gate the noise. Keep the signal.

Noisegate is a standalone Hermes Agent plugin and CLI that compacts noisy
terminal and tool output before it floods the model context. It is deterministic:
no model calls, no summaries by vibes, just command classification, safe
bypasses, head/tail fallback, and machine-readable metadata.

One-line agent instruction:

```text
Install and enable Noisegate for Hermes, then stop dumping terminal walls into context; use NOISEGATE_BYPASS when exact bytes matter.
```

## Install For Hermes

From this checkout, install into the same Python environment that runs `hermes`:

```bash
HERMES_PYTHON="$(head -1 "$(command -v hermes)" | sed 's/^#!//')"
uv pip install --python "$HERMES_PYTHON" -e .
hermes plugins enable noisegate
```

From a built package:

```bash
HERMES_PYTHON="$(head -1 "$(command -v hermes)" | sed 's/^#!//')"
uv pip install --python "$HERMES_PYTHON" noisegate-hermes
hermes plugins enable noisegate
```

Hermes discovers the pip entry point `hermes_agent.plugins:noisegate`. The
plugin registers `transform_terminal_output` for full terminal output before
Hermes' own truncation, plus `transform_tool_result` as the generic safety net
before a tool result is appended to model context.

## CLI

```bash
noisegate wrap -- pytest -q
noisegate wrap --store-artifact -- pytest -q
noisegate wrap --raw -- cat exact-output.txt
noisegate reduce --command "pytest" < noisy.log
noisegate reduce-json < hermes-tool-result.json
noisegate doctor
noisegate cat ng_<artifact-id>
noisegate cat --artifact-dir /tmp/noisegate-artifacts ng_<artifact-id>
noisegate artifacts list
noisegate artifacts stats
noisegate artifacts verify
```

`wrap` runs a command without a shell, captures stdout and stderr, writes the
compacted result to stdout, and exits with the wrapped command's exit code. It
captures up to 4 MiB per stream by default; use `--max-capture-bytes <n>` to
change that. If capture is truncated, Noisegate adds a
`[noisegate: capture truncated]` marker to the captured stream.

Use `--raw` or `--full` to bypass reducer compaction for a wrapped command while
still keeping the text capture cap:

```bash
noisegate wrap --raw -- pytest -q
noisegate wrap --full -- ./script-that-must-stay-exact
```

`wrap` is a bounded text-capture surface: it decodes captured bytes as UTF-8
with replacement and may truncate at the capture boundary. Use a higher
`--max-capture-bytes` or run the command directly when byte-perfect terminal
replay matters.

If compaction itself errors, `wrap` fails open and prints the captured text
unchanged.

`reduce` reads stdin and writes compacted text. `reduce-json` accepts either a
Hermes-like envelope with a `result` string or a direct JSON tool result. Bad
JSON fails open and is written back unchanged.

## What It Compacts

Noisegate recognizes terminal-like Hermes results from `terminal` and
`execute_code`, plus generic JSON tool results with long string fields such as
`stdout`, `stderr`, `output`, `text`, or `logs`.

Built-in reducers cover:

- `git status` and `git log`
- `pytest` and `unittest`
- `npm`, `pnpm`, and `yarn`
- `rg`, `grep`, `ag`, and `ack`
- Docker build-style logs
- generic long output through deterministic head/tail compaction

`git diff` and patch/file-content tools are protected by default. Noisegate
does not rewrite `read_file`, `write_file`, `patch`, `apply_patch`, or similar
exact-content results.

## Bypass

Use any of these when the next tool result must stay exact:

```text
NOISEGATE_BYPASS
NOISEGATE_RAW
[noisegate:bypass]
[noisegate:raw]
```

Environment flags:

```bash
NOISEGATE_DISABLE=1      # turn compaction off
NOISEGATE_ARTIFACTS=1    # opt in to private raw-output artifacts
```

Hook options use the `noisegate_` prefix, for example
`noisegate_max_chars`, `noisegate_head_lines`, `noisegate_tail_lines`, and
`noisegate_mode=off`.

## Artifacts

Raw terminal output is not stored by default.

When artifact mode is enabled, Noisegate writes the original output to a private
filesystem store:

- directory mode `0700`
- file mode `0600`
- default size cap of 1,000,000 bytes
- artifact IDs are content-addressed (`ng_<sha256-prefix>`)
- IDs are validated, paths stay contained, and symlink traversal is rejected

The compacted result includes the artifact ID and sha256 so a human can retrieve
it later with `noisegate cat <artifact-id>`.

Artifact inspection commands:

```bash
noisegate artifacts list --artifact-dir /tmp/noisegate-artifacts
noisegate artifacts stats --artifact-dir /tmp/noisegate-artifacts
noisegate artifacts verify --artifact-dir /tmp/noisegate-artifacts
```

`verify` recomputes sha256 prefixes from the stored files and returns a non-zero
exit code if an artifact was tampered with or has an invalid private-store path.

## Compatibility

Noisegate is designed as a small, standalone Hermes Agent plugin. It does one
job: compact noisy tool output before it is appended to model context.

If you run additional context, memory, or transcript layers around Hermes, treat
Noisegate as an inline compaction step rather than a storage system. Downstream
layers will usually see the compacted result. When exact raw output needs to be
recoverable, enable Noisegate artifact mode for that run.

## Safety Defaults

- Fail open: hook errors and bad JSON leave the original result alone.
- Exact reads, writes, patches, and diffs are preserved by default.
- Raw output storage is opt-in.
- Artifact storage uses private permissions, size limits, containment checks,
  and symlink rejection.
- No adapters for unrelated hosts are included. Noisegate targets Hermes Agent.

## Attribution

Noisegate was informed by the MIT-licensed Tokenjuice project, especially its
ideas around deterministic reducers, command classification, safe bypasses,
artifact opt-in, and machine-readable metadata. This package is a fresh Python
implementation focused on Hermes Agent.
