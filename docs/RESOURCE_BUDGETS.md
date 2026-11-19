# Extraction and crawl resource budgets

The extraction pipeline exposes byte, character, concurrency and link-count
budgets through environment settings. These are enforced at the application
boundary for both fresh extraction results and cached markdown.

| Setting | Default | Enforcement |
|---|---:|---|
| `WEB_SEARCH_MAX_RESPONSE_BYTES` | 200,000,000 | HTTP response body cap in the transport |
| `WEB_SEARCH_MAX_HTML_BYTES` | 100,000,000 | Reject an oversized HTML body before parsing |
| `WEB_SEARCH_MAX_MARKDOWN_BYTES` | 50,000,000 | Reject extracted markdown above this UTF-8 byte count |
| `WEB_SEARCH_MAX_DOCUMENT_CHARS` | 20,000,000 | Reject extracted documents above this character count |
| `WEB_SEARCH_MAX_CRAWL_BYTES` | 2,000,000,000 | Aggregate crawl output cap; the final page is clipped to the remaining UTF-8 byte allowance |
| `WEB_SEARCH_MAX_LINKS_PER_PAGE` | 500 | Stop parsing once this many unique internal links have been found on one page |
| `WEB_SEARCH_MAX_TOTAL_LINKS` | 10,000 | Cap the unique internal links retained across a crawl |
| `WEB_SEARCH_CONCURRENCY` | 3 | Parallel extraction workers, clamped to 1–16 |

## Enforcement notes

- The markdown/document caps are checked on cache hits as well as newly
  extracted content, so legacy cache entries cannot bypass a tightened cap.
- Oversized cached content is evicted before the budget error is raised.
- HTML byte counts use UTF-8 encoded bytes, not Python string character
  counts.
- Crawl clipping is UTF-8 safe: it never returns a partial multi-byte code
  point, and the aggregate returned content remains within the byte budget.
- Link scanning stops when its unique-link budget is reached; it does not
  first collect an unbounded list and then slice it.
- The worker ceiling is a hard application-level safety limit. Increasing
  concurrency above 16 is intentionally unsupported without revisiting the
  process memory and file-descriptor budget.

Limits are configurable for deployment-specific workloads. Raising them
increases memory and network exposure; size them against worker memory,
expected document sizes and the process/container resource limits.