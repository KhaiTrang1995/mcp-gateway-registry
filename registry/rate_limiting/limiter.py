"""The rate limiter: orchestrates caller (Limit A) and target (Limit B) gates.

A "gate" is one (axis, entity_type, subject, window) tuple with its definition.
Every applicable gate is enforced; ALL must pass. Different ``window_seconds``
are independent gates (a burst cap and a daily cap both apply); most-restrictive
resolution applies only among a caller's groups sharing the same window.

Gates are enforced **sequentially, tightest-window-first**, stopping at the first
denial. This guarantees that a request rejected by a tight burst gate never
increments a wider volume/day gate: the burst gate is evaluated first, denies,
and the later (longer-window) gates are never touched. A concurrent design would
consume every gate's quota on every attempt, letting a rejected burst exhaust the
daily cap -- so we deliberately trade one round trip per configured gate on the
allowed path (only paid when limits are configured; bounded by the backend
timeout) for correct cross-window behavior.
"""

import asyncio
import logging
import time

from ..observability.meters import (
    rate_limit_checks_total,
    rate_limit_errors_total,
    rate_limit_throttled_total,
)
from .backend import RateLimiterBackend
from .definitions_repository import DefinitionsRepository
from .models import AXIS_ABBREVIATION, RateLimitDecision, RateLimitDefinition

# Configure logging with basicConfig
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s,p%(process)s,{%(filename)s:%(lineno)d},%(levelname)s,%(message)s",
)
logger = logging.getLogger(__name__)

# Caller-axis entity type used in v1 (group-tier limits).
CALLER_GROUP_ENTITY_TYPE: str = "group"

# Default hard timeout per backend counter op. On timeout the op is treated as a
# backend error and follows the fail-open/closed policy, so a slow counter store
# fails FAST and never hangs the hot /validate path.
DEFAULT_BACKEND_TIMEOUT_SECONDS: float = 0.25


def _by_window(
    definitions: list[RateLimitDefinition],
) -> dict[int, list[RateLimitDefinition]]:
    """Group definitions by ``window_seconds`` so each window is resolved separately."""
    grouped: dict[int, list[RateLimitDefinition]] = {}
    for definition in definitions:
        grouped.setdefault(definition.window_seconds, []).append(definition)
    return grouped


def _window_reset_epoch(
    window_seconds: int,
    now_epoch: float,
) -> int:
    """Return the epoch second when the current fixed window for ``window_seconds`` resets."""
    window_index = int(now_epoch) // window_seconds
    return (window_index + 1) * window_seconds


class _Gate:
    """One resolved gate: an axis abbreviation, the counter subject, and its definition."""

    def __init__(
        self,
        axis_abbr: str,
        subject: str,
        definition: RateLimitDefinition,
    ) -> None:
        self.axis_abbr = axis_abbr
        self.subject = subject
        self.definition = definition

    def counter_key(self) -> str:
        """Build the counter key (window index is appended by the backend)."""
        d = self.definition
        return f"{self.axis_abbr}:{d.entity_type}:{self.subject}:{d.window_seconds}"


