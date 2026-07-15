# Contributing to Noisegate

Thanks for helping improve Noisegate.

Noisegate sits between noisy tool output and an agent's context window. Small changes can affect what an agent sees, what stays exact, and whether sensitive output is ever persisted. We therefore prefer focused, well-tested contributions over large speculative rewrites.

This guide is for people and coding agents. If an agent helps with your contribution, you are still responsible for every line submitted under your name.

## Start here

Before changing anything:

1. Read [README.md](README.md), [AGENTS.md](AGENTS.md), and the [product contract](docs/product-contract.md).
2. Search [open and closed issues](https://github.com/Tosko4/noisegate/issues) and [pull requests](https://github.com/Tosko4/noisegate/pulls) for related work.
3. Check that the change fits Noisegate's product scope.
4. For a feature, new integration, policy change, or broad refactor, open an issue before writing the implementation.
5. Keep one pull request focused on one problem.

Small bug fixes, regression tests, and clear documentation corrections may go straight to a pull request when the reason for the change is obvious.

## Product fit

Noisegate is a deterministic, Hermes-first plugin and CLI for safe terminal and tool-output compaction.

Changes are generally in scope when they improve:

- deterministic reduction of noisy terminal-like output;
- preservation of exact source, diffs, patches, retrieval results, and unknown tool output;
- fail-open behavior and artifact privacy;
- Hermes plugin integration without requiring Hermes-LCM;
- the CLI, installer, tests, packaging, release safety, or directly relevant documentation.

Changes are generally out of scope when they:

- turn Noisegate into an LLM summarizer, log database, raw-output archive, or semantic memory layer;
- add unrelated host or tool integrations;
- make Hermes-LCM, Hindsight, or another optional system a dependency;
- compact useful context without a narrow safety case;
- weaken exact-output protection, fail-open behavior, artifact privacy, or secret handling;
- add broad architecture or release churn unrelated to the reported problem.

If the product benefit is unclear, discuss it in an issue first. A working implementation is not automatically a good fit.

## Reporting an issue

A useful bug report includes:

- the Noisegate version and installation method;
- operating system and Python version;
- the command or Hermes tool shape involved;
- a minimal reproducible input with secrets removed;
- expected and actual output;
- whether exact output, inline compaction, or artifact mode was involved;
- relevant exit status and a short diagnostic excerpt.

Use synthetic fixtures whenever real output may contain credentials, private paths, personal data, or session content. Do not upload raw logs merely because they reproduce the problem.

Feature requests should explain the real workflow, why current behavior is insufficient, and why the change belongs in Noisegate rather than Hermes, Hermes-LCM, Hindsight, or another project.

Do not open batches of speculative, agent-generated issues. One verified problem with a reproducible case is worth more than twenty plausible guesses.

For a sensitive security issue, do not publish exploit details, secrets, or private output. Open a minimal public issue asking for a private reporting channel, without including technical details or a reproduction.

## Development setup

Noisegate requires Python 3.11 or newer. The project uses `uv` for Python dependencies and Node.js for the npm installer wrapper.

```bash
git clone https://github.com/Tosko4/noisegate.git
cd noisegate
uv sync --locked --group dev
uv run python -m pytest -q
```

Use a branch created from current `main`:

```bash
git fetch --prune origin
git switch main
git pull --ff-only origin main
git switch -c fix/short-description
```

Never put contribution work directly on `main`.

## Engineering rules

### Preserve the safety contract

Changes to reducers, command classification, hooks, artifacts, or installer behavior must preserve these defaults:

- failures return the original output or stop before side effects;
- source reads, searches, diffs, patches, retrieval results, MCP results, and unknown tools stay exact by default;
- raw artifacts remain opt-in, private, size-capped, path-contained, symlink-safe, and secret-aware;
- the early terminal hook never persists pre-redaction raw output;
- Noisegate works without Hermes-LCM and does not write raw output to Hindsight;
- compaction remains limited to explicitly supported noisy surfaces.

Read [docs/product-contract.md](docs/product-contract.md) before changing any of those boundaries.

### Use tests first for behavior changes

A bug fix should normally begin with a regression test that fails for the reported behavior and passes after the fix. Include paired exact-output and compactable-output cases when classification or shell reachability is involved.

Do not weaken, delete, or rewrite a test merely to make an implementation pass. If an existing expectation is wrong, explain the contract change in the issue and pull request.

During development, run the narrowest relevant tests often:

```bash
uv run python -m pytest -q tests/test_reducers.py
uv run python -m pytest -q tests/test_plugin_hook.py
```

Then run the full gate before requesting review.

### Keep the diff reviewable

- Fix the stated problem without drive-by refactors or formatting churn.
- Follow existing typed Python patterns and the configured Ruff rules.
- Add dependencies only when the benefit and maintenance cost are clear.
- Update user-facing docs when behavior, commands, flags, environment variables, hooks, protected surfaces, artifacts, installation, or packaging changes.
- Do not bump versions or prepare a release unless a maintainer asked for release work.
- Do not commit build output, raw logs, credentials, environment files, auth/session data, or generated sensitive artifacts.

## AI-assisted contributions

AI tools and coding agents are welcome. Unreviewed automation is not.

The contributor who opens the issue or pull request owns the contribution. Before asking maintainers to review it, you must:

- read and review the complete diff;
- understand the behavior and be able to explain it without handing the question back to an agent;
- verify factual claims, APIs, links, and compatibility assumptions against real sources or execution;
- run the tests you report and give the exact commands and results;
- check generated code and text for unnecessary churn, invented behavior, copied material, private data, and secrets;
- stay involved during review and make deliberate decisions about requested changes.

Disclose substantial AI assistance in the pull request description. Name the tool and describe what it helped with. You do not need to paste prompts or transcripts.

Example:

```text
AI assistance: Codex helped draft the regression tests and implementation.
I reviewed the full diff, verified the behavior against the linked issue, and
ran the validation commands listed above.
```

Do not use automation to:

- open speculative issues or bulk pull requests;
- claim work you have not investigated;
- publish review comments or maintainer replies you have not checked;
- fabricate test results, benchmarks, citations, or compatibility claims;
- submit code or documentation you cannot maintain and explain;
- turn a small issue into a broad rewrite because an agent suggested adjacent improvements.

Maintainers may close fully automated, duplicate, unverified, out-of-scope, or abandoned submissions without a detailed code review. This applies to low-quality work regardless of which tools produced it.

## Full local quality gate

Run this from the repository root before requesting review when your environment supports it. The complete gate must pass on the exact pull request head before merge, whether it is run by the contributor or a maintainer:

```bash
uv run ruff check .
uv run python -m pytest -q
uv run python scripts/check_release.py
uv run python scripts/check_contributors.py
(cd npm/noisegate && npm ci --ignore-scripts && npm test && npm pack --dry-run)
rm -rf dist
uv build
uvx twine check dist/*
git diff --check origin/main...HEAD
git diff --cached --check
git diff --check
```

For a tiny docs-only or metadata correction, you may request review after a proportionate subset when the omitted commands cannot exercise the changed behavior. List what you ran and identify any relevant required check you could not run; do not describe a partial gate as the full gate passing. Maintainers will still complete or verify the full pre-merge gate.

The contributor check compares commit authors with
[CONTRIBUTORS.md](CONTRIBUTORS.md). It normalizes GitHub noreply addresses to
their account login. If the check reports a missing contributor, add the exact
name it reports to `CONTRIBUTORS.md`.

If a command cannot run in your environment, say so plainly in the pull request. Do not report a partial or skipped gate as passing.

## Pull request requirements

A pull request should contain:

- a clear explanation of the problem and why it belongs in Noisegate;
- a focused implementation with no unrelated cleanup;
- a linked issue for non-trivial work;
- regression coverage for behavior changes;
- exact validation commands and outcomes;
- documentation updates when user-facing or operator behavior changes;
- safety and compatibility notes for exact-output, artifacts, hooks, installers, or release behavior;
- an AI-assistance disclosure when applicable.

These are evidence requirements, not mandatory headings. Use the pull request template when it helps reviewers find the information, but adapt it to the contribution. A typo fix may need only a short explanation and a docs check; a reducer, artifact, installer, workflow, dependency, or release change needs substantially more detail. Omitting an irrelevant section is fine. Omitting evidence needed to judge the actual risk is not.

A useful description follows this shape:

```markdown
## Why
Describe the real problem and its impact.

## What changed
Explain the approach and important boundaries.

## Validation
List exact commands and results.

## Safety and compatibility (when relevant)
Describe exact-output, fail-open, artifact, Hermes, and platform impact.

## Docs (when relevant)
List updated docs when behavior or operator guidance changes.

## AI assistance (when applicable)
Name the tool and scope when substantial AI assistance was used.
```

Before opening the pull request, inspect what reviewers will see:

```bash
git status --short --branch
git diff --check origin/main...HEAD
git diff --cached --check
git diff --check
git diff --stat origin/main...HEAD
git diff origin/main...HEAD
```

## Review and merge

CI must pass on the current pull request head. Maintainers may also run independent static, security, or AI-assisted review. You do not need to use a particular model or review tool yourself.

Reviews are proportional to the change. Maintainers will assess the contribution as a whole rather than reject it mechanically for a missing heading. When more information is needed, the review should identify the concrete gap, why it matters for this PR, and what would be sufficient to resolve it.

Treat review findings as claims to verify, not instructions to widen the pull request. Fix findings caused by or required for the current change. Report valid unrelated problems separately instead of folding them into the same diff.

Reply with concrete evidence: the changed file or test, the command run, and the result. Avoid raw agent transcripts and generic replies such as "fixed" or "should work now."

Maintainers decide product fit and merge readiness. Passing tests is required, but it does not override scope, safety, documentation, or maintainability concerns.

## License and contribution rights

By contributing, you agree that your work is submitted under the repository's [MIT License](LICENSE). Only submit code, text, fixtures, and other material that you have the right to contribute under those terms. AI assistance does not remove your responsibility for copyright, attribution, or license compatibility.
