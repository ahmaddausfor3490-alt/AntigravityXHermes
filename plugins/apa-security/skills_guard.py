"""Skills Security Guard enforcing root boundaries, mutation controls, and operation classification (Phase 4.4)."""

from __future__ import annotations

import logging
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
from typing import Any, Mapping, Optional, Sequence

from hermes_constants import get_skills_dir

from .config import APASecurityConfig
from .filesystem_guard import FilesystemGuard
from .models import OperationType, PermissionDecision, RiskLevel, SecurityEvaluation
from .path_validator import PathValidator, _is_within_root, _resolve_real_target

logger = logging.getLogger(__name__)

_NT_DEVICE_PREFIX_RE = re.compile(
    r"^(?:\\\\\.\\|\\\\\?\\(?:UNC|GLOBALROOT|Volume|PhysicalDrive)|\\\?\?\\)",
    re.IGNORECASE,
)

_SENSITIVE_SKILL_FILES = frozenset({
    ".env",
    ".git",
    ".ssh",
    ".aws",
    "id_rsa",
    "id_ed25519",
    "id_ecdsa",
    "authorized_keys",
    "known_hosts",
    "credentials",
    "credentials.json",
    "secrets.json",
    "config.yaml",
})

_SENSITIVE_SKILL_EXTS = frozenset({
    ".pem",
    ".key",
    ".pfx",
    ".p12",
})

_FORBIDDEN_SCRIPT_EXTENSIONS = frozenset({
    ".exe",
    ".dll",
    ".bat",
    ".cmd",
    ".vbs",
    ".msi",
    ".bin",
    ".scr",
    ".com",
    ".pif",
})

_DANGEROUS_PAYLOAD_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"powershell(\.exe)?\s+.*(-enc|-encodedcommand)", re.IGNORECASE), "Encoded PowerShell command"),
    (re.compile(r"\b(iex|invoke-expression)\b", re.IGNORECASE), "PowerShell Invoke-Expression"),
    (re.compile(r"curl\s+.*\|\s*(sh|bash)", re.IGNORECASE), "Piped curl-to-shell execution"),
    (re.compile(r"wget\s+.*\|\s*(sh|bash)", re.IGNORECASE), "Piped wget-to-shell execution"),
    (re.compile(r"rm\s+-rf\s+/(?!\S)", re.IGNORECASE), "Destructive root filesystem deletion"),
    (re.compile(r"del\s+/[fF]\s+/[sS]\s+/[qQ]\s+[cC]:", re.IGNORECASE), "Destructive Windows system deletion"),
    (re.compile(r"format\s+[cC]:", re.IGNORECASE), "Drive format command"),
)


def _has_traversal(candidate: str) -> bool:
    """Return True if candidate contains any '..' traversal component."""
    norm = candidate.replace("\\", "/")
    return ".." in norm.split("/")


def _is_sensitive_skill_file(path_str: str) -> bool:
    """Check if file targets sensitive credentials or keys."""
    p = Path(path_str)
    name_lower = p.name.lower()
    if name_lower in {s.lower() for s in _SENSITIVE_SKILL_FILES} or name_lower.startswith(".env."):
        return True
    if p.suffix.lower() in _SENSITIVE_SKILL_EXTS:
        return True
    for part in p.parts:
        if part.lower() in {".ssh", ".aws", ".git"}:
            return True
    return False


