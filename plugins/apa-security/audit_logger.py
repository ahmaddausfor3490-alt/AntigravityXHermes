"""Thread-safe JSONL security audit logger with multi-layer credential redaction."""

from __future__ import annotations

import json
import logging
import re
import threading
from pathlib import Path
from typing import Any, Mapping

from agent.redact import redact_sensitive_text

from .config import APASecurityConfig
from .models import AuditEvent

logger = logging.getLogger(__name__)

_SECRET_FIELD_NAMES_LOWER: frozenset[str] = frozenset(
    {
        "api_key",
        "apikey",
        "secret",
        "secret_key",
        "token",
        "access_token",
        "refresh_token",
        "id_token",
        "password",
        "passwd",
        "pwd",
        "authorization",
        "auth_token",
        "client_secret",
        "private_key",
        "cookie",
        "set-cookie",
        "credential",
        "credentials",
        "session_token",
    }
)

_EXTRA_MASK_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    # PEM private keys
    (
        re.compile(
            r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
            re.MULTILINE,
        ),
        "[REDACTED_PRIVATE_KEY]",
    ),
    # Google OAuth access tokens (ya29.*) and refresh tokens (1//*)
    (re.compile(r"\bya29\.[0-9A-Za-z_\-]{12,}\b"), "[REDACTED_GOOGLE_OAUTH_TOKEN]"),
    (re.compile(r"\b1//[0-9A-Za-z_\-]{20,}\b"), "[REDACTED_GOOGLE_REFRESH_TOKEN]"),
    # GitHub personal/fine-grained tokens
    (re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"), "[REDACTED_GITHUB_TOKEN]"),
    # AWS Access Key IDs
    (re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), "[REDACTED_AWS_KEY]"),
    # Bearer / Basic Authorization headers
    (re.compile(r"(?i)\b(Bearer|Basic)\s+[A-Za-z0-9\-._~+/]+=*"), r"\1 [REDACTED_AUTH_TOKEN]"),
    # URI embedded credentials (scheme://user:password@host)
    (re.compile(r"([a-zA-Z][a-zA-Z0-9+.-]*://[^/\s:@]+:)([^@\s/]+)(@)"), r"\1[REDACTED]\3"),
    # Generic sk-... API keys
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}\b"), "[REDACTED_API_KEY]"),
)

_MAX_STRING_AUDIT_LEN = 500


def scrub_sensitive_string(value: str) -> str:
    """Redact credentials, tokens, keys, and secrets from *value*."""
    if not value:
        return ""
    scrubbed = value
    for pattern, replacement in _EXTRA_MASK_RULES:
        scrubbed = pattern.sub(replacement, scrubbed)
    try:
        scrubbed = redact_sensitive_text(
            scrubbed,
            force=True,
            file_read=True,
            secret_file=True,
            redact_url_credentials=True,
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        pass
    if len(scrubbed) > _MAX_STRING_AUDIT_LEN:
        scrubbed = scrubbed[: _MAX_STRING_AUDIT_LEN - 3] + "..."
    return scrubbed


def scrub_audit_payload(data: Any, *, depth: int = 0) -> Any:
    """Recursively scrub a dict/list/scalar before writing to the audit log."""
    if depth > 6:
        return "[TRUNCATED_DEPTH]"
    if isinstance(data, str):
        return scrub_sensitive_string(data)
    if isinstance(data, (int, float, bool)) or data is None:
        return data
    if isinstance(data, Mapping):
        out: dict[str, Any] = {}
        for k, v in data.items():
            key_str = str(k)
            key_lower = key_str.lower()
            if key_lower in _SECRET_FIELD_NAMES_LOWER or any(
                s in key_lower for s in ("password", "secret", "token", "api_key", "private_key")
            ):
                out[key_str] = "[REDACTED_SECRET]"
            elif key_lower in ("content", "file_text", "patch", "old_string", "new_string", "code"):
                # Never log raw file bodies or full code blocks in security audit logs
                length = len(str(v)) if v is not None else 0
                out[key_str] = f"[OMITTED_BODY:{length}_chars]"
            else:
                out[key_str] = scrub_audit_payload(v, depth=depth + 1)
        return out
    if isinstance(data, (list, tuple, set, frozenset)):
        items = list(data)[:25]
        return [scrub_audit_payload(item, depth=depth + 1) for item in items]
    return scrub_sensitive_string(str(data))


class AuditLogger:
    """Thread-safe JSONL audit logger for APA Security Guard events."""

    def __init__(self, config: APASecurityConfig) -> None:
        self.config = config
        self.log_path: Path = config.audit_log_path
        self._lock = threading.Lock()

    def log_event(self, event: AuditEvent) -> bool:
        """Scrub and append *event* as a JSON line to ``self.log_path``."""
        if not self.config.audit_enabled:
            return False

        record = event.to_dict()
        record["target"] = scrub_sensitive_string(str(record.get("target") or ""))
        record["reason"] = scrub_sensitive_string(str(record.get("reason") or ""))
        record["details"] = scrub_audit_payload(record.get("details") or {})

        try:
            serialized = json.dumps(record, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError) as exc:
            logger.warning("Failed to serialize APA security audit event: %s", exc)
            return False

        with self._lock:
            try:
                self.log_path.parent.mkdir(parents=True, exist_ok=True)
                with self.log_path.open("a", encoding="utf-8") as fh:
                    fh.write(serialized + "\n")
                return True
            except OSError as exc:
                logger.warning("Failed to write APA security audit log %s: %s", self.log_path, exc)
                return False

    def read_events(self, limit: int = 100) -> list[dict[str, Any]]:
        """Read up to the last *limit* audit events from ``self.log_path``."""
        if not self.log_path.is_file():
            return []
        with self._lock:
            try:
                lines = self.log_path.read_text(encoding="utf-8").splitlines()
            except OSError:
                return []
        tail = lines[-limit:] if limit > 0 else lines
        events: list[dict[str, Any]] = []
        for line in tail:
            if not line.strip():
                continue
            try:
                parsed = json.loads(line)
                if isinstance(parsed, dict):
                    events.append(parsed)
            except ValueError:
                continue
        return events

    def validate_log_file(self) -> tuple[bool, int, str]:
        """Verify that every non-empty line in ``self.log_path`` is valid JSON with required fields."""
        if not self.log_path.is_file():
            return True, 0, "Audit log file not created yet."
        required = {"timestamp", "tool_name", "operation", "decision", "risk_level", "status"}
        count = 0
        with self._lock:
            try:
                lines = self.log_path.read_text(encoding="utf-8").splitlines()
            except OSError as exc:
                return False, 0, f"Cannot read log file: {exc}"
        for idx, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except ValueError as exc:
                return False, count, f"Corrupted JSON at line {idx}: {exc}"
            if not isinstance(obj, dict) or not required.issubset(obj.keys()):
                return False, count, f"Missing required audit fields at line {idx}"
            count += 1
        return True, count, f"Validated {count} audit record(s)."
