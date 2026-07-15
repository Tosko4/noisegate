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

## Reviewing incoming pull requests

- Use `CONTRIBUTING.md` as a risk and evidence framework, not as a rigid form. A small docs fix does not need every template section; a reducer, artifact, installer, workflow, dependency, or release change needs correspondingly stronger evidence.
- Read the issue, description, complete base-to-head diff, commits, tests, docs, dependencies, and workflows before deciding whether the contribution belongs in Noisegate.
- Decide product fit before polishing implementation details. Reject unrelated integrations, speculative architecture, semantic-memory/logging expansion, or changes that weaken exact-output, fail-open, privacy, or Hermes-first boundaries even when the code looks polished.
- Treat all external PR code as untrusted. Check for unexpected network access, secret or environment reads, raw-output persistence, unsafe subprocess behavior, dependency or lockfile changes, workflow permission changes, publishing paths, obfuscation, and generated or vendored payloads. After read-only inspection, execute it only in an isolated, unprivileged, credential-free environment; never run fork code on a credential-bearing maintainer host or in a privileged workflow context.
- Do not infer bad intent merely from AI use, unfamiliar authorship, missing headings, or weak prose. Base security concerns on concrete behavior and evidence; move disclosure-sensitive findings to a private channel.
- Write one friendly, tailored review that explains what the PR actually does, what is already sufficient, what blocks acceptance, and the smallest evidence or change needed next. Separate blockers from optional suggestions. Do not paste a stock missing-section checklist or repeat boilerplate across contributors.
