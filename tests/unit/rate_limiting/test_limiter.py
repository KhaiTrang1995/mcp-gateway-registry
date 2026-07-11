"""Unit tests for RateLimiter orchestration (fake backend, no DB).

Covers the behaviors the design calls out as critical: the Blocker-1 regression
(a denied burst must not consume a wider window's quota), most-restrictive
resolution within a window, gate stacking, and fail-open / fail-closed.
"""

from registry.rate_limiting.backend import IncrResult, RateLimiterBackend
from registry.rate_limiting.limiter import RateLimiter
from registry.rate_limiting.models import RateLimitDefinition


class _FakeBackend(RateLimiterBackend):
    """In-memory conditional counter honoring the deny-does-not-consume contract."""

    def __init__(self) -> None:
        self.counts: dict[str, int] = {}

    async def incr_if_allowed(
        self,
        key: str,
        window_seconds: int,
        max_requests: int,
    ) -> IncrResult:
        # The key here omits the window index (the fake keeps one window per key).
        current = self.counts.get(key, 0)
        if current >= max_requests:
            return IncrResult(allowed=False, current=max_requests)
        current += 1
        self.counts[key] = current
        return IncrResult(allowed=True, current=current)

    async def get(
        self,
        key: str,
        window_seconds: int,
    ) -> int:
        return self.counts.get(key, 0)


class _BoomBackend(RateLimiterBackend):
    """A backend whose ops always raise, to exercise fail-open / fail-closed."""

    async def incr_if_allowed(self, key, window_seconds, max_requests):
        raise RuntimeError("db down")

    async def get(self, key, window_seconds):
        raise RuntimeError("db down")


class _FakeDefs:
    """Minimal DefinitionsRepository stand-in returning fixed lists."""

    def __init__(
        self,
        caller_defs: list[RateLimitDefinition],
        target_defs: list[RateLimitDefinition],
    ) -> None:
        self._caller_defs = caller_defs
        self._target_defs = target_defs

    async def list_caller_limits(self, entity_type, names):
        return self._caller_defs if names else []

    async def list_target_limits(self, entity_type, name):
        return self._target_defs


async def _count_allowed(
    limiter: RateLimiter,
    times: int,
    **check_kwargs,
) -> int:
    """Drive ``times`` checks and count how many were allowed."""
    allowed = 0
    for _ in range(times):
        decision = await limiter.check(**check_kwargs)
        if decision.allowed:
            allowed += 1
    return allowed


def _caller(name, max_requests, window_seconds):
    return RateLimitDefinition(
        axis="caller",
        entity_type="group",
        name=name,
        max_requests=max_requests,
        window_seconds=window_seconds,
    )


