# Egress and trusted-host policies

## LLM data egress

The LLM endpoint receives research prompts and extracted evidence. The
configuration makes the operator's trust decision explicit:

| Policy | Behavior |
|---|---|
| warn (default) | Preserve existing behavior; log when the configured host is not loopback |
| local_only | Allow loopback endpoints only; reject every off-machine model host |
| allowlist | Allow loopback plus exact hostnames in WEB_SEARCH_LLM_ALLOWED_HOSTS |

Example:

```bash
# Strict local inference
export WEB_SEARCH_LLM_EGRESS_POLICY=local_only

# Or explicitly approve a remote inference endpoint
export WEB_SEARCH_LLM_EGRESS_POLICY=allowlist
export WEB_SEARCH_LLM_ALLOWED_HOSTS=llm.example.com,model.internal
```

Allowlist matching is exact hostname matching: no suffix, wildcard or
subdomain expansion. The policy is applied before both generation and
embedding requests. The default remains warn for compatibility with
existing OpenAI-compatible or remote model deployments.

## Trusted hosts and private IP access

WEB_SEARCH_TRUSTED_HOSTS lists exact hostnames that an operator may need to
reach inside a private network. Listing a hostname alone does not bypass the
private-address policy.

Private-IP exceptions require the separate explicit opt-in:

```bash
export WEB_SEARCH_TRUSTED_HOSTS=intranet.corp,localhost
export WEB_SEARCH_TRUSTED_HOSTS_ALLOW_PRIVATE=1
```

When enabled, the configured exact hostnames can resolve to private or
loopback addresses. The resolver logs this elevated policy at initialization.
Cloud metadata hostnames and addresses remain blocked regardless of the
allowlist or opt-in.

For ordinary internet crawling, leave WEB_SEARCH_TRUSTED_HOSTS empty and
WEB_SEARCH_TRUSTED_HOSTS_ALLOW_PRIVATE=0.

## Scope

These controls govern the application's LLM endpoint dispatch and URL
resolver trusted-host exception. They do not replace network-layer egress
controls, container isolation or secret-management policy.