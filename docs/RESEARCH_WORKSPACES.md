# Persistent research workspaces

Use `--project` to persist a Pro research run into a local SQLite workspace:

```bash
python web_search.py pro "Indian EV battery market" --use-llm --counter-evidence --project indian-ev-battery
python web_search.py projects
python web_search.py projects --project-id indian-ev-battery --runs 10
python web_search.py projects --project-id indian-ev-battery --run-id RUN_ID
python web_search.py projects --project-id indian-ev-battery --run-id RUN_ID --include-content
python web_search.py projects --project-id indian-ev-battery --delete --yes
```

## Stored data

- Project and run metadata.
- Original research synthesis and selected evaluation summaries.
- Source identity, publisher/type signals, content hash and captured text snapshot.
- Claim text, claim status and accepted supporting/contradicting/contextual evidence spans.
- Original citation versus counter-evidence role.
- Run-to-run source and claim deltas.

Each source snapshot is content-addressed by normalized URL identity and SHA-256
content hash. If the same URL yields new content, the new snapshot is stored
rather than overwriting the previous one.

Storage is bounded to 100 sources per run and 100,000 characters per source.
The database defaults to `~/.cache/web-search/research.db`; use
`--research-db PATH` or `WEB_SEARCH_RESEARCH_DB` to select another location.
The database file is created with owner-only permissions on POSIX systems.

## Temporal comparisons

For a repeated normalized query in the same project, the workspace reports:
- newly observed source URLs;
- changed source snapshots at existing URLs;
- newly observed claims;
- claim status transitions (for example, `unverified` to `supported`).

Claim-level comparisons are skipped when the normalized query changes, to avoid
comparing unrelated claims. Missing sources in a later run are not called
deleted or withdrawn because search result variation is not proof of removal.

## Data lifecycle

`projects --delete --project-id ID --yes` deletes the project, runs, snapshots,
claims and associated evidence rows through SQLite foreign-key cascades.

## Security and deployment limits

This is a local-first, single-user persistence feature. It is not:
- an encrypted database or a key-management integration;
- multi-tenant isolation or workspace RBAC;
- a distributed database or concurrent multi-node job store;
- a data retention policy or automatic expiry system.

Use encrypted storage/volume protection for sensitive research and only store
documents permitted by their licenses and access policies. Do not place a
shared enterprise database on an untrusted network filesystem. A service
deployment should provide authenticated tenant-scoped storage, key management,
auditable deletion and backup/restore controls externally.