class SkillsSecurityGuard:
    """Evaluates skill operations, enforces approved roots, and blocks mutation/script escapes."""

    def __init__(
        self,
        config: APASecurityConfig,
        filesystem_guard: FilesystemGuard,
        path_validator: PathValidator,
    ) -> None:
        self.config = config
        self.filesystem_guard = filesystem_guard
        self.path_validator = path_validator

    def get_approved_skill_roots(self) -> tuple[Path, ...]:
        """Return canonical paths of all authorized skill roots."""
        roots: list[Path] = []

        # 1. Workspace skills directories
        ws = self.config.workspace_root
        roots.append((ws / "skills").resolve())
        roots.append((ws / ".hermes" / "skills").resolve())

        # 2. User profile skills directory (~/.hermes/skills)
        try:
            profile_skills = get_skills_dir().resolve()
            roots.append(profile_skills)
        except Exception as exc:
            logger.debug("Could not resolve profile skills dir: %s", exc, exc_info=True)

        # 3. Explicitly configured allowed skill roots
        for custom_root in self.config.allowed_skill_roots:
            try:
                roots.append(custom_root.resolve())
            except Exception as exc:
                logger.debug("Could not resolve custom skill root %s: %s", custom_root, exc, exc_info=True)

        return tuple(roots)

    def is_within_approved_skill_root(self, target: Path | str) -> tuple[bool, Optional[Path]]:
        """Verify that target resolves within one of the approved skill roots without escape."""
        try:
            target_path = Path(target)
            real_target = _resolve_real_target(target_path)
            for approved in self.get_approved_skill_roots():
                if _is_within_root(real_target, approved):
                    return True, approved
        except (OSError, RuntimeError, ValueError):
            return False, None
        return False, None

    def is_pinned_or_protected(self, skill_name: str) -> bool:
        """Check if skill is protected against modification or deletion."""
        clean = skill_name.strip().lower()
        if clean in {s.lower() for s in self.config.pinned_skills}:
            return True
        return False

    def evaluate_skill_view(self, tool_name: str, args: Mapping[str, Any]) -> SecurityEvaluation:
        """Evaluate skill_view call (Classification: READ)."""
        name = str(args.get("name") or "").strip()
        file_path = str(args.get("file_path") or "").strip()

        if not name:
            return SecurityEvaluation(
                decision=PermissionDecision.DENY,
                risk_level=RiskLevel.HIGH,
                operation=OperationType.READ,
                reason="skill_view requires a non-empty skill 'name'.",
                tool_name=tool_name,
                target="",
                rule_id="SKILL_NAME_EMPTY",
            )

        if "\x00" in name or "\x00" in file_path:
            return SecurityEvaluation(
                decision=PermissionDecision.DENY,
                risk_level=RiskLevel.CRITICAL,
                operation=OperationType.READ,
                reason="Null byte detected in skill identifier or path.",
                tool_name=tool_name,
                target=name,
                rule_id="SKILL_NULL_BYTE",
            )

        if _NT_DEVICE_PREFIX_RE.match(name):
            return SecurityEvaluation(
                decision=PermissionDecision.DENY,
                risk_level=RiskLevel.CRITICAL,
                operation=OperationType.READ,
                reason=f"Device namespace or special path blocked in skill name: '{name}'.",
                tool_name=tool_name,
                target=name,
                rule_id="SKILL_NT_NAMESPACE",
            )

        if _has_traversal(name):
            return SecurityEvaluation(
                decision=PermissionDecision.DENY,
                risk_level=RiskLevel.CRITICAL,
                operation=OperationType.READ,
                reason=f"Path traversal or device namespace in skill name: '{name}'.",
                tool_name=tool_name,
                target=name,
                rule_id="SKILL_PATH_TRAVERSAL",
            )

        if _is_sensitive_skill_file(name):
            return SecurityEvaluation(
                decision=PermissionDecision.DENY,
                risk_level=RiskLevel.CRITICAL,
                operation=OperationType.READ,
                reason=f"Access to sensitive file '{name}' denied.",
                tool_name=tool_name,
                target=name,
                rule_id="SKILL_SENSITIVE_FILE",
            )

        # 2. Check file_path if requested
        if file_path:
            file_eval = self._validate_linked_file_arg(file_path, tool_name)
            if not file_eval.allowed:
                return file_eval

        # 3. Check unauthorized root, symlink escape, or junction escape
        esc = self._check_target_containment_and_escapes(name, file_path)
        if esc is not None:
            rule_id, reason, risk = esc
            return SecurityEvaluation(
                decision=PermissionDecision.DENY,
                risk_level=risk,
                operation=OperationType.READ,
                reason=reason,
                tool_name=tool_name,
                target=name,
                rule_id=rule_id,
            )

        return SecurityEvaluation(
            decision=PermissionDecision.ALLOW,
            risk_level=RiskLevel.LOW,
            operation=OperationType.READ,
            reason="skill_view operation approved.",
            tool_name=tool_name,
            target=f"{name}:{file_path}" if file_path else name,
            rule_id="SKILL_VIEW_OK",
        )

    def _validate_linked_file_arg(self, file_path: str, tool_name: str) -> SecurityEvaluation:
        """Validate safety of linked file path argument."""
        if "\x00" in file_path:
            return SecurityEvaluation(
                decision=PermissionDecision.DENY,
                risk_level=RiskLevel.CRITICAL,
                operation=OperationType.READ,
                reason=f"Null byte detected in linked file: '{file_path}'.",
                tool_name=tool_name,
                target=file_path,
                rule_id="SKILL_NULL_BYTE",
            )

        if _has_traversal(file_path):
            return SecurityEvaluation(
                decision=PermissionDecision.DENY,
                risk_level=RiskLevel.CRITICAL,
                operation=OperationType.READ,
                reason=f"Path traversal ('..') detected in linked file: '{file_path}'.",
                tool_name=tool_name,
                target=file_path,
                rule_id="SKILL_PATH_TRAVERSAL",
            )

        win_p = PureWindowsPath(file_path)
        posix_p = PurePosixPath(file_path)
        if win_p.is_absolute() or posix_p.is_absolute() or win_p.drive or _NT_DEVICE_PREFIX_RE.match(file_path):
            return SecurityEvaluation(
                decision=PermissionDecision.DENY,
                risk_level=RiskLevel.CRITICAL,
                operation=OperationType.READ,
                reason=f"Absolute path or device namespace in linked file: '{file_path}'.",
                tool_name=tool_name,
                target=file_path,
                rule_id="SKILL_ABSOLUTE_PATH",
            )

        if _is_sensitive_skill_file(file_path):
            return SecurityEvaluation(
                decision=PermissionDecision.DENY,
                risk_level=RiskLevel.CRITICAL,
                operation=OperationType.READ,
                reason=f"Access to sensitive file '{file_path}' denied.",
                tool_name=tool_name,
                target=file_path,
                rule_id="SKILL_SENSITIVE_FILE",
            )

        return SecurityEvaluation(
            decision=PermissionDecision.ALLOW,
            risk_level=RiskLevel.LOW,
            operation=OperationType.READ,
            reason="Linked file path argument safe.",
            tool_name=tool_name,
            target=file_path,
            rule_id="SKILL_FILE_OK",
        )

    def _check_target_containment_and_escapes(
        self, name: str, file_path: str = ""
    ) -> Optional[tuple[str, str, RiskLevel]]:
        """Check whether target is outside approved roots, or is a symlink/junction escape."""
        if name.startswith(("/", "\\")) or (len(name) > 1 and name[1] == ":"):
            is_in, _ = self.is_within_approved_skill_root(name)
            if not is_in:
                return (
                    "SKILL_ROOT_UNAUTHORIZED",
                    f"Access outside approved skill roots denied: '{name}'.",
                    RiskLevel.HIGH,
                )

        approved_roots = self.get_approved_skill_roots()
        for root in approved_roots:
            esc = self._check_root_candidate_escape(root, name, file_path, approved_roots)
            if esc is not None:
                return esc
        return None

    @staticmethod
    def _check_root_candidate_escape(
        root: Path, name: str, file_path: str, approved_roots: tuple[Path, ...]
    ) -> Optional[tuple[str, str, RiskLevel]]:
        target_dir = root / name
        targets = (target_dir / file_path, target_dir) if file_path else (target_dir,)
        for target in targets:
            is_link = False
            is_junc = False
            try:
                is_link = target.is_symlink()
                is_junc = hasattr(target, "is_junction") and target.is_junction()
            except OSError:
                pass

            if not (target.exists() or is_link or is_junc):
                continue

            real_target = _resolve_real_target(target)
            if not any(_is_within_root(real_target, r) for r in approved_roots):
                if is_junc:
                    return (
                        "SKILL_JUNCTION_ESCAPE",
                        f"Windows junction escape blocked: '{target}' resolves to '{real_target}'.",
                        RiskLevel.CRITICAL,
                    )
                if is_link:
                    return (
                        "SKILL_SYMLINK_ESCAPE",
                        f"Symlink escape blocked: '{target}' resolves to '{real_target}'.",
                        RiskLevel.CRITICAL,
                    )
                return (
                    "SKILL_ROOT_UNAUTHORIZED",
                    f"Target '{target}' resolves to '{real_target}' outside approved roots.",
                    RiskLevel.HIGH,
                )
        return None

    def evaluate_skill_manage(self, tool_name: str, args: Mapping[str, Any]) -> SecurityEvaluation:
        """Evaluate skill_manage call (Classification: WRITE or DELETE with per-op analysis)."""
        ops = self._extract_operations(args)
        if not ops:
            return SecurityEvaluation(
                decision=PermissionDecision.DENY,
                risk_level=RiskLevel.HIGH,
                operation=OperationType.WRITE,
                reason="skill_manage requires at least one operation.",
                tool_name=tool_name,
                target="",
                rule_id="SKILL_MANAGE_EMPTY",
            )

        sub_evaluations: list[SecurityEvaluation] = []
        for op in ops:
            sub_eval = self._evaluate_single_op(op, tool_name)
            sub_evaluations.append(sub_eval)
            if not sub_eval.allowed:
                return sub_eval  # Fail-closed immediately on first violation

        # Aggregate batch evaluation
        return self._aggregate_batch_evaluations(sub_evaluations, tool_name)

    @staticmethod
    def _extract_operations(args: Mapping[str, Any]) -> list[dict[str, Any]]:
        """Normalize batch operations array or single legacy flat action."""
        if "operations" in args and isinstance(args["operations"], (list, tuple)):
            return [dict(o) for o in args["operations"] if isinstance(o, Mapping)]
        if "action" in args:
            return [dict(args)]
        return []

    def _evaluate_single_op(self, op: dict[str, Any], tool_name: str) -> SecurityEvaluation:
        """Evaluate a single skill_manage operation independently."""
        action = str(op.get("action") or "").strip().lower()
        name = str(op.get("name") or "").strip()
        file_path = str(op.get("file_path") or "").strip()
        content = op.get("content") or op.get("new_string") or op.get("file_content") or ""

        # 1. Action classification
        if action in ("create", "edit", "patch", "write_file"):
            op_type = OperationType.WRITE
            risk = RiskLevel.MEDIUM
        elif action in ("delete", "remove_file"):
            op_type = OperationType.DELETE
            risk = RiskLevel.HIGH
        else:
            return SecurityEvaluation(
                decision=PermissionDecision.DENY,
                risk_level=RiskLevel.HIGH,
                operation=OperationType.OTHER,
                reason=f"Unknown skill_manage action: '{action}'.",
                tool_name=tool_name,
                target=name,
                rule_id="SKILL_ACTION_UNKNOWN",
            )

        # 2. Skill identity validation
        if not name:
            return SecurityEvaluation(
                decision=PermissionDecision.DENY,
                risk_level=RiskLevel.HIGH,
                operation=op_type,
                reason="Skill operation requires a non-empty 'name'.",
                tool_name=tool_name,
                target="",
                rule_id="SKILL_NAME_EMPTY",
            )

        if "\x00" in name or "\x00" in file_path:
            return SecurityEvaluation(
                decision=PermissionDecision.DENY,
                risk_level=RiskLevel.CRITICAL,
                operation=op_type,
                reason="Null byte detected in skill identifier or path.",
                tool_name=tool_name,
                target=name,
                rule_id="SKILL_NULL_BYTE",
            )

        if _NT_DEVICE_PREFIX_RE.match(name):
            return SecurityEvaluation(
                decision=PermissionDecision.DENY,
                risk_level=RiskLevel.CRITICAL,
                operation=op_type,
                reason=f"Device namespace or special path blocked in skill name: '{name}'.",
                tool_name=tool_name,
                target=name,
                rule_id="SKILL_NT_NAMESPACE",
            )

        if _has_traversal(name):
            return SecurityEvaluation(
                decision=PermissionDecision.DENY,
                risk_level=RiskLevel.CRITICAL,
                operation=op_type,
                reason=f"Path traversal or device namespace in skill name: '{name}'.",
                tool_name=tool_name,
                target=name,
                rule_id="SKILL_PATH_TRAVERSAL",
            )

        if _is_sensitive_skill_file(name):
            return SecurityEvaluation(
                decision=PermissionDecision.DENY,
                risk_level=RiskLevel.CRITICAL,
                operation=op_type,
                reason=f"Modification of sensitive file '{name}' denied.",
                tool_name=tool_name,
                target=name,
                rule_id="SKILL_SENSITIVE_FILE",
            )

        # 3. Pinned / protected skills protection
        if action in ("edit", "patch", "write_file", "delete", "remove_file"):
            if self.is_pinned_or_protected(name):
                return SecurityEvaluation(
                    decision=PermissionDecision.DENY,
                    risk_level=RiskLevel.CRITICAL,
                    operation=op_type,
                    reason=f"Skill '{name}' is pinned/protected and cannot be modified or deleted.",
                    tool_name=tool_name,
                    target=name,
                    rule_id="SKILL_PINNED_PROTECTED",
                )

        # 4. Check file_path safety
        if file_path:
            file_eval = self._validate_linked_file_arg(file_path, tool_name)
            if not file_eval.allowed:
                return SecurityEvaluation(
                    decision=PermissionDecision.DENY,
                    risk_level=file_eval.risk_level,
                    operation=op_type,
                    reason=file_eval.reason,
                    tool_name=tool_name,
                    target=f"{name}/{file_path}",
                    rule_id=file_eval.rule_id,
                )

        # 5. Check unauthorized root, symlink escape, or junction escape
        esc = self._check_target_containment_and_escapes(name, file_path)
        if esc is not None:
            rule_id, reason, risk_lvl = esc
            return SecurityEvaluation(
                decision=PermissionDecision.DENY,
                risk_level=risk_lvl,
                operation=op_type,
                reason=reason,
                tool_name=tool_name,
                target=name,
                rule_id=rule_id,
            )

        # 6. Payload safety checks on content
        if isinstance(content, str) and content:
            payload_eval = self._check_content_payload(content, tool_name, name, op_type)
            if not payload_eval.allowed:
                return payload_eval

        requires_confirm = (
            (op_type == OperationType.DELETE and self.config.require_confirmation_for_delete)
            or (op_type == OperationType.WRITE and self.config.require_confirmation_for_write)
        )
        return SecurityEvaluation(
            decision=PermissionDecision.ALLOW,
            risk_level=risk,
            operation=op_type,
            reason=f"Skill operation '{action}' validated.",
            tool_name=tool_name,
            target=f"{action}:{name}" + (f"/{file_path}" if file_path else ""),
            rule_id=f"SKILL_{action.upper()}_OK",
            requires_confirmation=requires_confirm,
        )

    def _check_content_payload(
        self,
        content: str,
        tool_name: str,
        skill_name: str,
        op_type: OperationType,
    ) -> SecurityEvaluation:
        """Check content payload for dangerous script patterns or sensitive references."""
        for pattern, desc in _DANGEROUS_PAYLOAD_PATTERNS:
            if pattern.search(content):
                return SecurityEvaluation(
                    decision=PermissionDecision.DENY,
                    risk_level=RiskLevel.CRITICAL,
                    operation=op_type,
                    reason=f"Skill content contains forbidden hazardous pattern: {desc}.",
                    tool_name=tool_name,
                    target=skill_name,
                    rule_id="SKILL_DANGEROUS_PAYLOAD",
                )

        return SecurityEvaluation(
            decision=PermissionDecision.ALLOW,
            risk_level=RiskLevel.LOW,
            operation=op_type,
            reason="Payload check passed.",
            tool_name=tool_name,
            target=skill_name,
            rule_id="SKILL_PAYLOAD_OK",
        )

    @staticmethod
    def _aggregate_batch_evaluations(
        evals: Sequence[SecurityEvaluation],
        tool_name: str,
    ) -> SecurityEvaluation:
        """Aggregate multiple operation evaluations, prioritizing DELETE and highest risk."""
        has_delete = any(e.operation == OperationType.DELETE for e in evals)
        has_write = any(e.operation == OperationType.WRITE for e in evals)
        requires_confirm = any(e.requires_confirmation for e in evals)

        op_type = OperationType.DELETE if has_delete else (OperationType.WRITE if has_write else OperationType.READ)
        max_risk = max((e.risk_level for e in evals), key=lambda r: ("low", "medium", "high", "critical").index(r.value.lower()))

        targets = [e.target for e in evals]
        return SecurityEvaluation(
            decision=PermissionDecision.ALLOW,
            risk_level=max_risk,
            operation=op_type,
            reason=f"Skill batch ({len(evals)} ops) validated successfully.",
            tool_name=tool_name,
            target="; ".join(targets[:3]) + (f" (+{len(targets)-3} more)" if len(targets) > 3 else ""),
            rule_id="SKILL_BATCH_OK",
            requires_confirmation=requires_confirm,
        )
