"""Pydantic models for rate-limit definitions and decisions.

A definition is described by two orthogonal dimensions plus a window:

- ``axis``: which side of the call the limit applies to (``caller`` or ``target``).
- ``entity_type``: what kind of thing on that axis. Allowlisted per axis so the
  model is not tied to any single target kind (MCP server, A2A agent, and later
  tool/skill all plug in without a schema change).
- ``window_seconds``: the fixed window. Ranges from seconds to a full day, so a
  per-day volume cap is the same mechanism as a per-second burst cap.

See ``.scratchpad/issue-295/lld.md`` (Data Models) for the full rationale.
"""

import logging

from pydantic import BaseModel, Field, model_validator

# Configure logging with basicConfig
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s,p%(process)s,{%(filename)s:%(lineno)d},%(levelname)s,%(message)s",
)
logger = logging.getLogger(__name__)


# Allowlisted entity types per axis. Extend TARGET_ENTITY_TYPES as new entity
# kinds gain a gateway chokepoint. Unknown values are rejected in the validator
# (fail closed).
#   Coarse targets (name = path-derived):  "mcp_server", "a2a_agent"
#   Fine targets   (name = "<parent>:<leaf>", from the JSON-RPC payload; later
#                   phase, NOT enforced in v1): "mcp_tool", "a2a_skill"
CALLER_ENTITY_TYPES: frozenset[str] = frozenset({"group"})
TARGET_ENTITY_TYPES: frozenset[str] = frozenset(
    {"mcp_server", "a2a_agent", "mcp_tool", "a2a_skill"}
)

# Fine-grained target kinds whose enforcement classifier is not wired in v1.
# The admin API accepts these only when explicitly acknowledged, so an operator
# never gets a silently-inert limit. See lld.md API Design.
UNENFORCED_ENTITY_TYPES: frozenset[str] = frozenset({"mcp_tool", "a2a_skill"})

# Axis abbreviations used in counter keys (kept short and stable).
AXIS_ABBREVIATION: dict[str, str] = {"caller": "clr", "target": "tgt"}

# Bounds for window_seconds: 1 second up to one full day.
MIN_WINDOW_SECONDS: int = 1
MAX_WINDOW_SECONDS: int = 86400


class RateLimitDefinition(BaseModel):
    """A rate-limit definition on one axis, for one entity type, at one window.

    - ``axis="caller"``: aggregate limit on a caller dimension (v1: a group).
    - ``axis="target"``: aggregate limit on one target entity across all callers
      (v1 enforced: an MCP server or an A2A agent; tool/skill modeled but not
      wired).
    """

    axis: str = Field(
        ...,
        description="Which side of the call: 'caller' or 'target'",
    )
    entity_type: str = Field(
        ...,
        description="caller: 'group'; target: 'mcp_server' | 'a2a_agent' | 'mcp_tool' | 'a2a_skill'",
    )
    name: str = Field(
        ...,
        min_length=1,
        description="Group name, server path, or agent name this applies to",
    )
    max_requests: int = Field(
        ...,
        ge=1,
        description="Max requests allowed per window",
    )
    window_seconds: int = Field(
        default=60,
        ge=MIN_WINDOW_SECONDS,
        le=MAX_WINDOW_SECONDS,
        description="Window length in seconds (up to one day)",
    )
    fail_closed: bool = Field(
        default=False,
        description="If true, deny on backend error (security-critical only)",
    )
    enabled: bool = Field(
        default=True,
        description="Toggle without deleting",
    )

    @model_validator(mode="after")
    def _check_axis_and_entity_type(self) -> "RateLimitDefinition":
        """Reject any axis/entity_type combination outside the allowlists (fail closed)."""
        if self.axis not in AXIS_ABBREVIATION:
            raise ValueError(f"axis must be one of {sorted(AXIS_ABBREVIATION)}, got '{self.axis}'")

        allowed = CALLER_ENTITY_TYPES if self.axis == "caller" else TARGET_ENTITY_TYPES
        if self.entity_type not in allowed:
            raise ValueError(
                f"entity_type '{self.entity_type}' invalid for axis '{self.axis}' "
                f"(allowed: {sorted(allowed)})"
            )
        return self

    def build_id(self) -> str:
        """Build the definition document ``_id``: '<axis>:<entity_type>:<name>:<window_seconds>'."""
        return f"{self.axis}:{self.entity_type}:{self.name}:{self.window_seconds}"


class RateLimitDecision(BaseModel):
    """The result of enforcing a single gate (or the aggregate allow).

    ``reset_epoch`` is used to pick the friendliest ``Retry-After`` when several
    gates deny at once: the caller should back off until the gate with the
    longest reset clears, not a short burst window.
    """

    allowed: bool
    axis: str | None = None
    entity_type: str | None = None
    limit: int | None = None
    remaining: int = 0
    reset_epoch: int = 0
    retry_after: int = 0

    @classmethod
    def allow(
        cls,
        remaining: int = 0,
    ) -> "RateLimitDecision":
        """Build an allow decision. ``remaining`` is best-effort headroom for headers."""
        return cls(allowed=True, remaining=max(0, remaining))

    @classmethod
    def deny(
        cls,
        definition: RateLimitDefinition,
        axis_abbr: str,
        reset_epoch: int,
        retry_after: int,
    ) -> "RateLimitDecision":
        """Build a deny decision carrying the info needed for 429 headers."""
        return cls(
            allowed=False,
            axis=axis_abbr,
            entity_type=definition.entity_type,
            limit=definition.max_requests,
            remaining=0,
            reset_epoch=reset_epoch,
            retry_after=max(0, retry_after),
        )

    def headers(self) -> dict[str, str]:
        """Build the ``X-RateLimit-*`` / ``Retry-After`` headers for a 429 response."""
        return {
            "X-RateLimit-Limit": str(self.limit if self.limit is not None else 0),
            "X-RateLimit-Remaining": "0",
            "X-RateLimit-Reset": str(self.reset_epoch),
            "Retry-After": str(self.retry_after),
            "Connection": "close",
        }