class RateLimiter:
    """Enforces caller and target aggregate limits on every checked call."""

    def __init__(
        self,
        backend: RateLimiterBackend,
        definitions: DefinitionsRepository,
        fail_open: bool = True,
        backend_timeout_seconds: float = DEFAULT_BACKEND_TIMEOUT_SECONDS,
    ) -> None:
        self._backend = backend
        self._definitions = definitions
        self._fail_open = fail_open
        self._backend_timeout_seconds = backend_timeout_seconds

    async def _build_caller_gates(
        self,
        identity: str,
        groups: list[str],
    ) -> list[_Gate]:
        """Build one caller gate per window, most-restrictive across the caller's groups."""
        caller_defs = await self._definitions.list_caller_limits(CALLER_GROUP_ENTITY_TYPE, groups)
        gates: list[_Gate] = []
        for defs_in_window in _by_window(caller_defs).values():
            tightest = min(defs_in_window, key=lambda d: d.max_requests)
            gates.append(_Gate(AXIS_ABBREVIATION["caller"], identity, tightest))
        return gates

    async def _build_target_gates(
        self,
        target_entity_type: str | None,
        target_name: str | None,
    ) -> list[_Gate]:
        """Build one target gate per window for the classified target entity (if any)."""
        if not target_entity_type or not target_name:
            return []
        target_defs = await self._definitions.list_target_limits(target_entity_type, target_name)
        return [
            _Gate(AXIS_ABBREVIATION["target"], target_name, definition)
            for definition in target_defs
        ]

    async def check(
        self,
        *,
        identity: str,
        groups: list[str],
        target_entity_type: str | None = None,
        target_name: str | None = None,
    ) -> RateLimitDecision:
        """Enforce every applicable gate; return the first denial or an aggregate allow.

        Gates are evaluated **sequentially, tightest-window-first**, short-circuiting on the
        first denial so a request rejected by a burst gate never increments a wider volume gate
        (deny-does-not-consume across windows).

        ``identity`` and ``groups`` MUST come from the validated token, never a client header.
        """
        caller_gates = await self._build_caller_gates(identity, groups)
        target_gates = await self._build_target_gates(target_entity_type, target_name)
        gates = caller_gates + target_gates
        if not gates:
            return RateLimitDecision.allow()

        # Tightest window first: a short burst cap is evaluated (and can deny) before a wider
        # daily cap, so the daily counter is never touched by a request the burst cap rejects.
        gates.sort(key=lambda gate: gate.definition.window_seconds)

        remaining_values: list[int] = []
        for gate in gates:
            decision = await self._enforce(gate)
            if not decision.allowed:
                return decision
            remaining_values.append(decision.remaining)
        # All gates passed. Report the tightest remaining headroom for the response headers.
        return RateLimitDecision.allow(remaining=min(remaining_values))

    async def _enforce(
        self,
        gate: _Gate,
    ) -> RateLimitDecision:
        """Enforce a single gate: conditional increment, emit metrics, build the decision."""
        definition = gate.definition
        labels: dict[str, str | int] = {
            "axis": gate.axis_abbr,
            "entity_type": definition.entity_type,
            "window_seconds": definition.window_seconds,
        }
        try:
            # Hard timeout so a slow counter store fails fast into the fail-open/closed
            # policy below, never hanging the /validate subrequest (a TimeoutError is an Exception).
            result = await asyncio.wait_for(
                self._backend.incr_if_allowed(
                    gate.counter_key(),
                    definition.window_seconds,
                    definition.max_requests,
                ),
                timeout=self._backend_timeout_seconds,
            )
        except Exception as exc:
            logger.warning(f"rate-limit backend error ({gate.counter_key()}): {exc}")
            rate_limit_errors_total.add(1, {"axis": gate.axis_abbr})
            if definition.fail_closed or not self._fail_open:
                return self._deny(gate)
            return RateLimitDecision.allow()  # fail-open

        outcome = "allow" if result.allowed else "deny"
        rate_limit_checks_total.add(1, {**labels, "outcome": outcome})
        if not result.allowed:
            rate_limit_throttled_total.add(1, labels)
            logger.warning(
                f"rate-limit throttled: axis={gate.axis_abbr} "
                f"entity_type={definition.entity_type} name={gate.subject} "
                f"limit={definition.max_requests}/{definition.window_seconds}s"
            )
            return self._deny(gate)
        return RateLimitDecision.allow(remaining=definition.max_requests - result.current)

    def _deny(
        self,
        gate: _Gate,
    ) -> RateLimitDecision:
        """Build a deny decision with reset/retry-after computed from the window boundary."""
        now_epoch = time.time()
        reset_epoch = _window_reset_epoch(gate.definition.window_seconds, now_epoch)
        retry_after = max(1, reset_epoch - int(now_epoch))
        return RateLimitDecision.deny(
            gate.definition,
            gate.axis_abbr,
            reset_epoch=reset_epoch,
            retry_after=retry_after,
        )
