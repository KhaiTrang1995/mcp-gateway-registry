"""Benchmark search quality against a deployed AI Registry.

Runs the ground truth query set (100 queries) against a live deployment's
/api/search/semantic endpoint and captures results for quality assessment
and before/after comparison.

Usage:
    # Test against your registry (provide URL and token file)
    uv run python scripts/benchmark_search.py \
        --url https://your-registry.example.com \
        --token-file .token \
        --output results.json

    # Compare two runs (e.g., before and after upgrade)
    uv run python scripts/benchmark_search.py \
        --compare results_before.json results_after.json

    # Use inline token instead of file
    uv run python scripts/benchmark_search.py \
        --url http://localhost \
        --token "eyJhbG..." \
        --output results.json
"""

import argparse
import json
import logging
import sys
import time
from pathlib import Path

import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s,p%(process)s,{%(filename)s:%(lineno)d},%(levelname)s,%(message)s",
)
logger = logging.getLogger(__name__)

QUERIES_FILE = Path(__file__).parent.parent / "tests/fixtures/search_dataset/ground_truth.json"


def _load_queries(
    queries_file: Path,
) -> list[dict]:
    """Load benchmark queries from JSON file."""
    with open(queries_file) as f:
        return json.load(f)


def _run_search(
    base_url: str,
    query: str,
    token: str | None = None,
) -> dict:
    """Execute a semantic search query against the deployment."""
    url = f"{base_url}/api/search/semantic"
    params = {"q": query, "max_results": 10}
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    response = requests.get(url, params=params, headers=headers, timeout=30)
    response.raise_for_status()
    return response.json()


def _extract_summary(
    response: dict,
) -> dict:
    """Extract a comparable summary from search response."""
    summary = {"servers": [], "tools": [], "agents": [], "skills": []}

    for server in response.get("servers", []):
        summary["servers"].append({
            "name": server.get("server_name"),
            "path": server.get("path"),
            "score": server.get("relevance_score"),
        })

    for tool in response.get("tools", []):
        summary["tools"].append({
            "name": tool.get("tool_name"),
            "server": tool.get("server_name"),
            "score": tool.get("relevance_score"),
        })

    for agent in response.get("agents", []):
        summary["agents"].append({
            "name": agent.get("agent_name"),
            "path": agent.get("path"),
            "score": agent.get("relevance_score"),
        })

    for skill in response.get("skills", []):
        summary["skills"].append({
            "name": skill.get("skill_name"),
            "path": skill.get("path"),
            "score": skill.get("relevance_score"),
        })

    return summary


def _run_benchmark(
    base_url: str,
    queries: list[dict],
    token: str | None = None,
) -> dict:
    """Run all benchmark queries and collect results."""
    results = {
        "url": base_url,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "queries": [],
    }

    for q in queries:
        query_text = q["query"]
        logger.info(f"Running query: '{query_text}'")

        start = time.time()
        try:
            response = _run_search(base_url, query_text, token)
            elapsed = time.time() - start

            summary = _extract_summary(response)
            results["queries"].append({
                "query": query_text,
                "description": q.get("description", ""),
                "elapsed_ms": round(elapsed * 1000, 1),
                "total_results": sum(len(v) for v in summary.values()),
                "results": summary,
            })
            logger.info(
                f"  -> {summary['servers'].__len__()} servers, "
                f"{summary['tools'].__len__()} tools, "
                f"{summary['agents'].__len__()} agents, "
                f"{summary['skills'].__len__()} skills "
                f"({elapsed*1000:.0f}ms)"
            )
        except Exception as e:
            logger.error(f"  -> FAILED: {e}")
            results["queries"].append({
                "query": query_text,
                "description": q.get("description", ""),
                "error": str(e),
            })

    return results


