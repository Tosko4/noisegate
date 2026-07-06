# Changelog

All notable changes to Noisegate are documented here. Release notes are generated from this file.

## [Unreleased]

### Added
- `noisegate doctor` now reports invalid Noisegate environment values instead of silently falling back.
- CI now validates GitHub Actions workflow syntax and uses explicit job timeouts.

### Changed
- Contributor checks now ignore merge commits and wrap `git log` failures with a clearer troubleshooting message.

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
