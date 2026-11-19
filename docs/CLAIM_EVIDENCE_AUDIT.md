# Claim-level evidence audit

The `pro` command can optionally perform a second LLM pass that audits each
sentence-sized claim against the sources cited by that claim.

## Run

```bash
python web_search.py pro "your research question" --use-llm --verify-claims
```

The flag is opt-in because it adds one model call and therefore adds latency and
model-token cost. It requires the LLM path to be available. The verifier uses
the configured main model, does not enable reasoning mode, and is bounded by
the remaining research call budget.

## Output

Two fields are added to the normal JSON response:

- `claim_audit[]`: claim text, cited source IDs, status, accepted evidence spans
  and a short verifier rationale.
- `claim_audit_summary`: counts by status, whether the verifier ran, and the
  share of claims classified as fully supported.

Statuses:

| Status | Meaning |
|---|---|
| `supported` | The verifier judged the claim supported and at least one verbatim evidence span was found in a cited source |
| `partially_supported` | A cited-source span was found, but the verifier did not classify the whole claim as supported |
| `contradicted` | The cited source evidence was classified as contradicting the claim |
| `mixed` | The cited sources contain both supporting and contradicting spans |
| `contextual_only` | A valid source span is related but does not directly support the claim |
| `unverified` | No acceptable evidence span was returned, the verifier failed, or the model response was invalid |
| `missing_citation` | The claim has no resolvable citation in the report |
| `not_run` | The audit was not requested; citation metadata is still exposed |

## Validation model

The verifier receives only the cited sources associated with each claim, with a
bounded evidence window selected from the captured page text. Retrieved source
text is explicitly treated as untrusted data.

The model's status is not accepted on its own. Every accepted evidence item must
refer to a known source that is actually cited by the claim, use an allowed
stance, and contain a quote that can be matched against the captured source
snapshot after Unicode and whitespace normalization. Fabricated quotes,
unknown source IDs, duplicate/unknown claim IDs, and source IDs not cited by
the claim are rejected.

Source IDs are deterministic hashes of normalized URLs. Content hashes are
computed separately so a changed page snapshot can be distinguished from the
same source identity. Original source order is preserved, including entries
whose extraction failed, so citation numbers cannot silently shift.

## Limitations

This is a model-assisted entailment assessment with server-side quote-span
validation, not a formal proof of truth. It validates the sources already cited
by the synthesis; it does not independently search for contrary sources or
prove that those sources are authoritative, independent, current, or complete.
A quoted span can exist while a model misinterprets its meaning. Treat
`support_rate` as an audit result for this run, not as calibrated factual
confidence. High-stakes findings still need human review.

The audit is intentionally opt-in. Without `--verify-claims`, the response
still contains extracted claim/citation metadata, but cited claims are marked
`not_run`, never `supported`.
