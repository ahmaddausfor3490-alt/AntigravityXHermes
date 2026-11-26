"""Configuration loading and fail-closed validation for APA Security Guard."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional

from agent.runtime_cwd import resolve_agent_cwd
from hermes_cli.config import load_config
from hermes_constants import get_hermes_home

from .models import PolicyMode

DEFAULT_PROTECTED_FILENAMES: frozenset[str] = frozenset(
    {
        ".env",
        ".env.local",
        ".env.production",
        ".env.development",
        ".env.staging",
        ".env.test",
        "id_rsa",
        "id_ed25519",
        "id_ecdsa",
        "id_dsa",
        "authorized_keys",
        "known_hosts",
        "credentials.json",
        "client_secret.json",
        "service_account.json",
        "oauth_creds.json",
        "auth.json",
        "secrets.yaml",
        "secrets.yml",
        "secrets.json",
        ".npmrc",
        ".pypirc",
        ".netrc",
        ".git-credentials",
        "apa_security_audit.jsonl",
    }
)

DEFAULT_PROTECTED_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".pem",
        ".key",
        ".pfx",
        ".p12",
        ".kdbx",
        ".keystore",
        ".jks",
        ".ovpn",
        ".der",
        ".cer",
    }
)

DEFAULT_PROTECTED_DIRECTORIES: frozenset[str] = frozenset(
    {
        ".ssh",
        ".aws",
        ".gnupg",
        ".kube",
        ".gemini",
        "antigravity-cli",
        ".hermes",
        "System32",
        "SysWOW64",
    }
)

DEFAULT_IMMUTABLE_SECURITY_PATHS: frozenset[str] = frozenset(
    {
        "plugins/apa-security",
        "plugins\\apa-security",
        "apa_security_audit.jsonl",
    }
)

_BOOL_FIELDS: tuple[str, ...] = (
    "enabled",
    "enforce_workspace_boundary",
    "allow_read",
    "allow_write",
    "allow_delete",
    "allow_execute",
    "allow_network",
    "allow_subagents",
    "require_confirmation_for_write",
    "require_confirmation_for_delete",
    "require_confirmation_for_execute",
    "require_confirmation_for_high_risk",
    "audit_enabled",
)

_VALID_UNATTENDED_ACTIONS: frozenset[str] = frozenset({"deny", "approve_hook"})


def _default_workspace_root() -> Path:
    try:
        return Path(str(resolve_agent_cwd())).resolve()
    except (OSError, RuntimeError, ValueError):
        return Path.cwd().resolve()


def _default_audit_log_path() -> Path:
    try:
        return (get_hermes_home() / "logs" / "apa_security_audit.jsonl").resolve()
    except (OSError, RuntimeError, ValueError):
        return (Path.cwd() / "logs" / "apa_security_audit.jsonl").resolve()


def _parse_str_tuple(raw: Any, field_name: str, errors: list[str]) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, (list, tuple, set, frozenset)):
        errors.append(f"{field_name} must be a list of strings, got {type(raw).__name__}")
        return ()
    items: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            errors.append(f"{field_name} entries must be strings, got {type(item).__name__}")
            continue
        cleaned = item.strip()
        if cleaned:
            items.append(cleaned)
    return tuple(items)


def _parse_path_tuple(raw: Any, field_name: str, errors: list[str]) -> tuple[Path, ...]:
    strs = _parse_str_tuple(raw, field_name, errors)
    paths: list[Path] = []
    for item in strs:
        try:
            paths.append(Path(item).expanduser().resolve())
        except (OSError, RuntimeError, ValueError) as exc:
            errors.append(f"Invalid path in {field_name} ({item!r}): {exc}")
    return tuple(paths)


def _parse_bool_fields(data: Mapping[str, Any], kwargs: dict[str, Any], errors: list[str]) -> None:
    for bfield in _BOOL_FIELDS:
        val = data.get(bfield)
        if val is None:
            continue
        if not isinstance(val, bool):
            errors.append(f"{bfield} must be a boolean, got {type(val).__name__}")
        else:
            kwargs[bfield] = val


def _parse_policy_and_action_fields(data: Mapping[str, Any], kwargs: dict[str, Any], errors: list[str]) -> None:
    raw_mode = data.get("policy_mode", PolicyMode.STANDARD.value)
    if isinstance(raw_mode, PolicyMode):
        kwargs["policy_mode"] = raw_mode
    elif isinstance(raw_mode, str):
        try:
            kwargs["policy_mode"] = PolicyMode(raw_mode.strip().lower())
        except ValueError:
            errors.append(f"Invalid policy_mode {raw_mode!r}; must be strict, standard, or permissive")
    else:
        errors.append(f"policy_mode must be a string, got {type(raw_mode).__name__}")

    raw_unattended = data.get("unattended_confirmation_action")
    if raw_unattended is not None:
        if not isinstance(raw_unattended, str) or raw_unattended.strip().lower() not in _VALID_UNATTENDED_ACTIONS:
            errors.append(
                f"unattended_confirmation_action must be one of {sorted(_VALID_UNATTENDED_ACTIONS)}, "
                f"got {raw_unattended!r}"
            )
        else:
            kwargs["unattended_confirmation_action"] = raw_unattended.strip().lower()


def _parse_single_path_field(
    data: Mapping[str, Any], key: str, kwargs: dict[str, Any], errors: list[str]
) -> None:
    raw_val = data.get(key)
    if raw_val is None or raw_val == "":
        return
    if not isinstance(raw_val, (str, Path)):
        errors.append(f"{key} must be a string or Path, got {type(raw_val).__name__}")
        return
    if "\x00" in str(raw_val):
        errors.append(f"{key} contains a null byte")
        return
    try:
        kwargs[key] = Path(raw_val).expanduser().resolve()
    except (OSError, RuntimeError, ValueError) as exc:
        errors.append(f"Invalid {key} {raw_val!r}: {exc}")


def _parse_collection_fields(data: Mapping[str, Any], kwargs: dict[str, Any], errors: list[str]) -> None:
    if "allowed_roots" in data:
        kwargs["allowed_roots"] = _parse_path_tuple(data.get("allowed_roots"), "allowed_roots", errors)
    if "allowed_skill_roots" in data:
        kwargs["allowed_skill_roots"] = _parse_path_tuple(data.get("allowed_skill_roots"), "allowed_skill_roots", errors)
    if "pinned_skills" in data:
        kwargs["pinned_skills"] = frozenset(_parse_str_tuple(data.get("pinned_skills"), "pinned_skills", errors))
    for str_list_key in ("blocked_paths", "blocked_commands", "allowed_commands"):
        if str_list_key in data:
            kwargs[str_list_key] = _parse_str_tuple(data.get(str_list_key), str_list_key, errors)
    if data.get("protected_filenames") is not None:
        extra_names = _parse_str_tuple(data["protected_filenames"], "protected_filenames", errors)
        kwargs["protected_filenames"] = DEFAULT_PROTECTED_FILENAMES | frozenset(extra_names)
    if data.get("protected_extensions") is not None:
        extra_exts = _parse_str_tuple(data["protected_extensions"], "protected_extensions", errors)
        kwargs["protected_extensions"] = DEFAULT_PROTECTED_EXTENSIONS | frozenset(
            e.lower() if e.startswith(".") else f".{e.lower()}" for e in extra_exts
        )
    if data.get("protected_directories") is not None:
        extra_dirs = _parse_str_tuple(data["protected_directories"], "protected_directories", errors)
        kwargs["protected_directories"] = DEFAULT_PROTECTED_DIRECTORIES | frozenset(extra_dirs)


def _merge_mapping_section(target: dict[str, Any], parent: Any, key: str) -> None:
    if isinstance(parent, Mapping):
        sub = parent.get(key)
        if isinstance(sub, Mapping):
            target.update(sub)


@dataclass(frozen=True)
class APASecurityConfig:
    """Validated, immutable configuration for APA Security Guard."""

    enabled: bool = True
    policy_mode: PolicyMode = PolicyMode.STANDARD
    workspace_root: Path = field(default_factory=_default_workspace_root)
    enforce_workspace_boundary: bool = True
    allow_read: bool = True
    allow_write: bool = True
    allow_delete: bool = False
    allow_execute: bool = True
    allow_network: bool = True
    allow_subagents: bool = True
    require_confirmation_for_write: bool = False
    require_confirmation_for_delete: bool = True
    require_confirmation_for_execute: bool = False
    require_confirmation_for_high_risk: bool = True
    unattended_confirmation_action: str = "approve_hook"
    allowed_roots: tuple[Path, ...] = ()
    allowed_skill_roots: tuple[Path, ...] = ()
    pinned_skills: frozenset[str] = field(default_factory=frozenset)
    blocked_paths: tuple[str, ...] = ()
    blocked_commands: tuple[str, ...] = ()
    allowed_commands: tuple[str, ...] = ()
    protected_filenames: frozenset[str] = DEFAULT_PROTECTED_FILENAMES
    protected_extensions: frozenset[str] = DEFAULT_PROTECTED_EXTENSIONS
    protected_directories: frozenset[str] = DEFAULT_PROTECTED_DIRECTORIES
    immutable_security_paths: frozenset[str] = DEFAULT_IMMUTABLE_SECURITY_PATHS
    audit_enabled: bool = True
    audit_log_path: Path = field(default_factory=_default_audit_log_path)
    fail_closed_reason: Optional[str] = None

    @classmethod
    def from_mapping(
        cls,
        data: Optional[Mapping[str, Any]] = None,
        *,
        strict: bool = False,
    ) -> APASecurityConfig:
        """Build an :class:`APASecurityConfig` from a dictionary with fail-closed validation."""
        if data is None:
            return cls()
        if not isinstance(data, Mapping):
            msg = f"Security config must be a mapping, got {type(data).__name__}"
            if strict:
                raise ValueError(msg)
            return cls(fail_closed_reason=msg)

        errors: list[str] = []
        kwargs: dict[str, Any] = {}
        _parse_bool_fields(data, kwargs, errors)
        _parse_policy_and_action_fields(data, kwargs, errors)
        _parse_single_path_field(data, "workspace_root", kwargs, errors)
        _parse_single_path_field(data, "audit_log_path", kwargs, errors)
        _parse_collection_fields(data, kwargs, errors)

        if errors:
            reason = "; ".join(errors)
            if strict:
                raise ValueError(f"Invalid APA security configuration: {reason}")
            kwargs["fail_closed_reason"] = f"Invalid security configuration (fail-closed): {reason}"

        return cls(**kwargs)

    @classmethod
    def load_from_hermes(cls, ctx: Any = None) -> APASecurityConfig:
        """Load configuration from Hermes ``config.yaml`` and plugin settings."""
        merged: dict[str, Any] = {}
        try:
            raw_cfg = load_config() or {}
        except (OSError, RuntimeError, ValueError):
            raw_cfg = {}

        if isinstance(raw_cfg, Mapping):
            _merge_mapping_section(merged, raw_cfg.get("security"), "apa")
            plugins_sec = raw_cfg.get("plugins")
            entries = plugins_sec.get("entries") if isinstance(plugins_sec, Mapping) else None
            entry = entries.get("apa-security") if isinstance(entries, Mapping) else None
            _merge_mapping_section(merged, entry, "settings")
            _merge_mapping_section(merged, entry, "config")

        if ctx is not None and hasattr(ctx, "get_config"):
            for key in (
                *_BOOL_FIELDS,
                "policy_mode",
                "workspace_root",
                "unattended_confirmation_action",
                "allowed_roots",
                "blocked_paths",
                "blocked_commands",
                "allowed_commands",
                "audit_log_path",
            ):
                try:
                    val = ctx.get_config(key, None)
                except (OSError, RuntimeError, ValueError):
                    val = None
                if val is not None:
                    merged[key] = val

        return cls.from_mapping(merged)
