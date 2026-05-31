# Fix Missing Search Embeddings

## Problem

Some servers, agents, or skills are registered in the registry but do not appear in semantic search results. This happens when embedding generation fails during registration (e.g., embedding model temporarily unavailable, network timeout, or model initialization error).

## How to Detect

Use the `embeddings-missing` command to scan for documents that exist in source collections but have no corresponding entry in the search embeddings index:

```bash
uv run python registry_management.py embeddings-missing
```

Example output:

```
Embeddings Index Status:
  Source documents:  380
  Indexed:           378
  Missing:           2

Missing documents (2):

  Path                                               Type            Name
  -------------------------------------------------- --------------- ------------------------------
  /atlassian/                                        mcp_server      Atlassian
  /my-new-agent                                     a2a_agent       My New Agent
```

## How to Fix

Re-index all missing documents in one command:

```bash
uv run python registry_management.py embeddings-reindex --all-missing
```

Or re-index specific paths:

```bash
uv run python registry_management.py embeddings-reindex \
    --paths /atlassian/ /my-new-agent
```

Example output:

```
Found 2 missing documents. Reindexing...
  Batch 1: 2 success, 0 failed

Reindex complete: 2 success, 0 failed
```

## Using the API Directly

If you prefer to call the REST API directly (e.g., from a script or monitoring system):

```bash
# Check for missing embeddings
curl -s -H "Authorization: Bearer $TOKEN" \
    https://your-registry/api/admin/embeddings/missing | jq .

# Re-index specific paths
curl -s -X POST \
    -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" \
    -d '{"paths": ["/atlassian/", "/my-new-agent"]}' \
    https://your-registry/api/admin/embeddings/reindex | jq .
```

## When to Run This

- After upgrading the registry (some documents may not have been re-indexed)
- After a transient embedding model failure (check logs for "Embedding model unavailable" warnings)
- After federation sync imports new assets (synced assets may arrive without embeddings)
- As a periodic health check (schedule weekly or after deployments)

## Requirements

- Admin permissions required (the "Get JWT Token" button in the UI provides an admin token)
- The embedding model must be available and healthy for re-indexing to succeed
- Batch limit: 100 paths per API call (the CLI handles batching automatically)
