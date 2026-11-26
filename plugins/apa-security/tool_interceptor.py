"""Hermes pre_tool_call and post_tool_call hook interceptor for APA Security Guard."""

from __future__ import annotations

import threading
import time
from typing import Any, Mapping, Optional

from .audit_logger import AuditLogger, scrub_audit_payload
from .config import APASecurityConfig
from .models import AuditEvent, PermissionDecision, RiskLevel, SecurityEvaluation
from .permission_manager import ConfirmationCallback
from .policy_engine import PolicyEngine

_IN_FLIGHT_TTL_SECS = 1800.0


class ToolInterceptor:
    """Bridges Hermes ``pre_tool_call`` and ``post_tool_call`` hooks to :class:`PolicyEngine` and :class:`AuditLogger`."""

    def __init__(
        self,
        config: Optional[APASecurityConfig] = None,
        *,
        policy_engine: Optional[PolicyEngine] = None,
        audit_logger: Optional[AuditLogger] = None,
        confirmation_callback: Optional[ConfirmationCallback] = None,
    ) -> None:
        self.config = config or APASecurityConfig()
        self.policy_engine = policy_engine or PolicyEngine(
            self.config, confirmation_callback=confirmation_callback
        )
        self.audit_logger = audit_logger or AuditLogger(self.config)
        self._lock = threading.Lock()
        self._in_flight: dict[tuple[str, str], tuple[float, SecurityEvaluation, bool]] = {}

    @staticmethod
    def _call_key(
        session_id: str,
        task_id: str,
        tool_call_id: str,
        turn_id: str,
        tool_name: str,
    ) -> tuple[str, str]:
        owner = session_id or task_id or "default"
        call_id = tool_call_id or turn_id or tool_name
        return (owner, call_id)

    def _evict_expired_locked(self, now: float) -> None:
        expired = [k for k, (ts, _ev, _logged) in self._in_flight.items() if now - ts > _IN_FLIGHT_TTL_SECS]
        for k in expired:
            self._in_flight.pop(k, None)

    def on_pre_tool_call(
        self,
        tool_name: str = "",
        args: Optional[dict[str, Any]] = None,
        task_id: str = "",
        session_id: str = "",
        tool_call_id: str = "",
        turn_id: str = "",
        **_: Any,
    ) -> Optional[dict[str, Any]]:
        """Hermes ``pre_tool_call`` hook callback.

        Returns:
        - ``None`` when the tool call is allowed to proceed.
        - ``{"action": "block", "message": "..."}`` when the tool call is denied.
        - ``{"action": "approve", "message": "...", "rule_key": "..."}`` when interactive
          approval is delegated to Hermes's fail-closed approval gate.
        """
        safe_args: Mapping[str, Any] = args if isinstance(args, Mapping) else {}
        evaluation = self.policy_engine.evaluate_tool_call(tool_name, safe_args)
        key = self._call_key(session_id, task_id, tool_call_id, turn_id, tool_name)
        now = time.monotonic()

        if evaluation.decision == PermissionDecision.DENY:
            self.audit_logger.log_event(
                AuditEvent(
                    tool_name=tool_name,
                    operation=evaluation.operation.value,
                    target=evaluation.target,
                    risk_level=evaluation.risk_level.value,
                    decision=evaluation.decision.value,
                    status="blocked",
                    reason=evaluation.reason,
                    session_id=session_id,
                    turn_id=turn_id,
                    tool_call_id=tool_call_id,
                    user_approved=evaluation.user_approved,
                    rule_id=evaluation.rule_id,
                    details={"args": scrub_audit_payload(dict(safe_args)), "phase": "pre_tool_call"},
                )
            )
            with self._lock:
                self._evict_expired_locked(now)
                self._in_flight[key] = (now, evaluation, True)
            return {
                "action": "block",
                "message": f"[APA Security Guard] BLOCKED ({evaluation.rule_id}): {evaluation.reason}",
            }

        if evaluation.decision == PermissionDecision.REQUIRE_CONFIRMATION:
            if self.config.unattended_confirmation_action == "deny":
                self.audit_logger.log_event(
                    AuditEvent(
                        tool_name=tool_name,
                        operation=evaluation.operation.value,
                        target=evaluation.target,
                        risk_level=evaluation.risk_level.value,
                        decision=evaluation.decision.value,
                        status="blocked_unconfirmed",
                        reason=evaluation.reason,
                        session_id=session_id,
                        turn_id=turn_id,
                        tool_call_id=tool_call_id,
                        user_approved=False,
                        rule_id=evaluation.rule_id,
                        details={"args": scrub_audit_payload(dict(safe_args)), "phase": "pre_tool_call"},
                    )
                )
                with self._lock:
                    self._evict_expired_locked(now)
                    self._in_flight[key] = (now, evaluation, True)
                return {
                    "action": "block",
                    "message": (
                        f"[APA Security Guard] CONFIRMATION REQUIRED ({evaluation.rule_id}): "
                        f"{evaluation.reason}"
                    ),
                }

            with self._lock:
                self._evict_expired_locked(now)
                self._in_flight[key] = (now, evaluation, False)
            return {
                "action": "approve",
                "message": f"[APA Security Guard] {evaluation.reason}",
                "rule_key": f"apa_security:{evaluation.rule_id or tool_name}",
            }

        with self._lock:
            self._evict_expired_locked(now)
            self._in_flight[key] = (now, evaluation, False)
        return None

    def on_post_tool_call(
        self,
        tool_name: str = "",
        args: Optional[dict[str, Any]] = None,
        result: Any = None,
        task_id: str = "",
        session_id: str = "",
        tool_call_id: str = "",
        turn_id: str = "",
        duration_ms: int = 0,
        status: Optional[str] = None,
        error_type: Optional[str] = None,
        error_message: Optional[str] = None,
        **_: Any,
    ) -> None:
        """Hermes ``post_tool_call`` observer hook callback."""
        safe_args: Mapping[str, Any] = args if isinstance(args, Mapping) else {}
        key = self._call_key(session_id, task_id, tool_call_id, turn_id, tool_name)

        with self._lock:
            entry = self._in_flight.pop(key, None)

        if entry is not None:
            _ts, evaluation, already_logged = entry
            if already_logged and error_type == "plugin_block":
                return
        else:
            evaluation = self.policy_engine.evaluate_tool_call(tool_name, safe_args)

        effective_status = status or ("error" if error_type else "ok")
        user_approved = evaluation.user_approved or (
            evaluation.decision == PermissionDecision.REQUIRE_CONFIRMATION and effective_status == "ok"
        )
        details: dict[str, Any] = {
            "args": scrub_audit_payload(dict(safe_args)),
            "phase": "post_tool_call",
        }
        if error_type:
            details["error_type"] = error_type
        if error_message:
            details["error_message"] = scrub_audit_payload(error_message)
        if result is not None and effective_status == "error" and not error_message:
            details["result_preview"] = scrub_audit_payload(str(result)[:200])

        self.audit_logger.log_event(
            AuditEvent(
                tool_name=tool_name,
                operation=evaluation.operation.value if hasattr(evaluation.operation, "value") else str(evaluation.operation),
                target=evaluation.target,
                risk_level=evaluation.risk_level.value if hasattr(evaluation.risk_level, "value") else str(evaluation.risk_level),
                decision=evaluation.decision.value if hasattr(evaluation.decision, "value") else str(evaluation.decision),
                status=effective_status,
                reason=evaluation.reason,
                session_id=session_id,
                turn_id=turn_id,
                tool_call_id=tool_call_id,
                user_approved=user_approved,
                duration_ms=int(duration_ms or 0),
                rule_id=evaluation.rule_id,
                details=details,
            )
        )
