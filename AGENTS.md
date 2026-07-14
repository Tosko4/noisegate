# Noisegate agent instructions

Noisegate is a standalone Hermes Agent plugin and CLI for deterministic terminal/tool-output compaction.

Read `CONTRIBUTING.md` before opening an issue, changing code, or preparing a pull request. It is the public contribution contract for people and agents; this file adds repository-specific execution guidance.

## Product scope

- Primary target: Hermes Agent.
- Must work without hermes-lcm.
- If hermes-lcm is present, behave compatibly and document exact semantics.
- Hindsight is not a raw output archive and must not be written to by Noisegate.
- Do not add compatibility adapters for unrelated tools/hosts unless explicitly requested.
- Build a real, installable, tested package — not a stub/demo.

## Safety defaults

- Fail open: if compaction errors, return the original output.
- Preserve exact-content reads and diffs by default.
- Do not store raw terminal output by default.
- Optional artifact storage must use private filesystem permissions, size limits, path containment, and clear docs.
- Never commit secrets, logs with secrets, environment files, auth/session data, or generated sensitive artifacts.

## Engineering expectations

- Use TDD for production behavior.
- Keep code small but complete: typed modules, tests, packaging, README, examples.
- Validate with the real test suite and package/install smoke tests.
- README should be human-readable and give agents a one-line install/use instruction.
