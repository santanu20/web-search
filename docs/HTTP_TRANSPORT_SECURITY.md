# HTTP transport security

The curl_cffi HTTP fetch path applies URL policy to the actual connection
destination, not only to an earlier DNS lookup.

## Address pinning

1. SafeURLResolver resolves the hostname and rejects forbidden addresses.
2. The resulting validated address set is passed to libcurl using
   CURLOPT_RESOLVE, while the original URL hostname remains in the URL for
   HTTP Host handling and TLS certificate/SNI validation.
3. Every redirect target is resolved, checked and pinned again before its
   request is sent.
4. IP-literal URLs need no DNS override.

If any address in a DNS answer violates the SSRF policy, the URL is rejected
before the request. The HTTP transport does not perform a second uncontrolled
origin DNS lookup.

## Response limits

The HTTP body is collected through curl's response callback. The byte budget
is applied to the decoded/decompressed chunks, not only to Content-Length
or a fully buffered response after download. This also covers chunked
responses and compressed bodies whose expanded size exceeds their wire size.

The configured WEB_SEARCH_MAX_RESPONSE_BYTES remains the upper bound. The
callback aborts the transfer as soon as the next chunk would exceed it.

## Proxy behavior

The protected curl_cffi path constructs its session with trust_env=False.
It therefore does not silently route requests through HTTP_PROXY,
HTTPS_PROXY or other ambient proxy variables. A remote proxy can resolve the
origin itself, which would bypass the local validated-address pin.

This means users who previously depended on environment proxy variables for
the extraction HTTP path must use a network route that permits direct
connections or a future explicitly configured proxy transport with its own
destination-enforcement design. Search-backend proxy behavior is separate.

## Scope limitation

These guarantees apply to the curl_cffi HTTP fetcher. The Playwright browser
fetcher is a separate transport and requires its own connection-level
destination enforcement. Its route interception currently performs URL/IP
policy checks but does not use libcurl's CURLOPT_RESOLVE; do not interpret
this document as certification of the browser path or of the complete
application against every SSRF scenario.