class TestRateLimiter:
    """Behavioral tests for gate orchestration."""

    async def test_no_definitions_allows(self):
        """With no matching definitions, every call is allowed."""
        limiter = RateLimiter(_FakeBackend(), _FakeDefs([], []), fail_open=True)
        decision = await limiter.check(identity="u", groups=[])
        assert decision.allowed is True

    async def test_single_caller_gate_enforced(self):
        """A single caller gate allows exactly max_requests then denies."""
        backend = _FakeBackend()
        limiter = RateLimiter(backend, _FakeDefs([_caller("dev", 5, 60)], []), fail_open=True)
        allowed = await _count_allowed(limiter, 8, identity="alice", groups=["dev"])
        assert allowed == 5

    async def test_burst_denial_does_not_consume_daily_cap(self):
        """BLOCKER-1 regression: requests rejected by the burst gate must NOT advance the daily gate.

        Caller has 5/min and 20/day. Drive 30 rapid calls: 5 succeed (burst), and the daily
        counter must read 5, not 30.
        """
        backend = _FakeBackend()
        caller_defs = [_caller("dev", 5, 60), _caller("dev", 20, 86400)]
        limiter = RateLimiter(backend, _FakeDefs(caller_defs, []), fail_open=True)

        allowed = await _count_allowed(limiter, 30, identity="alice", groups=["dev"])

        assert allowed == 5
        assert backend.counts.get("clr:group:alice:86400") == 5

    async def test_most_restrictive_wins_within_same_window(self):
        """Among a caller's groups sharing a window, the smallest max_requests governs."""
        backend = _FakeBackend()
        caller_defs = [_caller("g1", 100, 60), _caller("g2", 3, 60)]
        limiter = RateLimiter(backend, _FakeDefs(caller_defs, []), fail_open=True)
        allowed = await _count_allowed(limiter, 10, identity="bob", groups=["g1", "g2"])
        assert allowed == 3

    async def test_target_gate_counts_across_callers(self):
        """A target (A2A agent) gate is enforced independent of caller identity."""
        backend = _FakeBackend()
        target_defs = [
            RateLimitDefinition(
                axis="target",
                entity_type="a2a_agent",
                name="booking",
                max_requests=2,
                window_seconds=60,
            )
        ]
        limiter = RateLimiter(backend, _FakeDefs([], target_defs), fail_open=True)
        allowed = await _count_allowed(
            limiter,
            5,
            identity="anyone",
            groups=[],
            target_entity_type="a2a_agent",
            target_name="booking",
        )
        assert allowed == 2
        assert backend.counts.get("tgt:a2a_agent:booking:60") == 2

    async def test_caller_and_target_gates_stack(self):
        """When both a caller and a target gate apply, the tighter one governs."""
        backend = _FakeBackend()
        caller_defs = [_caller("dev", 10, 60)]
        target_defs = [
            RateLimitDefinition(
                axis="target",
                entity_type="mcp_server",
                name="mcpgw",
                max_requests=3,
                window_seconds=60,
            )
        ]
        limiter = RateLimiter(backend, _FakeDefs(caller_defs, target_defs), fail_open=True)
        allowed = await _count_allowed(
            limiter,
            8,
            identity="alice",
            groups=["dev"],
            target_entity_type="mcp_server",
            target_name="mcpgw",
        )
        assert allowed == 3

    async def test_deny_decision_has_retry_after(self):
        """A denied call returns 429-shaped info with a positive Retry-After."""
        limiter = RateLimiter(
            _FakeBackend(), _FakeDefs([_caller("dev", 1, 60)], []), fail_open=True
        )
        await limiter.check(identity="alice", groups=["dev"])  # consume the 1 allowed
        decision = await limiter.check(identity="alice", groups=["dev"])
        assert decision.allowed is False
        assert decision.limit == 1
        assert int(decision.headers()["Retry-After"]) >= 1

    async def test_fail_open_allows_on_backend_error(self):
        """With fail_open, a backend error results in allow (availability guardrail)."""
        limiter = RateLimiter(
            _BoomBackend(), _FakeDefs([_caller("dev", 5, 60)], []), fail_open=True
        )
        decision = await limiter.check(identity="x", groups=["dev"])
        assert decision.allowed is True

    async def test_fail_closed_definition_denies_on_backend_error(self):
        """A per-limit fail_closed=True denies when the backend errors."""
        fc = RateLimitDefinition(
            axis="caller",
            entity_type="group",
            name="dev",
            max_requests=5,
            window_seconds=60,
            fail_closed=True,
        )
        limiter = RateLimiter(_BoomBackend(), _FakeDefs([fc], []), fail_open=True)
        decision = await limiter.check(identity="x", groups=["dev"])
        assert decision.allowed is False

    async def test_global_fail_open_false_denies_on_error(self):
        """With fail_open=False globally, a backend error denies even without fail_closed."""
        limiter = RateLimiter(
            _BoomBackend(), _FakeDefs([_caller("dev", 5, 60)], []), fail_open=False
        )
        decision = await limiter.check(identity="x", groups=["dev"])
        assert decision.allowed is False