def _compare_results(
    file_a: Path,
    file_b: Path,
) -> None:
    """Print side-by-side comparison of two benchmark runs."""
    with open(file_a) as f:
        results_a = json.load(f)
    with open(file_b) as f:
        results_b = json.load(f)

    print(f"\n{'='*80}")
    print(f"COMPARISON: {file_a.name} vs {file_b.name}")
    print(f"{'='*80}")
    print(f"  A: {results_a['url']} ({results_a['timestamp']})")
    print(f"  B: {results_b['url']} ({results_b['timestamp']})")
    print()

    queries_a = {q["query"]: q for q in results_a["queries"]}
    queries_b = {q["query"]: q for q in results_b["queries"]}

    all_queries = list(queries_a.keys() | queries_b.keys())
    all_queries.sort()

    for query in all_queries:
        qa = queries_a.get(query, {})
        qb = queries_b.get(query, {})

        print(f"\n--- Query: '{query}' ---")

        if qa.get("error") or qb.get("error"):
            print(f"  A: ERROR {qa.get('error', 'N/A')}")
            print(f"  B: ERROR {qb.get('error', 'N/A')}")
            continue

        ra = qa.get("results", {})
        rb = qb.get("results", {})

        for entity_type in ["servers", "tools", "agents", "skills"]:
            items_a = ra.get(entity_type, [])
            items_b = rb.get(entity_type, [])

            if not items_a and not items_b:
                continue

            print(f"\n  {entity_type.upper()}:")
            max_len = max(len(items_a), len(items_b))

            for i in range(max_len):
                a_item = items_a[i] if i < len(items_a) else None
                b_item = items_b[i] if i < len(items_b) else None

                a_str = (
                    f"{a_item['name']} ({a_item['score']:.4f})"
                    if a_item
                    else "(none)"
                )
                b_str = (
                    f"{b_item['name']} ({b_item['score']:.4f})"
                    if b_item
                    else "(none)"
                )

                changed = ""
                if a_item and b_item:
                    if a_item["name"] != b_item["name"]:
                        changed = " [RERANKED]"
                    elif abs(a_item["score"] - b_item["score"]) > 0.001:
                        changed = " [score changed]"

                print(f"    #{i+1}: {a_str:40} | {b_str}{changed}")

        # Score saturation check
        all_scores_a = [
            item["score"]
            for items in ra.values()
            for item in items
            if item.get("score") is not None
        ]
        all_scores_b = [
            item["score"]
            for items in rb.values()
            for item in items
            if item.get("score") is not None
        ]

        saturated_a = sum(1 for s in all_scores_a if s >= 0.999)
        saturated_b = sum(1 for s in all_scores_b if s >= 0.999)
        unique_a = len(set(round(s, 4) for s in all_scores_a))
        unique_b = len(set(round(s, 4) for s in all_scores_b))

        print(f"\n  Score health: A={unique_a} unique scores ({saturated_a} saturated at 1.0)"
              f" | B={unique_b} unique scores ({saturated_b} saturated at 1.0)")


def main():
    """Parse args and run benchmark or comparison."""
    parser = argparse.ArgumentParser(
        description="Benchmark search scoring for before/after comparison",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Example usage:
    # Test against your deployed registry
    uv run python scripts/benchmark_search.py \\
        --url https://registry.example.com --token-file .token --output results.json

    # Compare before/after upgrade
    uv run python scripts/benchmark_search.py \\
        --compare results_before.json results_after.json
""",
    )
    parser.add_argument(
        "--url",
        type=str,
        help="Base URL of the deployment to benchmark",
    )
    parser.add_argument(
        "--output",
        type=str,
        help="Output JSON file for results",
    )
    parser.add_argument(
        "--token",
        type=str,
        default=None,
        help="Bearer token for authentication (inline)",
    )
    parser.add_argument(
        "--token-file",
        type=str,
        default=None,
        help="Path to file containing bearer token (e.g. .token)",
    )
    parser.add_argument(
        "--queries",
        type=str,
        default=str(QUERIES_FILE),
        help="Path to queries JSON file",
    )
    parser.add_argument(
        "--compare",
        nargs=2,
        metavar=("FILE_A", "FILE_B"),
        help="Compare two benchmark result files",
    )

    args = parser.parse_args()

    if args.compare:
        _compare_results(Path(args.compare[0]), Path(args.compare[1]))
        return

    if not args.url or not args.output:
        parser.error("--url and --output are required when not using --compare")

    token = args.token
    if not token and args.token_file:
        token_path = Path(args.token_file)
        if not token_path.exists():
            parser.error(f"Token file not found: {token_path}")
        raw = token_path.read_text().strip()
        if raw.lower().startswith("bearer "):
            token = raw[7:]
        else:
            token = raw

    queries = _load_queries(Path(args.queries))
    logger.info(f"Loaded {len(queries)} benchmark queries")
    logger.info(f"Target: {args.url}")

    results = _run_benchmark(args.url, queries, token)

    output_path = Path(args.output)
    with open(output_path, "w") as f:
        json.dump(results, f, indent=2)

    logger.info(f"Results saved to {output_path}")


if __name__ == "__main__":
    main()
