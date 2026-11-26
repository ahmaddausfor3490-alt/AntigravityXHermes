"""Permission evaluation and confirmation gate for APA Security Guard."""

from __future__ import annotations

from dataclasses import replace
from typing import Callable, Optional

from .config import APASecurityConfig
from .models import OperationType, PermissionDecision, PolicyMode, RiskLevel, SecurityEvaluation

ConfirmationCallback = Callable[[SecurityEvaluation], bool]


class PermissionManager:
    """Evaluates operation permissions and manages explicit user confirmation."""

    def __init__(
        self,
        config: APASecurityConfig,
        confirmation_callback: Optional[ConfirmationCallback] = None,
    ) -> None:
        self.config = config
        self.confirmation_callback = confirmation_callback

    def evaluate_permission(
        self,
        base_eval: SecurityEvaluation,
        *,
        requires_confirmation_hint: bool = False,
    ) -> SecurityEvaluation:
        """Apply policy permission flags and confirmation requirements to *base_eval*."""
        if self.config.fail_closed_reason:
            return replace(
                base_eval,
                decision=PermissionDecision.DENY,
                risk_level=RiskLevel.CRITICAL,
                reason=self.config.fail_closed_reason,
                rule_id="CONFIG_FAIL_CLOSED",
            )

        if base_eval.decision == PermissionDecision.DENY or base_eval.risk_level == RiskLevel.CRITICAL:
            return replace(base_eval, decision=PermissionDecision.DENY)

        op = base_eval.operation

        # 1. Check explicit allow/deny capability toggles
        if op == OperationType.READ and not self.config.allow_read:
            return replace(
                base_eval,
                decision=PermissionDecision.DENY,
                risk_level=RiskLevel.HIGH,
                reason="Read operations are disabled by security policy (allow_read=False).",
                rule_id="PERM_READ_DISABLED",
            )
        if op == OperationType.WRITE and not self.config.allow_write:
            return replace(
                base_eval,
                decision=PermissionDecision.DENY,
                risk_level=RiskLevel.HIGH,
                reason="Write operations are disabled by security policy (allow_write=False).",
                rule_id="PERM_WRITE_DISABLED",
            )
        if op == OperationType.DELETE and not self.config.allow_delete:
            return replace(
                base_eval,
                decision=PermissionDecision.DENY,
                risk_level=RiskLevel.HIGH,
                reason="Delete operations are disabled by security policy (allow_delete=False).",
                rule_id="PERM_DELETE_DISABLED",
            )
        if op == OperationType.EXECUTE and not self.config.allow_execute:
            return replace(
                base_eval,
                decision=PermissionDecision.DENY,
                risk_level=RiskLevel.HIGH,
                reason="Command/code execution is disabled by security policy (allow_execute=False).",
                rule_id="PERM_EXECUTE_DISABLED",
            )
        if op == OperationType.NETWORK and not self.config.allow_network:
            return replace(
                base_eval,
                decision=PermissionDecision.DENY,
                risk_level=RiskLevel.HIGH,
                reason="Network operations are disabled by security policy (allow_network=False).",
                rule_id="PERM_NETWORK_DISABLED",
            )
        if op == OperationType.DELEGATE and not self.config.allow_subagents:
            return replace(
                base_eval,
                decision=PermissionDecision.DENY,
                risk_level=RiskLevel.HIGH,
                reason="Subagent delegation is disabled by security policy (allow_subagents=False).",
                rule_id="PERM_SUBAGENT_DISABLED",
            )

        # 2. Check confirmation gates
        needs_confirm = requires_confirmation_hint
        confirm_rule = base_eval.rule_id
        confirm_reason = base_eval.reason

        if op == OperationType.WRITE and (
            self.config.require_confirmation_for_write
            or self.config.policy_mode == PolicyMode.STRICT
        ):
            needs_confirm = True
            confirm_rule = "PERM_CONFIRM_WRITE"
            confirm_reason = f"Write operation on '{base_eval.target}' requires user confirmation."
        elif op == OperationType.DELETE and (
            self.config.require_confirmation_for_delete
            or self.config.policy_mode in (PolicyMode.STRICT, PolicyMode.STANDARD)
        ):
            needs_confirm = True
            confirm_rule = "PERM_CONFIRM_DELETE"
            confirm_reason = f"Delete operation on '{base_eval.target}' requires user confirmation."
        elif op == OperationType.EXECUTE and (
            self.config.require_confirmation_for_execute
            or (self.config.policy_mode == PolicyMode.STRICT and base_eval.risk_level != RiskLevel.LOW)
        ):
            needs_confirm = True
            confirm_rule = "PERM_CONFIRM_EXECUTE"
            confirm_reason = f"Execution of '{base_eval.target}' requires user confirmation."
        elif base_eval.risk_level == RiskLevel.HIGH and self.config.require_confirmation_for_high_risk:
            needs_confirm = True
            confirm_rule = "PERM_CONFIRM_HIGH_RISK"
            confirm_reason = f"High-risk operation ({base_eval.tool_name}) requires user confirmation."

        if not needs_confirm:
            return replace(base_eval, decision=PermissionDecision.ALLOW)

        pending_eval = replace(
            base_eval,
            decision=PermissionDecision.REQUIRE_CONFIRMATION,
            reason=confirm_reason,
            rule_id=confirm_rule,
        )

        # 3. If an explicit confirmation callback is bound, invoke it fail-closed
        if self.confirmation_callback is not None:
            try:
                approved = bool(self.confirmation_callback(pending_eval))
            except (OSError, RuntimeError, TypeError, ValueError):
                approved = False
            if approved:
                return replace(
                    pending_eval,
                    decision=PermissionDecision.ALLOW,
                    reason=f"Confirmed by user: {confirm_reason}",
                    user_approved=True,
                )
            return replace(
                pending_eval,
                decision=PermissionDecision.DENY,
                reason=f"User denied confirmation: {confirm_reason}",
                rule_id=f"{confirm_rule}_DENIED",
                user_approved=False,
            )

        return pending_eval
