# Lossless-recovery consumer contract

Status: Noisegate-owned semantic requirements and portable test vectors for
[issue #39](https://github.com/Tosko4/noisegate/issues/39). This document does not
define a Hermes API, implement the production consumer planned in
[issue #38](https://github.com/Tosko4/noisegate/issues/38), or define a recovery
provider.

## Normative rule

> Treat every recovery ref as protected structured metadata. After any lossy or final-budget stage, if any required ref is absent, malformed, or differs by even one byte, reject the compact candidate and use the complete canonical sanitized result with no recovery ref.

Noisegate must never synthesize, guess, repair, normalize, truncate, rewrite, or
emit a partial plausible ref. A ref appearing in ordinary model-visible text does
not satisfy this rule.

## Terms

**Canonical sanitized envelope** is the complete host result after all mandatory
redaction and sanitization. Its type, content, and canonical byte representation
are fixed before optional preservation. It contains no provider-added recovery
ref. The host retains this exact value outside every lossy or final-budget path so
it remains available for fallback.

**Model-visible copy** is the post-sanitization working copy on which an eligible
consumer such as Noisegate may perform a lossy transform. It initially represents
the canonical sanitized envelope, but it is not the host's retained fallback.

**Protected structured metadata** is a channel separate from compactable content.
Lossy transforms cannot summarize, edit, relocate, or budget its recovery-ref
entries. The host validates it after every lossy stage and again after any final
budget before delivering a candidate.

**Opaque recovery ref** is a provider-produced value whose canonical bytes can be
compared by the host. Noisegate can carry the value but assigns no syntax or
storage meaning to it and never dereferences it. The fixture pack uses conspicuous
UTF-8 strings only to test byte equality; those strings are not a proposed
provider format.

**Full-sanitized fallback** is the complete canonical sanitized envelope, selected
unchanged, with the provider-owned recovery metadata removed and no provider ref
spliced into visible text. “Full” never means pre-redaction bytes. A naturally
occurring ref-like string that was already part of the canonical envelope remains
ordinary exact content; it is not provider recovery metadata.

## Ordering boundary

The required lifecycle is semantic, not a plugin-load-order convention:

```text
tool result
→ host redaction and sanitization
→ canonical sanitized envelope retained by the host
→ optional provider-neutral preservation
→ verified publication of safe envelope plus opaque ref(s)
→ model-visible copy plus separately protected metadata
→ zero or more lossy consumer transforms
→ protected-metadata validation after each transform
→ optional final-budget stage
→ protected-metadata validation after the final budget
→ compact/unchanged candidate plus exact refs, or full-sanitized fallback with no ref
```

Preservation must occur after sanitization and before Noisegate or any other lossy
or final-budget stage. A provider must never be asked to persist pre-sanitization
bytes. A ref is not published until the provider can truthfully report that the
canonical sanitized envelope was atomically preserved under its own contract.

The full-sanitized fallback is a safety escape from the lossy pipeline. A host
cannot run that fallback through the same insufficient final budget and still
satisfy this contract; the complete canonical result takes precedence over the
candidate budget when integrity fails.

## Consumer permissions and prohibitions

Within the future recovery-aware seam, Noisegate may:

- read the post-sanitization model-visible copy and the ordinary tool context the
  host already permits for classification;
- transform only model-visible fields already eligible under Noisegate's product
  contract;
- carry an ordered sequence of opaque refs only in the protected metadata channel;
- preserve unrelated structured metadata without treating it as a recovery ref;
- return an eligible compact candidate with the complete exact protected-ref
  sequence, return an unchanged candidate for exact/protected/unknown tools, or
  reject/decline a candidate so the host selects fallback.

Noisegate must not:

- receive pre-redaction bytes through the recovery-aware seam, copy host
  sanitization logic, or persist input that sanitization removed;
- modify the host-retained canonical sanitized envelope or make it pass through a
  lossy reducer;
- parse a ref for provider, profile, session, path, expiry, authorization, or
  storage semantics;
- create, verify, dereference, refresh, revoke, expire, purge, recover, or otherwise
  manage refs;
- call or import a provider directly, depend on Hermes-LCM, use Hindsight as an
  archive, or depend on plugin ordering;
- turn recovery-looking ordinary text into protected metadata;
- return an exact subset when any required ref is invalid, even if the remaining
  refs appear usable.

Noisegate's existing early terminal hook currently has a separate pre-redaction
placement and therefore disables raw artifact storage. This design does not alter
that production hook. The future recovery consumer must use the new post-
sanitization seam rather than treating the early hook as the preservation
boundary.

## Ownership

| Layer | Owns | Does not own |
| --- | --- | --- |
| Host | Sanitization order; canonical fallback retention; optional provider invocation; separate metadata transport; consumer dispatch; validation after every loss/final-budget stage; final selection | Provider storage policy; Noisegate reduction policy; provider-specific schemas in core |
| Provider | Atomic preservation of the canonical sanitized envelope; verified ref publication; authorization; scope; capacity; retention; quota; garbage collection; purge/expiry truthfulness; bounded recovery | Host sanitization; lossy compaction; Noisegate configuration or artifacts |
| Consumer (Noisegate) | Eligibility and deterministic transformation of the model-visible copy; carrying protected refs without change; rejecting unsafe candidates; fixture and cross-plugin expectations | Canonical storage; ref lifecycle or recovery; host API naming; provider configuration |

The host remains the final integrity enforcer because later consumers and final
budgets occur outside Noisegate's control. Noisegate must nevertheless preserve
the same invariant internally so it never knowingly produces an unsafe candidate.

## Protected-ref invariants

1. After successful publication, the host pins one ordered sequence of distinct
   required refs for the result. Each required ref has exactly one occurrence.
2. Every ref is opaque. Equality is exact equality of the host contract's canonical
   bytes, not decoded, normalized, case-folded, parsed, or prefix equality.
3. Every candidate observation after a lossy stage and after the final budget must
   contain exactly the pinned sequence: same values, same order, same occurrence
   count, and no extra refs.
4. A duplicate occurrence of the same required ref is not silently deduplicated.
   It changes the occurrence sequence and rejects the candidate. Duplicate refs in
   the provider publication are unusable metadata and must not establish a required
   sequence.
5. A complete exact ref in compactable text is only text. A ref counts only in the
   protected structured-metadata slot.
6. Missing, reordered, duplicated, malformed, unverified, truncated, rewritten, or
   one-byte-mutated metadata rejects the whole candidate. Multiple refs are atomic
   for this decision; no partial set is returned.
7. Provider failure or unverifiable publication establishes no usable ref. The
   output is the full-sanitized fallback with no recovery metadata.
8. Fallback selects the retained canonical sanitized envelope byte-for-byte and
   carries an empty provider-ref sequence. It never embeds a recovery notice copied
   from a rejected candidate.
9. Noisegate never guesses a replacement from ordinary text, another ref, provider
   metadata, or a familiar-looking format.

These are sequence and occurrence semantics, not mathematical-set semantics. The
fixture validator intentionally rejects an observed `[A, A]` when the required
sequence is `[A]`, and accepts `[A, B]` only when the required sequence is exactly
`[A, B]`.

## Fail-open state machine

| State/observation | Required action |
| --- | --- |
| Canonical sanitized envelope unavailable | Do not enter preservation or lossless-recovery handling; retain the host's existing safe failure behavior. |
| Provider absent | Run ordinary standalone Noisegate behavior with no recovery metadata. Noisegate may compact eligible content or leave it exact under its current contract. |
| Provider available, preservation not requested or no ref published by design | Treat the result like provider-absent operation; never invent a ref. |
| Provider fails before verified publication | Select the complete canonical sanitized envelope with no ref; do not send a lossy candidate. |
| Provider metadata malformed, duplicate, or unverified | Select the complete canonical sanitized envelope with no ref. |
| Verified distinct ordered refs published | Pin their canonical bytes and build the model-visible candidate with refs in separate protected metadata. |
| Consumer returns no change for an exact/protected/unknown result | Keep the model-visible bytes unchanged and continue validating the exact protected-ref sequence. |
| Consumer error, invalid candidate, or candidate unavailable | Reject the candidate and select the complete canonical sanitized envelope with no ref. |
| Any post-loss observation differs from the pinned ref sequence | Irreversibly reject that candidate; a later stage cannot re-add or repair the ref. Select fallback. |
| Final-budget observation differs, cannot fit metadata, or cannot be verified | Select the complete canonical sanitized envelope with no ref, even if it exceeds the candidate budget. |
| Every observation matches exactly | Deliver the last compact or unchanged model-visible candidate plus the complete ordered refs. |

Rejecting one candidate does not delete provider data or make lifecycle claims. It
only prevents delivery of an output whose recovery metadata cannot be trusted.

## Standalone and mixed installations

With no provider capability, Noisegate must import, configure, and call nothing
new. It receives no recovery metadata and behaves exactly as it does today:
eligible noisy output may compact, protected and unknown surfaces remain exact,
and errors fail open under the existing product contract. Installing a provider
without Noisegate is also useful independently; this contract does not require a
consumer to perform a lossy transform.

When both are available, their composition is mediated by the host's explicit
post-sanitization seam. Neither component discovers the other through imports or
load order. Provider failure degrades to the sanitized host result, not to broken
Noisegate output and not to provider absence followed by unprotected loss.

## Separation from Noisegate artifacts

Noisegate's current artifact feature is an explicit, opt-in private filesystem
store with its own `ng_` identifiers, size cap, permission, containment, symlink,
and secret-scanning rules. It is off by default, and early terminal-hook artifact
storage remains disabled. That feature and provider recovery are separate systems:

- enabling provider recovery must not enable Noisegate artifacts;
- enabling Noisegate artifacts must not request provider preservation;
- a Noisegate artifact identifier is not an opaque provider ref and cannot satisfy
  a required recovery-ref slot;
- a provider ref is not converted into, stored inside, or recovered through a
  Noisegate artifact;
- Noisegate must not implicitly copy the provider's canonical sanitized envelope
  into its artifact store;
- coexisting metadata uses distinct semantic slots and is never merged or
  reinterpreted.

This design does not change existing artifact behavior, defaults, storage, or
documentation.

## Retention and lifecycle boundary

Retention belongs entirely to the provider. Noisegate defines no TTL, quota,
profile/session scope, authorization rule, expiry promise, garbage-collection
schedule, purge behavior, or recovery command. It must not infer any of those from
an opaque ref or advertise indefinite recovery.

Provider-side expiry, purge, corruption, authorization denial, or unavailability
must produce truthful provider/recovery observations at recovery time. Portable
Noisegate fixtures may describe only what crosses the plugin boundary—for example,
“publication failed, so the host delivered canonical sanitized output with no
ref.” They do not implement lifecycle policy, storage, or recovery.

## Portable vectors

The language-neutral format is documented in
[`tests/fixtures/lossless_recovery/README.md`](../../tests/fixtures/lossless_recovery/README.md),
with cases in
[`vectors.json`](../../tests/fixtures/lossless_recovery/vectors.json). The pack uses
semantic slots rather than prospective host field names. Its small Python validator
lives under `tests/` and derives only the fixture truth table; it is excluded from
the runtime package and does not import Noisegate or a provider.

The vectors cover provider absence/no publication, exact and damaged single refs,
tiny budgets, relocation into text, duplicate occurrences, ordered multiple refs,
provider failure/unusable metadata, JSON strings and structured terminal-like
objects, normal and extreme budgets, exact and unknown tools, pre-redaction canary
exclusion, and distinct Noisegate artifact metadata.

## Readiness handoff to #38

Issue #38 remains blocked until an accepted, stable Hermes host contract is linked
there and satisfies these semantics. Readiness requires evidence that the host:

- establishes the post-sanitization/pre-loss ordering without plugin-order
  assumptions;
- retains the complete canonical sanitized envelope outside all lossy and budget
  stages;
- exposes a provider-neutral optional-publication observation and separately
  protected ordered refs;
- can represent ref canonical bytes without asking Noisegate to parse provider
  syntax;
- validates protected metadata after every lossy transform and after the final
  budget;
- can bypass the candidate budget to return exact full-sanitized/no-ref fallback;
- distinguishes provider absence, no preservation, failed publication, unusable
  metadata, and verified publication;
- never sends pre-redaction bytes to the provider or recovery-aware Noisegate seam;
- remains useful with provider-only, Noisegate-only, neither, or both installed.

Once that contract is accepted, #38 should link its issue/PR and map the accepted
semantics to these fixtures before production code is written. The concrete API
names, callback shapes, envelope representation, and metadata field names remain
owned by the accepted Hermes contract; this repository must not invent them.

## Stephen/Hermes handoff checklist

- [ ] Define the canonical sanitized-envelope boundary and prove sanitization runs
  before provider publication or lossy consumer dispatch.
- [ ] Retain the canonical fallback independently of the working model-visible copy.
- [ ] Give the provider only the canonical sanitized envelope and publish refs only
  after the provider reports verified atomic preservation.
- [ ] Carry refs in a generic protected structured-metadata channel, never inside
  compactable text.
- [ ] Pin a distinct ordered required-ref sequence with canonical bytes and exact
  occurrence semantics.
- [ ] Validate the sequence after each lossy consumer and after the final budget;
  reject missing, extra, duplicate, reordered, malformed, unverified, truncated,
  or byte-different occurrences.
- [ ] Guarantee complete canonical-sanitized/no-ref fallback for provider,
  consumer, serialization, metadata-transport, and final-budget failures.
- [ ] Ensure fallback cannot be truncated by the final candidate budget and cannot
  retain a provider ref or rejected recovery notice.
- [ ] Preserve ordinary provider-absent and no-preservation dispatch without a new
  runtime dependency.
- [ ] Mirror the host-owned vectors for provider failure, unusable metadata,
  pre-redaction canaries, missing/mutated/duplicate/multiple refs, and post-budget
  validation in focused Hermes tests.
- [ ] Reuse JSON-string, structured-object, oversized/tiny-budget, protected-tool,
  unknown-tool, and artifact-coexistence vectors as later real-dispatch cases in
  #38 after the host seam exists.
- [ ] Keep persistence, retention, quota, authorization, garbage collection,
  expiry, purge, and recovery implementation in the provider repository.
- [ ] Choose and document concrete API and field names in the accepted Hermes
  contract, not in this design pack.

## Open host-contract questions

The following questions require Hermes-side answers before #38 can implement an
adapter. They do not change the semantic requirements above:

1. What host representation designates the canonical sanitized envelope and its
   exact comparison bytes across string and structured results?
2. How does the host expose protected metadata generically while preventing every
   lossy consumer and final-budget serializer from treating it as text?
3. How does a consumer distinguish “unchanged,” “accepted candidate,” and “reject
   candidate/use fallback” without receiving provider-specific details?
4. Where is the canonical fallback retained, and how does final delivery bypass a
   budget that cannot fit it?
5. How are multiple refs ordered and associated with one result while rejecting a
   duplicate occurrence unambiguously?
6. What provider-neutral evidence makes publication verified, and how are failure
   and unusable metadata represented without exposing provider schemas?
7. Which host stages can be lossy after Noisegate, and at which exact boundaries is
   the protected sequence revalidated?
8. How will host tests prove that neither provider nor recovery-aware consumer sees
   values removed during sanitization?
9. How is composition made independent of plugin registration/load order for
   provider-only, Noisegate-only, neither, and combined installations?
10. Which accepted host tests and upstream contract version/link will #38 use as
    its implementation readiness signal?
