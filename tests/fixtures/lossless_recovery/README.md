# Lossless-recovery contract-vector format

This directory contains provider-neutral, language-neutral JSON test data for the
consumer contract in
[`docs/design/lossless-recovery-consumer-contract.md`](../../../docs/design/lossless-recovery-consumer-contract.md).
It is test data, not a host schema, wire format, provider API, or production
Noisegate abstraction. All payloads, refs, identifiers, and canaries are synthetic.

## Encoding and comparison

`vectors.json` is UTF-8 JSON. A field ending in `_utf8` represents the UTF-8 byte
sequence obtained by decoding that JSON string and encoding it as UTF-8. Ref
equality, order, and occurrence counts are compared over those bytes. JSON content
inside an envelope string is not reparsed by the fixture validator; the complete
envelope string is the comparison unit.

The `synrec:v1:` prefix is a conspicuous test-only marker that keeps committed
fixture refs obvious to reviewers. It is not a proposed ref format, and the
validator does not treat it as ref syntax. A protected ref whose marker-prefix byte
changes remains a well-formed opaque observation for exact-byte comparison. Text
containing the marker in `canonical_sanitized_envelope_utf8` or
`model_visible_copy_utf8` remains ordinary content and never satisfies
`recovery_refs_utf8`.

## Pack fields

- `format_version`: fixture-format revision, currently `1`.
- `central_rule`: the contract's normative protected-ref rule, copied exactly so a
  test can detect semantic drift.
- `synthetic_recovery_ref_marker_utf8`: test-only marker described above.
- `forbidden_pre_redaction_canaries_utf8`: synthetic values that must be absent
  from every consumer-visible slot in every case.
- `cases`: ordered vector objects. Case order is for review only and has no runtime
  meaning.

## Case fields

- `id`: stable, lowercase test identifier.
- `covers`: issue-matrix tags exercised by the case.
- `owner`: `primary`, optional `supporting` layers, and a plain-language
  `invariant`. The only layer names are `host`, `provider`, and `consumer`.
- `provider_observation`: what the host makes observable at the consumer boundary.
  Its semantic `state` is one of `absent`, `no_preservation`, `published`,
  `failed_before_publication`, or `unusable_metadata`. Only `published` supplies
  the distinct, ordered `required_protected_refs_utf8` sequence.
- `canonical_sanitized_envelope_utf8`: the complete post-sanitization envelope
  retained outside the lossy candidate and selected verbatim on fallback. It may
  contain naturally occurring ref-like text, but never gains a provider ref from a
  protected metadata slot.
- `candidate_stages`: zero or more consecutive `post_lossy_transform`
  observations followed by at most one `post_final_budget` observation. A lossy
  observation after the final budget or a repeated final budget is invalid. Each
  stage contains the model-visible copy plus a separate protected-metadata object.
- `expected`: the declared decision, selected semantic source, and output ref
  sequence. The validator independently derives these values from provider and
  stage observations, then rejects a disagreement.
- `forbidden_pre_redaction_canaries_utf8`: case-specific synthetic canaries that
  may appear only in this control field.
- `notes`: review context, including repository/layer boundaries when useful.

## Protected metadata and decisions

Each stage's `protected_structured_metadata` has three semantic slots:

- `recovery_metadata_state`: `absent`, `well_formed`, `malformed`, or `unverified`;
- `recovery_refs_utf8`: the ordered protected recovery-ref occurrences;
- `other_semantic_entries`: unrelated typed metadata. The artifact-coexistence
  vector uses this to show that Noisegate's existing opt-in artifact metadata is
  not a provider recovery ref.

For a published provider observation, every stage must contain the complete
required ref sequence exactly once per distinct required ref, byte-for-byte and in
order, with `well_formed` metadata. A missing, extra, duplicate, reordered,
malformed, unverified, truncated, or changed occurrence derives
`full_sanitized_fallback`, and that failure remains latched across all later stage
observations. Provider failure, unusable publication metadata, or no candidate
also derives fallback. Fallback selects
`canonical_sanitized_envelope_utf8` exactly and returns an empty protected-ref
sequence.

When the provider is absent or publishes no preservation metadata, an otherwise
valid candidate has `absent` recovery metadata and can be accepted with no refs.
This represents existing standalone behavior, not a new recovery mode.

The validator intentionally does not model persistence, retention, quota,
authorization, expiry, garbage collection, dereferencing, or recovery. A lifecycle
case may record only a provider/host observation such as failure before publication;
implementation and policy remain outside this fixture format.
