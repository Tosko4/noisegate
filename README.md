# Noisegate

<p align="center">
  <img src="assets/noisegate-hero.png" alt="Noisegate: Gate the noise. Keep the signal." width="100%">
</p>

<p align="center">
  <a href="https://github.com/Tosko4/noisegate/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/Tosko4/noisegate/actions/workflows/ci.yml/badge.svg"></a>
  <a href="https://github.com/Tosko4/noisegate/releases"><img alt="GitHub release" src="https://img.shields.io/github/v/release/Tosko4/noisegate?include_prereleases&sort=semver"></a>
  <a href="LICENSE"><img alt="License: MIT" src="https://img.shields.io/badge/license-MIT-black"></a>
  <img alt="Python 3.11+" src="https://img.shields.io/badge/python-3.11%2B-orange">
</p>

**Gate the noise. Keep the signal.**

Noisegate is a tiny, deterministic compaction layer for Hermes Agent. It catches the massive terminal walls, test logs, build spam, and search dumps before they flood your model context, while leaving exact content alone.

No model calls. No fuzzy summaries. No "trust me bro" compression.

Just predictable reducers, safe bypasses, protected outputs, and enough metadata to know what happened.

## The problem

Agents are great at running tools.

Tools are great at producing nonsense amounts of output.

A single test run, package install, Docker build, or search command can dump thousands of lines into context. Most of it is noise. Some of it matters. If you blindly truncate it, you lose the useful part. If you blindly keep it, you burn context and make the next model call worse.

Noisegate sits in the middle:

```text
noisy tool output  ->  Noisegate  ->  compact signal-rich result
```

It is built for agent work where the context window is valuable and exactness matters.

## What it does

Noisegate gives Hermes Agent two surfaces:

- a **Hermes plugin** that can compact noisy tool results before they enter the conversation
- a **CLI** you can use directly in terminals, scripts, CI jobs, and smoke tests

It knows how to reduce common noisy outputs:

- `pytest` and `unittest`
- `npm`, `pnpm`, and `yarn`
- `git status` and `git log`
- search output from `rg`, `grep`, `ag`, and `ack`
- Docker build-style logs
- generic long output with deterministic head/tail fallback

And it refuses to touch things that should stay exact:

- file reads
- patches and diffs
- skill documents
- memory, LCM, Hindsight, MCP, search, and web extraction results
- unknown future tools unless explicitly allowed

That last bit matters. A compactor that damages retrieved context is worse than no compactor.

## Install for Hermes

Noisegate is distributed as the Python package `noisegate-hermes`. The Hermes plugin must be installed into the same Python environment that runs `hermes`.

The easiest path is the installer command:

```bash
uvx --from noisegate-hermes noisegate install-hermes
```

That command:

1. finds the `hermes` launcher on `PATH`;
2. resolves the Hermes Python interpreter from either a Python console-script shebang or the official Hermes bash shim;
3. installs `noisegate-hermes` into that interpreter environment;
4. enables the `noisegate` Hermes entry-point plugin through Hermes config helpers;
5. runs `noisegate doctor` inside the Hermes Python environment.

Preview the exact commands without changing anything:

```bash
uvx --from noisegate-hermes noisegate install-hermes --dry-run
```

From a checkout, install that exact checkout into Hermes:

```bash
uv run noisegate install-hermes --package .
```

Manual fallback for older installs where `command -v hermes` is a Python console script:

```bash
HERMES_PYTHON="$(head -1 "$(command -v hermes)" | sed 's/^#!//')"
case "$(basename "$HERMES_PYTHON")" in
  python*|pypy*) ;;
  *) echo "Hermes launcher is not a Python console script; use noisegate install-hermes" >&2; exit 1 ;;
esac
HERMES_PREFIX="$(dirname "$(dirname "$HERMES_PYTHON")")"
if [ ! -f "$HERMES_PREFIX/pyvenv.cfg" ]; then
  echo "Hermes Python is not inside a virtual environment; use noisegate install-hermes" >&2
  exit 1
fi
uv pip install --python "$HERMES_PYTHON" noisegate-hermes
"$HERMES_PYTHON" - <<'PY'
from hermes_cli.config import load_config, save_config
cfg = load_config()
plugins = cfg.setdefault("plugins", {})
enabled = plugins.get("enabled") if isinstance(plugins.get("enabled"), list) else []
disabled = plugins.get("disabled") if isinstance(plugins.get("disabled"), list) else []
if "noisegate" not in enabled:
    enabled.append("noisegate")
plugins["enabled"] = enabled
plugins["disabled"] = [name for name in disabled if name != "noisegate"]
save_config(cfg)
PY
"$HERMES_PYTHON" -m noisegate.cli doctor
```

