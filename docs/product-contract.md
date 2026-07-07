# Noisegate product contract

Noisegate exists to improve agent context value, not to make output shorter at any cost. Use this checklist before changing reducers, Hermes hooks, artifact handling, installers, or operator docs.

## Maintainer checklist

### 1. Context value over byte-count wins

- [ ] The compacted output makes the next agent turn more useful, not merely smaller.
- [ ] Important failure lines, tracebacks, resolver conflicts, denied/not-found messages, and exit codes stay visible.
- [ ] Generic head/tail fallback is used only when no safer reducer applies.

### 2. Exact context stays exact

- [ ] File reads and source-like terminal commands stay byte-for-byte unchanged.
- [ ] Diffs, patches, code-search output, retrieved context, skills, memory, Hindsight, LCM, MCP, web extraction, and unknown future tools remain protected by default.
- [ ] If a command mixes source inspection with words like `ERROR` or `failed`, Noisegate does not reinterpret that source as a build/test failure.

### 3. Slop reducers stay deterministic and scoped

- [ ] Package/build/test reducers cover common agent slop: `apt`/`apt-get`, `pip`, `uv`, `npm`/`pnpm`/`yarn`, pytest/unittest, and Docker build-style logs.
- [ ] Docker runtime logs are only compacted when command intent is explicitly log-like, such as `docker logs` or `docker compose logs`; other Docker commands are not silently treated as build logs.
- [ ] No LLM summaries, semantic memory writes, or broad host-framework adapters are introduced.

### 4. Fail-open is real

- [ ] Any reducer/plugin/install helper exception preserves original output or exits with a controlled error before side effects.
- [ ] If metadata, omission notices, or recovery notices would exceed the budget or make JSON larger, the hook returns no change.
- [ ] Hook failures never block Hermes tool execution.

### 5. Artifacts are recovery, not an archive

- [ ] Raw artifacts stay off by default.
- [ ] Artifact mode remains explicit, private filesystem only, size-capped, path-contained, symlink-safe, and permissioned `0700`/`0600`.
- [ ] `transform_terminal_output` never stores pre-redaction raw output.
- [ ] Docs describe artifact privacy honestly and do not imply Noisegate is a log archive.

### 6. Hermes integration stays thin and safe

- [ ] Hermes hook compaction remains limited to noisy terminal-like surfaces: `terminal`, `process`, `read_terminal`, and `browser_console`.
- [ ] Useful-context tools are protected by name/prefix, and unknown future tools fail closed.
- [ ] Noisegate stays independent from Hermes-LCM and Hindsight.

### 7. install-hermes stays environment-safe

- [ ] The installer rejects bare/system Python interpreters and only installs into the actual Hermes virtualenv.
- [ ] Shell launchers, env assignments, and Windows launchers are resolved fail-closed.
- [ ] Installer subprocesses scrub caller `PYTHONHOME`/`PYTHONPATH`.
- [ ] Dry-run shows planned install/config/doctor commands before changing Hermes.

### 8. Docs match behavior

- [ ] README/operator docs say what Noisegate does and does not compact.
- [ ] Any new flag, env var, bypass marker, hook behavior, artifact behavior, installer behavior, or protected surface is documented.
- [ ] Boundaries are documented instead of hidden behind optimistic product claims.

If a proposed change cannot satisfy this checklist, keep it out of Noisegate or document the deliberate boundary explicitly.
