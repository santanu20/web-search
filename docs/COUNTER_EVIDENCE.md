# Independent counter-evidence discovery

Run the Pro research mode with:

```bash
python web_search.py pro "your research question" --use-llm --counter-evidence
```

The option implies `--verify-claims`. It identifies up to eight cited claims,
generates one bounded contrary-evidence query per claim, searches for URLs not
already used in the original investigation, extracts a bounded set of new
pages, and associates candidates to claims by lexical relevance. Candidates
from the same host as an original cited source are excluded.

The verifier receives original cited sources and separately labeled
counter-evidence sources. Each accepted evidence record must reference a known
source assigned to that exact claim, use an allowed stance, and contain an
exact contiguous quote found in the captured source snapshot.

The response distinguishes `source_role: cited` from
`source_role: counter_evidence`. If original source evidence supports a claim
and newly discovered evidence contradicts it, the claim is marked `mixed`.
If only the counter-evidence contradicts the claim, it is marked
`contradicted`.

## Output

The `counter_evidence` response object reports whether the option ran, the
number of queries, discovered results, extracted pages, claims with candidate
evidence, and the candidate source URLs. The `claim_audit[]` entries contain
the final verified evidence and source roles.

## Limits

- This is a bounded first pass, not an exhaustive search for all contrary evidence.
- Different hosts do not prove organizational independence; publisher ownership,
  syndicated content, and shared upstream sources require source-lineage analysis.
- Candidate selection is lexical; the LLM assesses stance and relevance, and
  server-side quote validation checks only whether the cited words are present.
- Search failures, missing extraction, and budget exhaustion are reported rather
  than treated as proof that no contrary evidence exists.
- The feature does not rewrite the original synthesis citations; counter-evidence
  is a separate audit layer.
