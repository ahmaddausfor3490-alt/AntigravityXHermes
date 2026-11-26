"""Canonical path resolution, workspace boundary, and symlink/junction escape guard."""

from __future__ import annotations

import os
import re
from pathlib import Path

from agent.file_safety import get_read_block_error

from .config import APASecurityConfig
from .models import PathValidationResult, RiskLevel

_NT_DEVICE_PREFIX_RE = re.compile(
    r"^(?:\\\\\.\\|\\\\\?\\(?:UNC|GLOBALROOT|Volume|PhysicalDrive)|\\\?\?\\)",
    re.IGNORECASE,
)
_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:(?:[\\/]|$)")


def _norm_path(path: Path) -> str:
    """Return case-normalized, resolved path string without trailing separator."""
    s = os.path.normcase(os.path.normpath(str(path)))
    if len(s) > 3 and s.endswith(("\\", "/")):
        s = s.rstrip("\\/")
    return s


def _is_within_root(candidate: Path, root: Path) -> bool:
    """Return True if *candidate* is equal to or inside *root* (Windows-case-safe)."""
    cand_norm = _norm_path(candidate)
    root_norm = _norm_path(root)
    if cand_norm == root_norm:
        return True
    sep = "\\" if "\\" in root_norm else "/"
    prefix = root_norm if root_norm.endswith(sep) else root_norm + sep
    return cand_norm.startswith(prefix)


def _is_unc_path(raw: str) -> bool:
    """Detect Windows UNC share paths (``\\\\server\\share`` or ``//server/share``)."""
    stripped = raw.strip()
    if stripped.startswith(("\\\\?\\", "\\\\.\\")):
        if stripped.upper().startswith("\\\\?\\UNC\\"):
            return True
        return False
    if stripped.startswith("\\\\") or (os.name == "nt" and stripped.startswith("//")):
        return True
    return False


def _resolve_real_target(candidate: Path) -> Path:
    """Resolve symlinks and Windows NTFS junctions along *candidate* and its ancestors."""
    try:
        return Path(os.path.realpath(str(candidate)))
    except OSError:
        # Walk up to nearest existing ancestor and resolve realpath from there
        current = candidate
        tail_parts: list[str] = []
        while current != current.parent:
            if current.exists():
                try:
                    real_base = Path(os.path.realpath(str(current)))
                    for part in reversed(tail_parts):
                        real_base = real_base / part
                    return real_base
                except OSError:
                    break
            tail_parts.append(current.name)
            current = current.parent
        return candidate.resolve(strict=False)


def _has_reparse_or_symlink_in_chain(lexical_path: Path, workspace_root: Path) -> bool:
    """Return True if any path component between *workspace_root* and *lexical_path* is a symlink or junction."""
    current = lexical_path
    root_norm = _norm_path(workspace_root)
    visited = 0
    while visited < 64:
        visited += 1
        try:
            if current.is_symlink():
                return True
            if hasattr(current, "is_junction") and current.is_junction():
                return True
        except OSError:
            pass
        if _norm_path(current) == root_norm or current == current.parent:
            break
        current = current.parent
    return False


