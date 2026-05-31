# Switching Embedding Models

## Problem

You want to change the embedding model used for semantic search (e.g., from the default `all-MiniLM-L6-v2` to a higher-quality model via Amazon Bedrock or OpenAI). After switching, the old embeddings are incompatible because different models produce vectors of different dimensions and in different vector spaces.

## How Embedding Models Work in the Registry

The registry stores embeddings in a dimension-specific collection:
- `all-MiniLM-L6-v2` (default): 384 dimensions, stored in `mcp_embeddings_384`
- Amazon Bedrock Titan v2: 1024 dimensions, stored in `mcp_embeddings_1024`
- OpenAI or other LiteLLM models: 1536 dimensions, stored in `mcp_embeddings_1536`

When you change the model, the registry uses a **new collection** automatically. The old collection remains untouched (you can switch back).

## Step-by-Step: Switch to a New Model

### 1. Update Configuration

Set the new embedding provider in your environment:

```bash
# For Amazon Bedrock Titan v2
EMBEDDINGS_PROVIDER=litellm
EMBEDDINGS_MODEL_NAME=amazon.titan-embed-text-v2:0
EMBEDDINGS_MODEL_DIMENSIONS=1024
EMBEDDINGS_AWS_REGION=us-east-1

# For OpenAI text-embedding-3-small
EMBEDDINGS_PROVIDER=litellm
EMBEDDINGS_MODEL_NAME=text-embedding-3-small
EMBEDDINGS_MODEL_DIMENSIONS=1536
EMBEDDINGS_API_KEY=sk-...
```

### 2. Restart the Registry

After restarting, the registry uses the new collection (`mcp_embeddings_1024` or `mcp_embeddings_1536`). This collection starts empty.

### 3. Check Missing Embeddings

The new collection has no documents, so all assets will be reported as missing:

```bash
uv run python registry_management.py embeddings-missing
```

```
Embeddings Index Status:
  Source documents:  380
  Indexed:           0
  Missing:           380
```

### 4. Reindex All Documents

Generate embeddings for all documents using the new model:

```bash
uv run python registry_management.py embeddings-reindex --all-missing
```

```
Found 380 missing documents. Reindexing...
  Batch 1: 100 success, 0 failed
  Batch 2: 100 success, 0 failed
  Batch 3: 100 success, 0 failed
  Batch 4: 80 success, 0 failed

Reindex complete: 380 success, 0 failed
```

### 5. Verify

Run a semantic search to confirm the new model is working:

```bash
uv run python registry_management.py server-search --query "documentation search"
```

## Rolling Back

If the new model performs poorly, revert the environment variables to the previous model and restart. The old collection (`mcp_embeddings_384`) still has all its data intact.

## Using the API Directly

```bash
TOKEN=$(cat .token | jq -r '.tokens.access_token')

# Check how many documents need indexing with the new model
curl -s -H "Authorization: Bearer $TOKEN" \
    https://your-registry/api/admin/embeddings/missing | jq '.total_missing'

# Reindex in batches (max 100 per call)
curl -s -X POST \
    -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" \
    -d '{"paths": ["/server-1", "/server-2", "..."]}' \
    https://your-registry/api/admin/embeddings/reindex | jq .
```

## Performance Notes

- Reindexing 100 documents takes ~10-30 seconds depending on the model
- Local sentence-transformers models (all-MiniLM-L6-v2) are fastest
- LiteLLM models (Bedrock, OpenAI) are bounded by API rate limits
- The registry remains fully operational during reindexing (search uses whatever embeddings exist)
- Documents not yet reindexed will be found via keyword search only (not vector similarity)

## Configuration Reference

| Variable | Default | Description |
|----------|---------|-------------|
| `EMBEDDINGS_PROVIDER` | `sentence-transformers` | Provider: `sentence-transformers` or `litellm` |
| `EMBEDDINGS_MODEL_NAME` | `all-MiniLM-L6-v2` | Model identifier |
| `EMBEDDINGS_MODEL_DIMENSIONS` | `384` | Vector dimensions (must match model output) |
| `EMBEDDINGS_API_KEY` | (none) | API key for LiteLLM providers |
| `EMBEDDINGS_API_BASE` | (none) | Custom API base URL |
| `EMBEDDINGS_AWS_REGION` | `us-east-1` | AWS region for Bedrock models |
