# Noisegate product contract

Noisegate exists to improve agent context, not merely to make text shorter. This checklist is the maintainer contract for changes to reducers, hooks, artifacts, installers, and docs.

## Context value

- [ ] Compacted output must keep the next useful action visible: failing test name, package resolver error, Dockerfile line, command exit signal, permission error, or equivalent.
- [ ] A reducer that only saves tokens while hiding the root cause is a product regression.
- [ ] Success noise may be aggressively compacted when the retained head/tail and metadata still explain what ran.
- [ ] Failure output must prefer actionable lines over pretty summaries.

## Exact-output safety

- [ ] Source/content reads stay exact by default: `cat`, `sed -n`, `head`, `tail`, `nl`, `bat`, `jq`, and `yq`.
- [ ] Mixed source-plus-noisy commands stay exact when source is part of the command intent, for example `cat file && pytest`, `find -exec cat`, or `rg | xargs sed`.
- [ ] Diffs, patches, file reads, search/file tools, web extraction, skill docs, memory, Hindsight, LCM, MCP, and unknown future tools are protected.
- [ ] New broad tool classes are opt-in only. Unknown tools must fail protected, not compacted.

## Slop recognition

- [ ] Known noisy install/build/test surfaces are classified explicitly: `apt`/OS package maintenance, `pip`/`uv`/npm-family dependency installs, pytest, unittest, Docker builds, node test/build output, search listings, and inventory listings.
- [ ] Package-manager success logs can compact hard.
- [ ] Package-manager failures must keep resolver, lock, missing package, GPG, repository, peer dependency, and network/package lookup errors.
- [ ] Docker failures must keep the failing step, Dockerfile location, and `failed to solve` / exit-code line when present.

## Fail-open behavior

- [ ] Engine errors return the original text with fail-open metadata.
- [ ] Hermes hook errors return `None` so Hermes keeps the original result.
- [ ] Bad JSON, unsupported shapes, no-gain compaction, invalid budgets, and too-large metadata must preserve original output.
- [ ] Artifact notice/storage problems must not replace useful output with partial notices.

## Artifact boundary

- [ ] Artifacts are opt-in recovery handles, not a background raw-log archive.
- [ ] Artifact storage remains private: `0700` directory, `0600` files, content-addressed IDs, size cap, containment checks, and symlink rejection.
- [ ] `transform_terminal_output` must never persist pre-redaction terminal output.
- [ ] Docs must say raw artifacts are disabled by default and should be used deliberately.

## Hermes integration

- [ ] Hermes integration stays thin: `transform_terminal_output` and `transform_tool_result`, with no Hermes-LCM dependency and no broad host framework layer.
- [ ] The noisy Hermes tool allowlist stays narrow: `terminal`, `process`, `read_terminal`, and `browser_console`.
- [ ] Noisegate does not write to Hindsight, LCM, memory, or other semantic stores.

## Installer safety

- [ ] `install-hermes` resolves the real Hermes Python and rejects bare/system Python before install commands are built.
- [ ] Shell shims, env shebangs, variable expansion, and Windows launchers fail closed unless they resolve to a validated virtualenv Python.
- [ ] Installer subprocesses scrub `PYTHONHOME` and `PYTHONPATH`.
- [ ] Dry-run shows the exact install/enable/doctor commands before side effects.

## Scope limits

- [ ] No LLM summaries.
- [ ] No raw-output archive by default.
- [ ] No unrelated host/tool adapter framework.
- [ ] No semantic memory layer.
- [ ] No compaction of useful-context surfaces without a strict safety case and tests.

## Documentation honesty

- [ ] README/docs must state what Noisegate does and does not do.
- [ ] Docs must explain that compaction is deterministic and not lossless.
- [ ] Docs must tell operators when to use bypass/raw/full modes.
- [ ] New reducers, flags, artifact behavior, installer behavior, or safety boundaries require docs updates or a clear review note explaining why not.
