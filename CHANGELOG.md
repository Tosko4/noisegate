# Changelog

All notable changes to Noisegate are documented here. Release notes are generated from this file.

## [Unreleased]

### Added

- Conservative MCP policy coverage now locks `mcp_*` / `mcp__*` results, MCP exact-evidence categories, discovery metadata, and generic wrapper calls to exact-output defaults.

### Changed

- Generic `tool_call` wrappers now use the wrapped tool name when it is unambiguous, allowing wrapped terminal noise to compact while wrapped MCP/source tools and ambiguous ownership stay exact.
- Secret/header-looking raw output is refused for artifact storage even when artifact mode is enabled.

### Fixed

- Hermes background `process` poll/log/wait payloads now preserve process metadata while safely compacting command- or content-identified Docker, journalctl, `systemctl status`, dmesg, and follow-mode tail streams from known log targets; commandless non-diagnostic previews, followed source/config files, `systemctl show` properties, source, diff, and patch payloads remain exact.

## [0.2.0] - 2026-07-14

Noisegate 0.2.0 is a safety and signal-quality release. Compacted output keeps more of the details that help an agent recover from a failure, while source, patches, retrieval results, and other exact evidence are handled more conservatively. The goal is not simply shorter output; it is smaller output that remains trustworthy.

This release also hardens the boundary between Noisegate and Hermes. Installer target detection, JSON envelopes, command aliases, exit hints, and artifact decisions now have stricter regression coverage. No config migration is required, the noisy-tool allowlist has not been broadened, and raw artifact storage remains opt-in.

### Highlights

- **More useful failure output.** Tight budgets now favor assertion messages, tracebacks, chained exceptions, `BaseExceptionGroup` headers, concrete dependency-resolution errors, and other actionable anchors over repetitive progress lines.
- **Exact context stays exact.** Direct source reads, V4A patches, `git show REV:path`, attached `fd`/`fdfind` read consumers, protected `execute_code` output, and Hermes-LCM recovery references are preserved instead of being mistaken for noisy logs.
- **A safer Hermes boundary.** `reduce-json` now handles mixed and nested host envelopes, conflicting command aliases, structured `argv`, exit-status precedence, metadata-key collisions, and artifact no-gain paths without corrupting valid JSON or claiming recovery data that was not stored.
- **Better operator visibility.** Diagnostic metadata can be sent to stderr without changing stdout, and `noisegate doctor` exposes the effective runtime configuration and artifact limits more clearly.

### Added

- `noisegate reduce`, `reduce-json`, and `wrap` accept `--metadata` / `--debug` for out-of-band diagnostic JSON on stderr.
- Regression contracts define why `execute_code` remains protected by default and how command-aware routes can deliberately compact known terminal-style output instead.
- Product-contract and adversarial tests cover exact-output ownership, shell and package-runner wrappers, Docker/build noise, dependency failures, malformed host payloads, and recovery-handle preservation.

### Changed

- `noisegate install-hermes` now accepts only a real Hermes console script or supported Hermes shim inside a virtual environment. Ambiguous or unrelated Python launchers fail closed.
- `install-hermes --dry-run` explicitly reports that install, enable, doctor, and Hermes restart/reload actions are not performed.
- Plugin enablement is idempotent and removes a stale `noisegate` entry from `plugins.disabled` without rewriting an already-correct configuration.
- Unchanged reductions report more precise reason codes and attempted reducers; `noisegate doctor` now shows effective config, artifact location, size limits, and environment fallback warnings.

### Fixed

- Pytest and Python diagnostics retain the assertion, traceback, task, chained-exception, and exception-group details most likely to explain the failure under tight line or character budgets.
- `uv run pytest` distinguishes resolver failures that happen before pytest starts from resolver-like text printed by a real test run.
- Source-like content remains exact even when it contains strings such as `FAILED`, `ERROR`, `Traceback`, or `npm ERR!`; exact passthrough no longer depends on whether the content happens to resemble a log.
- Hermes-LCM `externalized_ref` values and recovery placeholders survive compaction without requiring Noisegate artifacts. If every distinct recovery handle cannot be retained, Noisegate fails open.
- Complete JSON envelopes make artifact decisions transactionally: no-gain paths write nothing, accepted recovery IDs resolve to the stored raw text, and multi-artifact envelopes avoid partial or orphaned writes.

### Documentation

- Clarified Noisegate's ground-up Hermes positioning and corrected the Tokenjuice attribution link used for background context.

## [0.1.2] - 2026-07-07

### Changed
- README install/update guidance is clearer and more human, with the canonical Hermes install command shown up front.
- npm package README now documents install and update behavior, dry-run usage, and the wrapper's relationship to the Python package.
- Release notes generation now includes update instructions, merged PRs by category, PR authors, release ranges, and first-time contributors since the previous release.
- PyPI package metadata now includes homepage, repository, issues, and changelog links.

### Fixed
- Release workflow reruns now update existing GitHub Release notes as well as replacing assets.
- Standalone PyPI publish retries now tolerate already-published immutable files.

## [0.1.1] - 2026-07-07

### Added
- `noisegate doctor` now reports invalid Noisegate environment values instead of silently falling back.
- CI now validates GitHub Actions workflow syntax and uses explicit job timeouts.
- `noisegate install-hermes` to install and enable Noisegate inside the same Python environment as Hermes, with a dry-run mode for safe operator review.
- PyPI trusted-publishing workflow foundation for `noisegate-hermes`.
- npm installer-wrapper package foundation for reserving `noisegate` and delegating to the Python package without postinstall scripts.
- npm trusted-publishing/provenance workflow foundation for the installer wrapper.

### Changed
- Contributor checks now ignore merge commits, normalize GitHub noreply author emails, resolve the `git` executable before release contributor checks, and wrap `git log` failures with clearer troubleshooting messages.
- README now presents PyPI as the canonical distribution path and documents the npm wrapper as installer-only.

### Security
- Pin GitHub Actions dependencies to reviewed commit SHAs to reduce mutable-tag supply-chain risk.

### Fixed
- Fail open for unusably tiny compaction budgets instead of emitting marker-only output that can obscure the original command result.
- Handle concurrent same-content artifact writes without false collision errors.

## [0.1.0] - 2026-07-06

### Added
- Standalone Hermes Agent plugin and CLI for deterministic terminal/tool-output compaction.
- CI/CD automation for linting, tests, builds, package smoke tests, release metadata checks, and contributor checks.
- GitHub release workflow that bumps version files, promotes changelog notes, creates tags, and publishes GitHub Releases with built artifacts.
- Release helper scripts for version consistency, changelog extraction, and contributor verification.
- Hermes hooks for `transform_terminal_output` and `transform_tool_result`.
- `noisegate wrap`, `reduce`, `reduce-json`, `doctor`, `cat`, and artifact inspection commands.
- Optional private artifact storage with validation, size limits, and symlink-safe containment.
- Exact-output protection for file reads, patch/diff output, context retrieval tools, MCP tools, web/search tools, and unknown future tools.

### Changed
- Hermes hook compaction now uses an explicit noisy-tool allowlist: `terminal`, `process`, `read_terminal`, and `browser_console`.
- Early terminal hook disables raw artifact storage because Hermes calls it before terminal redaction.
