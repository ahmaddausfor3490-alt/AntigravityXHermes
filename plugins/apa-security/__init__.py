"""APA Security Guard — Hermes Agent security plugin entrypoint."""

from __future__ import annotations

from typing import Optional

from hermes_cli.plugins import PluginContext

from .audit_logger import AuditLogger, scrub_audit_payload, scrub_sensitive_string
from .command_validator import CommandValidator
from .config import APASecurityConfig
from .filesystem_guard import FilesystemGuard
from .models import (
    AuditEvent,
    CommandValidationResult,
    OperationType,
    PathValidationResult,
    PermissionDecision,
    PolicyMode,
    RiskLevel,
    SecurityEvaluation,
)
from .path_validator import PathValidator
from .permission_manager import ConfirmationCallback, PermissionManager
from .policy_engine import PolicyEngine
from .skills_guard import SkillsSecurityGuard
from .tool_interceptor import ToolInterceptor

__all__ = [
    "APASecurityConfig",
    "AuditEvent",
    "AuditLogger",
    "CommandValidationResult",
    "CommandValidator",
    "ConfirmationCallback",
    "FilesystemGuard",
    "OperationType",
    "PathValidationResult",
    "PathValidator",
    "PermissionDecision",
    "PermissionManager",
    "PolicyEngine",
    "PolicyMode",
    "RiskLevel",
    "SecurityEvaluation",
    "SkillsSecurityGuard",
    "ToolInterceptor",
    "get_active_interceptor",
    "register",
    "scrub_audit_payload",
    "scrub_sensitive_string",
]

_active_interceptor: Optional[ToolInterceptor] = None


def get_active_interceptor() -> Optional[ToolInterceptor]:
    """Return the active :class:`ToolInterceptor` instance registered in this process, if any."""
    return _active_interceptor


def register(ctx: PluginContext) -> None:
    """Register APA Security Guard ``pre_tool_call`` and ``post_tool_call`` hooks with Hermes."""
    global _active_interceptor
    config = APASecurityConfig.load_from_hermes(ctx)
    interceptor = ToolInterceptor(config=config)
    _active_interceptor = interceptor

    ctx.register_hook("pre_tool_call", interceptor.on_pre_tool_call)
    ctx.register_hook("post_tool_call", interceptor.on_post_tool_call)

    def _on_unload() -> None:
        global _active_interceptor
        if _active_interceptor is interceptor:
            _active_interceptor = None

    ctx.on_unload(_on_unload)
