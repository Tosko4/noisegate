# Changelog

All notable changes to Noisegate are documented here. Release notes are generated from this file.

## [Unreleased]

### Added
- `noisegate reduce`, `reduce-json`, and `wrap` now accept `--metadata`/`--debug` to print diagnostic JSON to stderr without changing stdout.
- Regression coverage now defines the `execute_code` print-output boundary across JSON, source, config, diff, test, web excerpt, dependency, package-manager, and invalid-text payloads.

### Changed
- `noisegate install-hermes --dry-run` now explicitly states that install/enable/doctor commands are not run and no Hermes restart/reload is performed.
- Unchanged reduction metadata now reports more specific reason codes and the attempted reducer when a reducer could not produce a safe smaller output.
- `noisegate doctor` now prints the effective runtime config, artifact directory, and artifact size cap alongside environment warnings.

### Fixed
- `install-hermes` now rejects non-Hermes Python launchers instead of trusting any virtualenv Python shebang, and it validates shell shims that exec Python are actually invoking `hermes_cli`.
- The Hermes config helper now avoids rewriting an already-correct plugin config while still removing stale `noisegate` entries from `plugins.disabled`.

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
