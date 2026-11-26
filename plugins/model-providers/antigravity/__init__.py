"""Google Antigravity model provider plugin for Hermes Agent (APA).

Registers ``antigravity`` as a first-class ``external_process`` model provider
backed by the official ``agy`` CLI (`agy acp --stdio` + Google OAuth).
"""

from __future__ import annotations

import logging
from pathlib import Path
import sys
import types
from typing import Any, Callable

# Ensure parent namespace packages exist when loaded via spec_from_file_location
# under "plugins.model_providers.antigravity" where "model-providers" has a hyphen on disk.
if __package__:
    _parts = __package__.split(".")
    for _idx in range(1, len(_parts)):
        _parent_name = ".".join(_parts[:_idx])
        if _parent_name not in sys.modules:
            _pkg_mod = types.ModuleType(_parent_name)
            _pkg_mod.__path__ = [str(Path(__file__).resolve().parents[len(_parts) - _idx])]
            sys.modules[_parent_name] = _pkg_mod

from providers import register_provider
from providers.base import ProviderProfile

from .acp_transport import (
    AGYACPTransport,
    AntigravityACPError,
    AntigravityAuthError,
    coerce_timeout_seconds,
)
from .bridge import (
    StreamingToolCallBuffer,
    build_chat_completion,
    extract_tool_calls_from_text,
    format_prompt_for_session,
)
from .client import (
    ANTIGRAVITY_FALLBACK_MODELS,
    DEFAULT_ANTIGRAVITY_MODEL,
    AntigravityClient,
)
from .process_manager import (
    AGYAuthStatus,
    AGYProcessManager,
    AGYVersionInfo,
    ARGS_ENV_VAR,
    COMMAND_ENV_VARS,
    DEFAULT_ACP_ARGS,
    build_agy_env,
    parse_models_output,
    resolve_agy_executable,
)

logger = logging.getLogger(__name__)

ANTIGRAVITY_MODEL_ALIASES: dict[str, str] = {
    "argon": "gemini-4-argon-medium",
    "gemini-4-argon": "gemini-4-argon-medium",
    "hydrogen": "gemini-4-hydrogen-high",
    "gemini-4-hydrogen": "gemini-4-hydrogen-high",
    "gemini-3.8-flash": "gemini-3.8-flash-medium",
    "claude-opus-5-5": "claude-opus-5-5-high",
    "claude-sonnet-5-5": "claude-sonnet-5-5-high",
}


def _auth_action_login(pm: AGYProcessManager) -> bool:
    code = pm.run_login()
    return code == 0


def _auth_action_logout(pm: AGYProcessManager) -> bool:
    code = pm.run_logout()
    return code == 0


def _auth_action_status(pm: AGYProcessManager) -> bool:
    status = pm.check_auth_status()
    return bool(status.installed and status.authenticated)


_AUTH_ACTION_DISPATCH: dict[str, Callable[[AGYProcessManager], bool]] = {
    "add": _auth_action_login,
    "login": _auth_action_login,
    "refresh": _auth_action_status,
    "status": _auth_action_status,
    "logout": _auth_action_logout,
    "remove": _auth_action_logout,
}


def handle_antigravity_auth(action: str, _args: Any = None) -> bool:
    """Delegate provider authentication actions to official ``agy`` CLI commands."""
    normalized = str(action or "status").strip().lower()
    handler = _AUTH_ACTION_DISPATCH.get(normalized, _auth_action_status)
    pm = AGYProcessManager()
    try:
        return handler(pm)
    except (FileNotFoundError, OSError, RuntimeError) as exc:
        logger.warning("Antigravity auth action '%s' failed: %s", normalized, exc)
        return False


def classify_antigravity_api_error(error_str: str) -> str | None:
    """Classify AGY authentication failures for Hermes error recovery."""
    lowered = (error_str or "").lower()
    auth_phrases = (
        "authentication required",
        "auth required",
        "not logged in",
        "agy login",
        "unauthenticated",
        "-32000",
        "-32001",
    )
    if any(phrase in lowered for phrase in auth_phrases):
        return "auth"
    return None


