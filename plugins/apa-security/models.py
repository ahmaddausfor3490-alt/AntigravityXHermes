"""Data models and enumerations for APA Security Guard."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any


class PermissionDecision(str, Enum):
    """Final authorization decision for an intercepted tool or action."""

    ALLOW = "ALLOW"
    DENY = "DENY"
    REQUIRE_CONFIRMATION = "REQUIRE_CONFIRMATION"


class RiskLevel(str, Enum):
    """Severity classification for an intercepted operation."""

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class OperationType(str, Enum):
    """Normalized operation category across Hermes tools."""

    READ = "read"
    WRITE = "write"
    DELETE = "delete"
    EXECUTE = "execute"
    NETWORK = "network"
    DELEGATE = "delegate"
    OTHER = "other"


class PolicyMode(str, Enum):
    """Security policy strictness mode."""

    STRICT = "strict"
    STANDARD = "standard"
    PERMISSIVE = "permissive"


@dataclass(frozen=True)
class PathValidationResult:
    """Outcome of validating a filesystem path against boundary and symlink rules."""

    allowed: bool
    canonical_path: str
    reason: str = ""
    rule_id: str = ""
    risk_level: RiskLevel = RiskLevel.LOW


@dataclass(frozen=True)
class CommandValidationResult:
    """Outcome of analyzing a shell/PowerShell command string."""

    allowed: bool
    risk_level: RiskLevel
    operation: OperationType
    reason: str = ""
    rule_id: str = ""
    extracted_paths: tuple[str, ...] = ()
    requires_confirmation: bool = False


@dataclass(frozen=True)
class SecurityEvaluation:
    """Complete evaluation outcome produced by PolicyEngine."""

    decision: PermissionDecision
    risk_level: RiskLevel
    operation: OperationType
    reason: str
    tool_name: str
    target: str = ""
    rule_id: str = ""
    user_approved: bool = False
    requires_confirmation: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def allowed(self) -> bool:
        """Return True only when the decision is ALLOW."""
        return self.decision == PermissionDecision.ALLOW


@dataclass
class AuditEvent:
    """Structured security audit record persisted as JSONL."""

    tool_name: str
    operation: str
    target: str
    risk_level: str
    decision: str
    status: str
    reason: str
    session_id: str = ""
    turn_id: str = ""
    tool_call_id: str = ""
    user_approved: bool = False
    duration_ms: int = 0
    rule_id: str = ""
    details: dict[str, Any] = field(default_factory=dict)
    timestamp: str = field(
        default_factory=lambda: datetime.now(UTC).isoformat()
    )

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable dictionary representation."""
        return asdict(self)
