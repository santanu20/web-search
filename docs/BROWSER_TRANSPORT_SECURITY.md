# Browser transport security

The Playwright fallback uses a short-lived loopback HTTPS CONNECT proxy. This
keeps Chromium's TLS session addressed to the original hostname while moving
DNS resolution and upstream TCP connection into the process's SSRF policy
boundary.

## Request path

1. The requested URL is validated before Chromium starts.
2. Chromium is configured to use the loopback proxy for the browser context.
3. For every HTTPS CONNECT request, the proxy parses the authority and calls
   SafeURLResolver on that hostname and port.
4. The proxy opens the upstream socket using one of the validated numeric IP
   addresses. It never passes the origin hostname to the OS connect call.
5. TLS runs end-to-end between Chromium and the remote origin through the
   tunnel, preserving hostname-based SNI and certificate validation.
6. Existing Playwright route interception remains as an early additional
   policy check; the proxy is the connection-level enforcement point.

The proxy rejects ordinary HTTP proxy requests. Non-HTTPS URLs remain on the
curl transport, which has its own address-pinning implementation.

## Resource limits

The proxy uses a bounded worker semaphore, a per-tunnel byte limit, an
aggregate per-navigation byte limit, and a maximum tunnel lifetime. It is
shut down with the browser on both success and failure paths. A denied
destination or exhausted aggregate budget is surfaced as a policy/budget
error instead of silently returning a partial browser page.

## Operational trade-offs

- Browser-mode HTTP URLs are not attempted; the curl fetcher handles them.
- A browser page that requires plaintext HTTP mixed-content resources may
  render incompletely because the browser proxy intentionally refuses
  non-CONNECT traffic.
- Environment/system proxies are not involved in the browser path; Chromium
  is explicitly configured to use the process-local guard.
- The current CONNECT proxy covers HTTPS browser requests. It is not a
  general-purpose forward proxy and should not be repurposed for arbitrary
  application traffic.
