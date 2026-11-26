"""Security validation, path traversal defense, and containment enforcement for APA Skills (Phase 4.3)."""

from __future__ import annotations

import logging
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
from typing import Any, Mapping, Optional

from .models import SkillSecurityError

logger = logging.getLogger(__name__)

# Pattern matching Windows NT object namespace and device prefixes
_NT_DEVICE_PREFIX_RE = re.compile(
    r"^(?:\\\\\.\\|\\\\\?\\(?:UNC|GLOBALROOT|Volume|PhysicalDrive)|\\\?\?\\)",
    re.IGNORECASE,
)

# Sensitive files that must never be loaded as skill references or supporting assets
_SENSITIVE_FILE_NAMES = frozenset({
    ".env",
    ".git",
    ".gitignore",
    ".gitmodules",
    ".ssh",
    ".aws",
    ".azure",
    "id_rsa",
    "id_ed25519",
    "id_ecdsa",
    "id_dsa",
    "authorized_keys",
    "known_hosts",
    "credentials",
    "credentials.json",
    "secrets.json",
    "config.yaml",
    ".bash_history",
    ".zsh_history",
    "SAM",
    "SYSTEM",
    "SECURITY",
})

_SENSITIVE_EXTENSIONS = frozenset({
    ".pem",
    ".key",
    ".pfx",
    ".p12",
    ".kdbx",
})


def has_traversal_component(path_str: str) -> bool:
    """Return True if path contains any '..' traversal component."""
    normalized = path_str.replace("\\", "/")
    parts = normalized.split("/")
    return ".." in parts


def is_sensitive_file(path_or_name: str | Path) -> bool:
    """Check if a path or filename targets sensitive configuration, credential, or VCS data."""
    p = Path(path_or_name)
    name_lower = p.name.lower()

    if name_lower in {s.lower() for s in _SENSITIVE_FILE_NAMES}:
        return True

    if p.suffix.lower() in _SENSITIVE_EXTENSIONS:
        return True

    # Also check if any parent directory is a sensitive namespace
    for part in p.parts:
        if part.lower() in {".ssh", ".aws", ".azure", ".git"}:
            return True

    return False


def validate_skill_path_safety(
    candidate: str | Path,
    approved_root: Optional[Path] = None,
    *,
    allow_absolute: bool = False,
) -> None:
    """Validate path safety against traversal, device namespaces, and boundary escapes.

    Raises:
        SkillSecurityError: If any security boundary is breached.
    """
    raw_str = str(candidate).strip()
    if not raw_str:
        raise SkillSecurityError("Path cannot be empty.")

    # 1. Null byte or control character injection
    if "\x00" in raw_str or any(ord(c) < 32 for c in raw_str if c not in "\t\n\r"):
        raise SkillSecurityError(f"Path contains forbidden control characters or null bytes: {raw_str!r}")

    # 2. Windows NT Device Namespace / UNC / Volume device escape
    if _NT_DEVICE_PREFIX_RE.match(raw_str):
        raise SkillSecurityError(f"Path uses forbidden NT device namespace: {raw_str}")

    # 3. Path traversal ('..')
    if has_traversal_component(raw_str):
        raise SkillSecurityError(f"Path traversal ('..') detected: {raw_str}")

    # 4. Absolute paths or drive prefixes when disallowed
    if not allow_absolute:
        win_path = PureWindowsPath(raw_str)
        posix_path = PurePosixPath(raw_str)
        if win_path.is_absolute() or posix_path.is_absolute() or win_path.drive:
            raise SkillSecurityError(f"Absolute path or drive prefix not allowed: {raw_str}")

    # 5. Sensitive file access check
    if is_sensitive_file(raw_str):
        raise SkillSecurityError(f"Access to sensitive file or credential path forbidden: {raw_str}")

    # 6. Approved root boundary containment (including symlink and junction escape)
    if approved_root is not None:
        try:
            target_path = Path(raw_str)
            if not target_path.is_absolute():
                target_path = approved_root / target_path

            resolved_target = target_path.resolve()
            resolved_root = approved_root.resolve()

            # Ensure resolved_target is located within resolved_root
            resolved_target.relative_to(resolved_root)
        except (ValueError, RuntimeError) as exc:
            raise SkillSecurityError(
                f"Path '{raw_str}' escapes approved boundary '{approved_root}': {exc}"
            ) from exc


def check_symlink_or_junction_escape(path: Path, boundary_root: Path) -> bool:
    """Return True if path resolves outside the approved boundary root via symlink or junction."""
    try:
        resolved_path = path.resolve()
        resolved_boundary = boundary_root.resolve()
        resolved_path.relative_to(resolved_boundary)
        return False
    except (ValueError, RuntimeError, OSError):
        return True


def record_skill_security_audit(
    action: str,
    target: str,
    decision: str,
    reason: str,
    details: Optional[Mapping[str, Any]] = None,
) -> None:
    """Record a security-relevant event to the audit log."""
    extra = dict(details or {})
    logger.info(
        "APA_SKILL_SECURITY_AUDIT: action=%s target=%s decision=%s reason=%s details=%s",
        action,
        target,
        decision,
        reason,
        extra,
    )
    # Attempt forwarding to apa-security AuditLogger if registered in process
    try:
        import sys
        for mod_name in list(sys.modules.keys()):
            if "apa_security" in mod_name and hasattr(sys.modules[mod_name], "get_active_interceptor"):
                interceptor = sys.modules[mod_name].get_active_interceptor()
                if interceptor and getattr(interceptor, "audit_logger", None):
                    from plugins.apa_security.models import AuditEvent  # type: ignore[import-not-found]
                    interceptor.audit_logger.log_event(
                        AuditEvent(
                            tool_name="apa_skills",
                            operation=action,
                            target=target,
                            risk_level="high" if decision == "denied" else "low",
                            extra={"decision": decision, "reason": reason, **extra},
                        )
                    )
                break
    except Exception as exc:
        logger.debug("Could not dispatch audit event to apa-security: %s", exc, exc_info=True)