Noisegate registers two Hermes hooks:

```text
transform_terminal_output
transform_tool_result
```

### npm installer wrapper

The npm package `noisegate-hermes` is only a thin convenience installer. It is not the canonical implementation. It delegates to the Python package:

```bash
npx -p noisegate-hermes noisegate install-hermes
```

If your npm client does not resolve the single-bin shortcut, use:

```bash
npx -p noisegate-hermes noisegate-hermes-installer install-hermes
```

The npm package has no `postinstall` script and does not bundle the Python implementation.

### Publishing and package security

Noisegate uses release-cycle package publishing, not ad-hoc local tokens:

- PyPI package: `noisegate-hermes`
- npm package: `noisegate-hermes` installer wrapper
- the main release workflow publishes the GitHub Release, then PyPI, then npm
- npm publish waits until the matching `noisegate-hermes` version is visible on PyPI
- GitHub Actions publish with OIDC/trusted publishing where supported
- npm publish uses provenance (`npm publish --provenance`)
- no long-lived publish tokens in git or workflow files
- any emergency/recovery credentials belong in Keeper, not chat, git, logs, or CI output
- `main` is branch-protected; package releases must come from reviewed release-cycle changes

## Try it in 30 seconds

Run the health check:

```bash
noisegate doctor
```

Compact a noisy stream:

```bash
python - <<'PY' | noisegate reduce --command "pytest"
for i in range(300):
    print(f"collecting test line {i:03d}")
print("FAILED tests/test_example.py::test_signal")
print("AssertionError: expected signal, got noise")
PY
```

Wrap a command and keep its exit code:

```bash
noisegate wrap -- pytest -q
```

Bypass compaction when exact output matters:

```bash
NOISEGATE_BYPASS=1 noisegate reduce < exact-output.txt
noisegate wrap --raw -- ./script-that-must-stay-exact
noisegate wrap --full -- ./another-exact-command
```

## CLI reference

```bash
noisegate reduce --command "pytest" < noisy.log
noisegate reduce-json < hermes-tool-result.json
noisegate wrap -- pytest -q
noisegate wrap --store-artifact -- pytest -q
noisegate wrap --raw -- cat exact-output.txt
noisegate install-hermes --dry-run
noisegate doctor
noisegate cat ng_<artifact-id>
noisegate cat --artifact-dir /tmp/noisegate-artifacts ng_<artifact-id>
noisegate artifacts list --artifact-dir /tmp/noisegate-artifacts
noisegate artifacts stats --artifact-dir /tmp/noisegate-artifacts
noisegate artifacts verify --artifact-dir /tmp/noisegate-artifacts
```

`wrap` runs a command without a shell, captures stdout and stderr in arrival order, writes the compacted result to stdout, and exits with the wrapped command's exit code. It captures up to 4 MiB of combined output by default; use `--max-capture-bytes <n>` to change that.

If capture is truncated, Noisegate adds this marker:

```text
[noisegate: capture truncated]
```

`reduce-json` accepts either a Hermes-like envelope with a `result` string or a direct JSON tool result. Bad JSON fails open and is written back unchanged.

## For AI agents

If you are an AI agent working in this repo, read this first.

**One-line instruction:**

```text
Install and enable Noisegate for Hermes, then stop dumping terminal walls into context; use NOISEGATE_BYPASS when exact bytes matter.
```

Operational rules:

1. Use Noisegate for noisy terminal/tool output, not for exact source material.
2. Do not compact file reads, patches, diffs, retrieved context, skill docs, memory results, MCP results, or web extraction output.
3. Do not treat Noisegate as a raw-output archive. Raw artifacts are off by default.
4. Keep Hermes-LCM optional. Noisegate must work without it.
5. Do not write raw terminal output into Hindsight.
6. If compaction fails, preserve the original output.
7. Before committing, run the quality gate and scan the diff for secrets and personal/private data.

Safe smoke test for a lane or installation:

```bash
noisegate doctor
python - <<'PY' | noisegate reduce --command "pytest"
for i in range(260):
    print(f"line {i:03d}")
print("FAILED tests/test_demo.py::test_keeps_signal")
print("AssertionError: signal survived")
PY
printf '%s\n' '[noisegate:bypass]' 'line that must stay exact' | noisegate reduce --command "pytest"
```

