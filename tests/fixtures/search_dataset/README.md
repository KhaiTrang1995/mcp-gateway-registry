# Search Evaluation Test Harness

This folder contains a self-contained evaluation harness for testing search scoring methods offline. No server, Docker, or database required.

## What It Does

Runs a set of queries against a local document dataset using the same embedding model as production (all-MiniLM-L6-v2, 384 dimensions), scores results using both RRF and legacy methods, and measures quality against human-annotated ground truth using standard information retrieval metrics.

## Files

| File | Purpose |
|------|---------|
| `unified_dataset.json` | 378 documents with embeddings (dumped from `mcp_embeddings_384_default`) |
| `ground_truth.json` | 100 queries with expected results and relevance grades |
| `../../scripts/evaluate_search.py` | The evaluation script that runs everything |

## How to Run

```bash
cd /path/to/mcp-gateway-registry

# Full evaluation (both methods compared)
uv run python scripts/evaluate_search.py

# Per-query breakdown
uv run python scripts/evaluate_search.py --verbose

# Save detailed JSON for analysis
uv run python scripts/evaluate_search.py --output results.json

# Single method only
uv run python scripts/evaluate_search.py --method rrf
uv run python scripts/evaluate_search.py --method legacy
```

First run downloads the embedding model (~80MB). Subsequent runs use the cached model and complete in ~20 seconds.

## How the Evaluation Works

### Step 1: Load dataset and ground truth

The script loads all 378 documents (with their stored 384-dim embeddings) and the 100 annotated queries.

### Step 2: For each query, encode it

The query is encoded using the same `all-MiniLM-L6-v2` model that produced the document embeddings. This gives us a real query vector (not an approximation).

### Step 3: Score documents using both methods

**RRF method:**
1. Compute cosine similarity between query vector and every document embedding
2. Sort by similarity to get the vector-ranked list
3. Compute text_boost for every document (keyword matching on name, path, tags, tools, etc.)
4. Sort by text_boost to get the keyword-ranked list
5. Merge using RRF formula: `score(doc) = 1/(60 + vector_rank) + 1/(60 + keyword_rank)`

**Legacy method:**
1. Same cosine similarity computation
2. Same text_boost computation
3. Combine: `score = (cosine + 1.0) / 2.0 + text_boost * 0.1`
4. Clamp to [0, 1]

### Step 4: Compare against ground truth using NDCG@10

For each query, we know which documents should appear and how relevant they are (grade 1-3). We check where those documents actually landed in the ranked results and compute NDCG (Normalized Discounted Cumulative Gain).

## Understanding the Metrics

### NDCG@10 (Normalized Discounted Cumulative Gain at position 10)

Measures how well the top 10 results match the ideal ranking.

- **1.0** = perfect (all expected documents appear in ideal order)
- **0.0** = none of the expected documents appear in top 10
- **0.5** = expected documents appear but not in ideal positions

NDCG rewards relevant documents ranked higher. Finding the right answer at position #1 is worth more than finding it at position #8.

### Recall@10

What fraction of expected documents appear anywhere in the top 10.

- **1.0** = all expected documents found
- **0.5** = half of the expected documents found

### MRR (Mean Reciprocal Rank)

How quickly the first relevant result appears.

- **1.0** = first result is relevant
- **0.5** = second result is the first relevant one
- **0.1** = tenth result is the first relevant one

## Ground Truth Format

Each entry in `ground_truth.json`:

```json
{
  "query": "cloudflare",
  "category": "exact-name",
  "description": "Exact product name in server names and tags",
  "expected": [
    {"path": "/ai-registry/cloudflare-docs", "grade": 3, "reason": "Cloudflare in name and tags"},
    {"path": "/cloudflare-docs", "grade": 3, "reason": "Cloudflare in name and tags"},
    {"path": "/cloudflare-api", "grade": 3, "reason": "Cloudflare in name and tags"}
  ]
}
```

**Fields:**
- `query`: The search string
- `category`: One of the 10 test categories (see below)
- `description`: What this query tests
- `expected`: List of documents that should appear, with relevance grades
  - `path`: The document `_id` in the dataset
  - `grade`: 3 = perfect match, 2 = highly relevant, 1 = somewhat relevant
  - `reason`: Why this document is expected (for human reviewers)

## Query Categories

| Category | Count | Tests |
|----------|-------|-------|
| `exact-name` | 10 | Product/tool names as queries (lexical precision) |
| `semantic` | 10 | Natural language with no keyword overlap (vector quality) |
| `agent-focused` | 10 | Queries targeting agent assets |
| `skill-focused` | 10 | Queries targeting skill assets |
| `tool-precision` | 10 | Exact tool names (should find parent server) |
| `multi-entity` | 10 | Correct answers span multiple entity types |
| `conflict-ambiguous` | 4 | Generic words matching many documents |
| `conflict-vector-vs-lexical` | 6 | Vector and keyword signals disagree |
| `no-answer` | 10 | Nothing in the dataset truly matches |
| `tricky` | 20 | Edge cases, adversarial, non-English, empty queries |

## How to Add New Queries

1. Open `ground_truth.json`
2. Add a new entry with `query`, `category`, `description`, and `expected`
3. Make sure `path` values exist in `unified_dataset.json` (check the `_id` field)
4. Run the evaluation to see how both methods handle your new query

To validate paths exist:
```bash
python3 -c "
import json
with open('tests/fixtures/search_dataset/unified_dataset.json') as f:
    ids = {d['_id'] for d in json.load(f)}
with open('tests/fixtures/search_dataset/ground_truth.json') as f:
    for q in json.load(f):
        for exp in q['expected']:
            if exp['path'] not in ids:
                print(f'MISSING: {exp[\"path\"]} in query \"{q[\"query\"]}\"')
"
```

## How to Update the Dataset

If the registry content changes and you want a fresh dump:

```bash
docker exec mcp-mongodb mongosh --quiet mcp_registry --eval "
const col = db.mcp_embeddings_384_default;
print(JSON.stringify(col.find({}).toArray()));
" > tests/fixtures/search_dataset/unified_dataset.json
```

Before committing, check for customer data:
```bash
grep -i "tiaa\|expedia\|ericsson" tests/fixtures/search_dataset/unified_dataset.json
```

## How to Test a New Scoring Method

1. Add your scoring function to `scripts/evaluate_search.py` (follow the pattern of `_score_rrf` and `_score_legacy`)
2. Add it to the `--method` choices in the argparser
3. Wire it into `_run_evaluation`
4. Run: `uv run python scripts/evaluate_search.py --method your_method`
5. Compare NDCG, recall, MRR against existing methods

The harness is designed to be extended. Each scoring method is a function that takes `(docs, query_embedding, query_tokens)` and returns a ranked list of `(doc, score)` tuples.
