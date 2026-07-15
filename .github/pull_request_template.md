<!-- Use these sections proportionally. For a tiny docs or test-only change, omit or mark clearly irrelevant sections rather than inventing filler. Maintainers care about enough evidence for the actual risk, not template compliance. -->

## Why

<!-- Describe the real problem, its impact, and the related issue. -->

## What changed

<!-- Explain the approach, important boundaries, and anything deliberately left out. -->

## Validation

<!-- List the exact commands you ran and their outcomes. Do not write "tests pass" without evidence. -->

```text
command -> result
```

## Safety and compatibility

<!-- Use this section when the change affects exact-output, fail-open, artifacts, Hermes, platforms, installers, or releases. Otherwise omit it. -->

## Docs

<!-- Use this section when docs changed or the docs impact needs explanation. Otherwise omit it. -->

## AI assistance

<!-- Use this section when substantial AI assistance was involved. Name the tool and what it helped with. You remain responsible for the full contribution. -->

## Checklist

- [ ] I read `CONTRIBUTING.md`, `AGENTS.md`, and `docs/product-contract.md`.
- [ ] This change fits Noisegate's Hermes-first compaction mission.
- [ ] The diff is focused and contains no unrelated cleanup.
- [ ] I added or updated tests for behavior changes.
- [ ] I ran validation proportionate to the change and identified any relevant required check I could not run; I understand the full gate must pass before merge.
- [ ] I checked whether README or operator docs need an update.
- [ ] I inspected the diff for secrets, private data, raw logs, and generated artifacts.
- [ ] I reviewed and understand all AI-assisted code or text in this pull request.