Expected result:

- `doctor` reports a healthy package/plugin state
- long noisy output gets smaller
- the failure line stays visible
- bypass output stays unchanged
- no config writes, git writes, service restarts, or artifact writes happen unless explicitly requested
- install-hermes dry-runs show exact install/enable commands before changing Hermes

## Safety model

Noisegate is intentionally conservative.

For Hermes hook traffic, it compacts only this explicit allowlist:

```text
terminal
process
read_terminal
browser_console
```

Protected surfaces include:

```text
read_file
write_file
patch
apply_patch
skill_view
skill_manage
session_search
memory
hindsight_*
lcm_*
mcp_* / mcp__*
web_search
web_extract
execute_code
search_files
git diff / unified diffs
unknown future tools
```

Bypass controls:

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
NOISEGATE_ARTIFACT_DIR=/path/to/artifacts
NOISEGATE_ARTIFACT_SIZE_CAP=1000000
```

`noisegate doctor` reports ignored or fallback environment values, so typos like `NOISEGATE_ARTIFACTS=maybe` do not fail silently.

Hermes calls `transform_terminal_output` before its built-in terminal redaction pass. Noisegate still compacts inline terminal output there, but it disables raw artifact storage on that early hook so pre-redaction output is not persisted.

## Artifacts

Raw terminal output is not stored by default.

When artifact mode is enabled, Noisegate writes the original output to a private filesystem store:

- directory mode `0700`
- file mode `0600`
- default size cap of 1,000,000 bytes
- content-addressed IDs shaped like `ng_<sha256-prefix>`
- path containment and symlink traversal checks

Retrieve an artifact:

```bash
noisegate cat ng_<artifact-id>
```

Inspect the store:

```bash
noisegate artifacts list --artifact-dir /tmp/noisegate-artifacts
noisegate artifacts stats --artifact-dir /tmp/noisegate-artifacts
noisegate artifacts verify --artifact-dir /tmp/noisegate-artifacts
```

`verify` recomputes hashes and returns a non-zero exit code if an artifact was tampered with, has an invalid private-store path, or if a live temp artifact file is still present. Stale temp files created by interrupted writes are removed during verification/new writes without printing their raw contents.

## Hermes-LCM and memory layers

Noisegate is not Hermes-LCM and does not require Hermes-LCM.

If Hermes-LCM is installed, Noisegate acts as an inline compaction step before noisy output inflates active context. Downstream context or transcript layers usually see the compacted result.

Hindsight is semantic long-term memory, not a raw output bucket. Noisegate must not write raw terminal output into Hindsight.

## Development

Start clean:

```bash
git status --short --branch
git remote -v
```

Run the local quality gate:

```bash
uv run ruff check .
uv run python -m pytest -q
uv run python scripts/check_release.py
uv run python scripts/check_contributors.py
rm -rf dist
uv build
uvx twine check dist/*
git diff --check
```

Release helpers:

```bash
uv run python scripts/prepare_release.py 0.2.0
uv run python scripts/check_release.py --tag v0.2.0
uv run python scripts/build_release_notes.py v0.2.0 --output dist/release-notes.md
```

Release metadata must stay aligned across:

- `pyproject.toml` -> `[project].version`
- `noisegate/_version.py` -> `__version__`
- `noisegate/plugin.yaml` -> plugin manifest `version`
- `uv.lock` -> locked editable project version
- `npm/noisegate/package.json` -> npm wrapper version
- `npm/noisegate/package-lock.json` -> npm wrapper lockfile version

GitHub Actions run linting, tests, release metadata checks, contributor checks, package build, `twine check`, npm wrapper checks, npm dry-run packing, and wheel install smoke tests on Python 3.11, 3.12, and 3.13.

## What Noisegate is not

Noisegate is not a model summarizer.

It is not a replacement for logs.

It is not a database.

It is not a magic context engine.

It is a small gate in front of noisy agent output. That is the whole point.

## Attribution

Noisegate was informed by the MIT-licensed [Tokenjuice](https://github.com/denthought/tokenjuice) project, especially its ideas around deterministic reducers, command classification, safe bypasses, artifact opt-in, and machine-readable metadata.

Noisegate is a fresh Python implementation focused on Hermes Agent.

## License

MIT. See [LICENSE](LICENSE).