class PathValidator:
    """Validates paths against traversal, device namespaces, symlinks, junctions, and workspace boundaries."""

    def __init__(self, config: APASecurityConfig) -> None:
        self.config = config
        self.workspace_root = Path(os.path.realpath(str(config.workspace_root)))
        self.allowed_roots = tuple(Path(os.path.realpath(str(r))) for r in config.allowed_roots)

    def _is_in_allowed_boundary(self, target: Path) -> bool:
        if _is_within_root(target, self.workspace_root):
            return True
        return any(_is_within_root(target, root) for root in self.allowed_roots)

    def validate_path(self, raw_path: str) -> PathValidationResult:
        """Validate *raw_path* and return a :class:`PathValidationResult`."""
        if not isinstance(raw_path, str) or not raw_path.strip():
            return PathValidationResult(
                allowed=False,
                canonical_path="",
                reason="Path must be a non-empty string.",
                rule_id="PATH_EMPTY",
                risk_level=RiskLevel.HIGH,
            )

        if "\x00" in raw_path:
            return PathValidationResult(
                allowed=False,
                canonical_path="",
                reason="Null byte detected in path.",
                rule_id="PATH_NULL_BYTE",
                risk_level=RiskLevel.CRITICAL,
            )

        cleaned = raw_path.strip()

        # 1. Check Windows NT device / namespace prefixes before any Path resolution
        if _NT_DEVICE_PREFIX_RE.match(cleaned):
            return PathValidationResult(
                allowed=False,
                canonical_path=cleaned,
                reason=f"Blocked Windows device or NT namespace path: {cleaned}",
                rule_id="PATH_NT_NAMESPACE",
                risk_level=RiskLevel.CRITICAL,
            )

        # 2. Check UNC share paths
        if _is_unc_path(cleaned):
            return PathValidationResult(
                allowed=False,
                canonical_path=cleaned,
                reason=f"Blocked UNC network path: {cleaned}",
                rule_id="PATH_UNC_SHARE",
                risk_level=RiskLevel.CRITICAL,
            )

        # 3. Delegate to Hermes core file_safety check (ADS, DOS devices CON/NUL/COM1, etc.)
        block_err = get_read_block_error(cleaned)
        if block_err:
            return PathValidationResult(
                allowed=False,
                canonical_path=cleaned,
                reason=f"Blocked unsafe special path: {block_err}",
                rule_id="PATH_DEVICE_OR_ADS",
                risk_level=RiskLevel.CRITICAL,
            )

        # 4. Construct lexical path relative to workspace_root when relative
        try:
            expanded = Path(cleaned).expanduser()
            if not expanded.is_absolute() and not _WINDOWS_DRIVE_RE.match(cleaned):
                lexical_abs = Path(os.path.abspath(str(self.workspace_root / expanded)))
            else:
                lexical_abs = Path(os.path.abspath(str(expanded)))
            real_target = _resolve_real_target(lexical_abs)
        except (OSError, RuntimeError, ValueError) as exc:
            return PathValidationResult(
                allowed=False,
                canonical_path=cleaned,
                reason=f"Path resolution failed: {exc}",
                rule_id="PATH_RESOLUTION_ERROR",
                risk_level=RiskLevel.HIGH,
            )

        # 5. Detect symlink or Windows junction escape
        lexical_inside = self._is_in_allowed_boundary(lexical_abs)
        real_inside = self._is_in_allowed_boundary(real_target)
        has_link = _has_reparse_or_symlink_in_chain(lexical_abs, self.workspace_root)

        if (lexical_inside and not real_inside) or (has_link and not real_inside):
            return PathValidationResult(
                allowed=False,
                canonical_path=str(real_target),
                reason=(
                    f"Symlink or junction escape blocked: '{raw_path}' resolves to "
                    f"'{real_target}' outside workspace '{self.workspace_root}'."
                ),
                rule_id="PATH_SYMLINK_ESCAPE",
                risk_level=RiskLevel.CRITICAL,
            )

        # 6. Check workspace containment
        if self.config.enforce_workspace_boundary and not real_inside:
            has_traversal = ".." in Path(cleaned).parts or ".." in cleaned
            rule_id = "PATH_TRAVERSAL_OUTSIDE_WORKSPACE" if has_traversal else "PATH_OUTSIDE_WORKSPACE"
            return PathValidationResult(
                allowed=False,
                canonical_path=str(real_target),
                reason=(
                    f"Access outside workspace boundary denied: '{real_target}' "
                    f"is outside workspace '{self.workspace_root}'."
                ),
                rule_id=rule_id,
                risk_level=RiskLevel.HIGH,
            )

        return PathValidationResult(
            allowed=True,
            canonical_path=str(real_target),
            reason="Path validated within workspace boundary.",
            rule_id="PATH_OK",
            risk_level=RiskLevel.LOW,
        )
