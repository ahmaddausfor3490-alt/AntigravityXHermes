"""Filesystem access guard for read, write, patch, search, and delete operations."""

from __future__ import annotations

from pathlib import Path

from agent.file_safety import get_write_denied_error, is_write_denied

from .config import APASecurityConfig
from .models import OperationType, PathValidationResult, PermissionDecision, RiskLevel, SecurityEvaluation
from .path_validator import PathValidator


def _matches_env_secret_name(filename_lower: str) -> bool:
    """Return True if *filename_lower* is a dotenv secret file (excluding .env.example/.sample/.template)."""
    if filename_lower == ".env":
        return True
    if filename_lower.startswith(".env."):
        safe_suffixes = (".example", ".sample", ".template", ".dist", ".schema")
        return not filename_lower.endswith(safe_suffixes)
    return False


class FilesystemGuard:
    """Enforces workspace boundaries, sensitive file protection, and self-tampering defense."""

    def __init__(self, config: APASecurityConfig, path_validator: PathValidator | None = None) -> None:
        self.config = config
        self.path_validator = path_validator or PathValidator(config)
        self._protected_filenames_lower = frozenset(n.lower() for n in config.protected_filenames)
        self._protected_exts_lower = frozenset(e.lower() for e in config.protected_extensions)
        self._protected_dirs_lower = frozenset(d.lower() for d in config.protected_directories)
        self._blocked_paths_lower = tuple(p.lower().replace("\\", "/") for p in config.blocked_paths)

    def _check_sensitive_path(self, target_path: Path, raw_path: str, operation: OperationType) -> tuple[bool, str, str]:
        """Return ``(is_blocked, reason, rule_id)`` if *target_path* or *raw_path* hits a protected resource."""
        for candidate in (target_path, Path(raw_path)):
            name_lower = candidate.name.lower()
            if _matches_env_secret_name(name_lower) or name_lower in self._protected_filenames_lower:
                return (
                    True,
                    f"Access to sensitive file '{candidate.name}' is denied by APA Security Guard.",
                    "FS_SENSITIVE_FILENAME",
                )
            suffix_lower = candidate.suffix.lower()
            if suffix_lower and suffix_lower in self._protected_exts_lower:
                return (
                    True,
                    f"Access to protected key/certificate extension '{suffix_lower}' is denied.",
                    "FS_SENSITIVE_EXTENSION",
                )
            parts_lower = [p.lower() for p in candidate.parts]
            for prot_dir in self._protected_dirs_lower:
                if prot_dir in parts_lower:
                    return (
                        True,
                        f"Access to protected directory '{prot_dir}' is denied by APA Security Guard.",
                        "FS_PROTECTED_DIRECTORY",
                    )

            # Protect .git hooks and config from modification/deletion
            if ".git" in parts_lower and operation in (OperationType.WRITE, OperationType.DELETE):
                return (
                    True,
                    "Modifying or deleting files inside '.git' is denied to prevent repository/hook tampering.",
                    "FS_GIT_TAMPER",
                )
            if ".git" in parts_lower and name_lower in ("config", "credentials", ".git-credentials"):
                return (
                    True,
                    "Accessing git credential/config files inside '.git' is denied.",
                    "FS_GIT_CREDENTIALS",
                )

        norm_target = str(target_path).lower().replace("\\", "/")
        norm_raw = raw_path.lower().replace("\\", "/")
        for blocked in self._blocked_paths_lower:
            if blocked and (blocked in norm_target or blocked in norm_raw):
                return (
                    True,
                    f"Path matches blocked_paths policy rule '{blocked}'.",
                    "FS_BLOCKED_PATH_POLICY",
                )

        # Self-tampering protection: model cannot modify/delete APA Security Guard or its config
        if operation in (OperationType.WRITE, OperationType.DELETE):
            for imm in self.config.immutable_security_paths:
                imm_norm = imm.lower().replace("\\", "/")
                if imm_norm in norm_target or imm_norm in norm_raw:
                    return (
                        True,
                        f"Self-tampering protection: modifying or deleting '{imm}' is strictly forbidden.",
                        "FS_SELF_TAMPER_PROTECTION",
                    )

        return (False, "", "")

    def evaluate_path_operation(
        self,
        raw_path: str,
        operation: OperationType,
        *,
        tool_name: str = "",
    ) -> SecurityEvaluation:
        """Validate a filesystem operation (`READ`, `WRITE`, `DELETE`) on *raw_path*."""
        if self.config.fail_closed_reason:
            return SecurityEvaluation(
                decision=PermissionDecision.DENY,
                risk_level=RiskLevel.CRITICAL,
                operation=operation,
                reason=self.config.fail_closed_reason,
                tool_name=tool_name,
                target=raw_path,
                rule_id="CONFIG_FAIL_CLOSED",
            )

        # Check sensitive filename/directory patterns on raw_path BEFORE and AFTER resolution
        # so even a blocked outside-workspace sensitive path is tagged accurately, while still checking boundary.
        path_res: PathValidationResult = self.path_validator.validate_path(raw_path)
        target_path = Path(path_res.canonical_path) if path_res.canonical_path else Path(raw_path)

        is_sens, sens_reason, sens_rule = self._check_sensitive_path(target_path, raw_path, operation)
        if not path_res.allowed:
            reason = f"{path_res.reason} ({sens_reason})" if is_sens else path_res.reason
            effective_rule = (
                sens_rule
                if (is_sens and path_res.rule_id == "PATH_DEVICE_OR_ADS")
                else path_res.rule_id
            )
            return SecurityEvaluation(
                decision=PermissionDecision.DENY,
                risk_level=RiskLevel.CRITICAL if is_sens else path_res.risk_level,
                operation=operation,
                reason=reason,
                tool_name=tool_name,
                target=path_res.canonical_path or raw_path,
                rule_id=effective_rule,
                metadata={"sensitive_target": is_sens, "sensitive_rule": sens_rule},
            )

        if is_sens:
            return SecurityEvaluation(
                decision=PermissionDecision.DENY,
                risk_level=RiskLevel.CRITICAL,
                operation=operation,
                reason=sens_reason,
                tool_name=tool_name,
                target=path_res.canonical_path,
                rule_id=sens_rule,
            )

        if operation in (OperationType.WRITE, OperationType.DELETE) and is_write_denied(path_res.canonical_path):
            write_err = get_write_denied_error(path_res.canonical_path) or "Write denied by Hermes file safety policy."
            return SecurityEvaluation(
                decision=PermissionDecision.DENY,
                risk_level=RiskLevel.CRITICAL,
                operation=operation,
                reason=write_err,
                tool_name=tool_name,
                target=path_res.canonical_path,
                rule_id="FS_HERMES_WRITE_DENIED",
            )

        risk = RiskLevel.LOW
        if operation == OperationType.WRITE:
            risk = RiskLevel.MEDIUM
        elif operation == OperationType.DELETE:
            risk = RiskLevel.HIGH

        return SecurityEvaluation(
            decision=PermissionDecision.ALLOW,
            risk_level=risk,
            operation=operation,
            reason="Filesystem check passed.",
            tool_name=tool_name,
            target=path_res.canonical_path,
            rule_id="FS_OK",
        )