class AntigravityProfile(ProviderProfile):
    """ProviderProfile implementation for Google Antigravity via official AGY CLI."""

    def resolve_model(self, model: str) -> str:
        """Resolve shorthand model aliases (e.g. 'argon') to canonical AGY model IDs."""
        cleaned = (model or "").strip()
        if not cleaned:
            return DEFAULT_ANTIGRAVITY_MODEL
        return self.model_aliases.get(cleaned, cleaned)

    def resolve_aux_model(self, *, base_url: str = "", model: str = "") -> str:
        """Return the default auxiliary model for Antigravity."""
        del base_url, model
        return self.default_aux_model

    def create_client(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        command: str | None = None,
        args: list[str] | tuple[str, ...] | None = None,
        client_kwargs: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> AntigravityClient:
        del api_key, base_url
        extra = dict(client_kwargs or {})
        extra.update(kwargs)
        resolved_command = (
            command
            or extra.pop("command", None)
            or extra.pop("acp_command", None)
            or self.process_command
        )
        resolved_args = (
            args
            or extra.pop("args", None)
            or extra.pop("acp_args", None)
            or self.process_args
        )
        return AntigravityClient(
            command=resolved_command,
            args=resolved_args,
            default_model=str(extra.pop("default_model", DEFAULT_ANTIGRAVITY_MODEL)),
            **extra,
        )

    def setup_status(self) -> tuple[bool, str] | None:
        status = AGYProcessManager().check_auth_status()
        if not status.installed:
            return False, status.detail
        if not status.authenticated:
            return False, status.detail
        ver = f" ({status.version})" if status.version else ""
        return True, f"AGY CLI ready{ver}"

    def discover_models(self) -> list[str]:
        pm = AGYProcessManager()
        try:
            live = pm.fetch_live_models()
            if live:
                return live
        except (FileNotFoundError, OSError, RuntimeError) as exc:
            logger.debug("Live agy models discovery unavailable, using fallback list: %s", exc)
        return list(self.fallback_models)

    def fetch_models(self, *, api_key: str = "") -> list[str]:
        del api_key
        return self.discover_models()

    def supported_reasoning_efforts(self, model: str) -> tuple[str, ...] | None:
        del model
        return ("low", "medium", "high", "xhigh")


antigravity = AntigravityProfile(
    name="antigravity",
    aliases=("agy", "google-antigravity"),
    display_name="Google Antigravity (AGY)",
    description="Google Antigravity models via official AGY CLI (ACP stdio + Google OAuth)",
    signup_url="https://antigravity.google",
    auth_type="external_process",
    base_url="process://antigravity",
    supports_streaming=True,
    process_command="agy",
    process_args=DEFAULT_ACP_ARGS,
    process_command_env_vars=COMMAND_ENV_VARS,
    process_args_env_var=ARGS_ENV_VAR,
    login_args=("login",),
    default_aux_model="gemini-3.8-flash-low",
    fallback_models=ANTIGRAVITY_FALLBACK_MODELS,
    model_aliases=ANTIGRAVITY_MODEL_ALIASES,
    auth_handler=handle_antigravity_auth,
    classify_api_error=classify_antigravity_api_error,
)

register_provider(antigravity)

__all__ = [
    "ANTIGRAVITY_FALLBACK_MODELS",
    "ANTIGRAVITY_MODEL_ALIASES",
    "DEFAULT_ANTIGRAVITY_MODEL",
    "AGYACPTransport",
    "AGYAuthStatus",
    "AGYProcessManager",
    "AGYVersionInfo",
    "AntigravityACPError",
    "AntigravityAuthError",
    "AntigravityClient",
    "AntigravityProfile",
    "StreamingToolCallBuffer",
    "antigravity",
    "build_agy_env",
    "build_chat_completion",
    "classify_antigravity_api_error",
    "coerce_timeout_seconds",
    "extract_tool_calls_from_text",
    "format_prompt_for_session",
    "handle_antigravity_auth",
    "parse_models_output",
    "resolve_agy_executable",
]
