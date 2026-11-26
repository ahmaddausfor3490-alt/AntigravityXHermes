"""Central security policy engine coordinating path, filesystem, command, and permission checks."""

from __future__ import annotations

import json
import re
from typing import Any, Callable, Mapping, Optional

from .command_validator import CommandValidator
from .config import APASecurityConfig
from .filesystem_guard import FilesystemGuard
from .models import OperationType, PermissionDecision, RiskLevel, SecurityEvaluation
from .path_validator import PathValidator
from .permission_manager import ConfirmationCallback, PermissionManager
from .skills_guard import SkillsSecurityGuard

_READ_FILE_TOOLS: frozenset[str] = frozenset({"read_file", "file_read", "file_info", "view_file"})
_SEARCH_FILE_TOOLS: frozenset[str] = frozenset({"search_files", "file_search", "directory_list", "list_dir"})
_WRITE_FILE_TOOLS: frozenset[str] = frozenset(
    {"write_file", "patch", "file_write", "file_edit", "file_move", "directory_create"}
)
_DELETE_FILE_TOOLS: frozenset[str] = frozenset({"delete_file", "file_delete", "remove_file", "directory_delete"})
_TERMINAL_TOOLS: frozenset[str] = frozenset({"terminal", "terminal_execute", "background_execute", "process_start"})
_PROCESS_TOOLS: frozenset[str] = frozenset({"process", "process_manage", "process_status", "process_terminate"})
_NETWORK_TOOLS: frozenset[str] = frozenset(
    {"web_search", "web_extract", "browser_navigate", "browser_action", "browser_click", "http_request"}
)
_DELEGATE_TOOLS: frozenset[str] = frozenset({"delegate_task", "spawn_subagent"})

_CODE_DELETE_RE = re.compile(
    r"\b(?:os\.(?:remove|unlink|rmdir|removedirs)|shutil\.rmtree|Path\([^)]*\)\.unlink)\b"
)
_CODE_SUBPROCESS_RE = re.compile(
    r"\b(?:subprocess\.(?:run|Popen|call|check_output|check_call)|os\.(?:system|popen|exec\w*|spawn\w*)|ctypes)\b"
)
_CODE_STRING_PATH_RE = re.compile(
    r"""['"]((?:\.\.[\\/]|~[\\/]|[A-Za-z]:[\\/]|\\\\)[^'"]+|\.env(?:\.[A-Za-z0-9_.-]+)?|[^'"]*(?:\.ssh|\.aws|\.gemini|auth\.json|id_rsa|credentials\.json)[^'"]*)['"]""",
    re.IGNORECASE,
)


def _extract_primary_path(args: Mapping[str, Any]) -> str:
    for key in ("path", "file_path", "target_path", "directory", "dir", "source", "destination"):
        val = args.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return ""


class PolicyEngine:
    """Evaluates Hermes tool calls against APA Security Guard policies."""

    def __init__(
        self,
        config: Optional[APASecurityConfig] = None,
        *,
        confirmation_callback: Optional[ConfirmationCallback] = None,
    ) -> None:
        self.config = config or APASecurityConfig()
        self.path_validator = PathValidator(self.config)
        self.filesystem_guard = FilesystemGuard(self.config, self.path_validator)
        self.command_validator = CommandValidator(self.config, self.filesystem_guard)
        self.permission_manager = PermissionManager(self.config, confirmation_callback=confirmation_callback)
        self.skills_guard = SkillsSecurityGuard(self.config, self.filesystem_guard, self.path_validator)

    def set_confirmation_callback(self, callback: Optional[ConfirmationCallback]) -> None:
        """Bind or clear the interactive confirmation callback."""
        self.permission_manager.confirmation_callback = callback

    def _evaluate_read_or_search(
        self, tool_name: str, args: Mapping[str, Any], *, default_to_workspace: bool = False
    ) -> SecurityEvaluation:
        raw_path = _extract_primary_path(args)
        if not raw_path and default_to_workspace:
            raw_path = str(self.config.workspace_root)
        fs_eval = self.filesystem_guard.evaluate_path_operation(
            raw_path, OperationType.READ, tool_name=tool_name
        )
        return self.permission_manager.evaluate_permission(fs_eval)

    def _evaluate_write_or_patch(self, tool_name: str, args: Mapping[str, Any]) -> SecurityEvaluation:
        # Check both source and destination if present (e.g. file_move)
        paths_to_check: list[str] = []
        for key in ("path", "file_path", "target_path", "source", "destination"):
            val = args.get(key)
            if isinstance(val, str) and val.strip() and val.strip() not in paths_to_check:
                paths_to_check.append(val.strip())
        if not paths_to_check:
            paths_to_check.append("")

        last_eval: Optional[SecurityEvaluation] = None
        for raw_path in paths_to_check:
            fs_eval = self.filesystem_guard.evaluate_path_operation(
                raw_path, OperationType.WRITE, tool_name=tool_name
            )
            if not fs_eval.allowed:
                return fs_eval
            last_eval = fs_eval

        assert last_eval is not None
        return self.permission_manager.evaluate_permission(last_eval)

    def _evaluate_delete(self, tool_name: str, args: Mapping[str, Any]) -> SecurityEvaluation:
        raw_path = _extract_primary_path(args)
        fs_eval = self.filesystem_guard.evaluate_path_operation(
            raw_path, OperationType.DELETE, tool_name=tool_name
        )
        if not fs_eval.allowed:
            return fs_eval
        return self.permission_manager.evaluate_permission(fs_eval, requires_confirmation_hint=True)

    def _evaluate_terminal(self, tool_name: str, args: Mapping[str, Any]) -> SecurityEvaluation:
        command = args.get("command") or args.get("cmd") or ""
        if not isinstance(command, str):
            command = str(command)
        cwd = args.get("workdir") or args.get("cwd")
        cwd_str = str(cwd).strip() if isinstance(cwd, str) and cwd.strip() else None

        cmd_res = self.command_validator.validate_command(command, cwd=cwd_str)
        base_eval = SecurityEvaluation(
            decision=PermissionDecision.ALLOW if cmd_res.allowed else PermissionDecision.DENY,
            risk_level=cmd_res.risk_level,
            operation=cmd_res.operation,
            reason=cmd_res.reason,
            tool_name=tool_name,
            target=command[:200],
            rule_id=cmd_res.rule_id,
            metadata={"extracted_paths": list(cmd_res.extracted_paths)},
        )
        if not cmd_res.allowed:
            return base_eval
        return self.permission_manager.evaluate_permission(
            base_eval, requires_confirmation_hint=cmd_res.requires_confirmation
        )

    def _evaluate_process(self, tool_name: str, args: Mapping[str, Any]) -> SecurityEvaluation:
        action = str(args.get("action") or "poll").strip().lower()
        data = args.get("data") or args.get("input") or ""
        if action in ("submit", "write", "send_keys") and isinstance(data, str) and data.strip():
            cmd_res = self.command_validator.validate_command(data)
            base_eval = SecurityEvaluation(
                decision=PermissionDecision.ALLOW if cmd_res.allowed else PermissionDecision.DENY,
                risk_level=cmd_res.risk_level,
                operation=cmd_res.operation,
                reason=cmd_res.reason,
                tool_name=tool_name,
                target=f"process:{action}:{data[:120]}",
                rule_id=cmd_res.rule_id,
            )
            if not cmd_res.allowed:
                return base_eval
            return self.permission_manager.evaluate_permission(
                base_eval, requires_confirmation_hint=cmd_res.requires_confirmation
            )

        base_eval = SecurityEvaluation(
            decision=PermissionDecision.ALLOW,
            risk_level=RiskLevel.MEDIUM if action in ("kill", "terminate") else RiskLevel.LOW,
            operation=OperationType.EXECUTE,
            reason=f"Process management action '{action}' validated.",
            tool_name=tool_name,
            target=f"process:{action}:{args.get('session_id', '')}",
            rule_id="PROCESS_OK",
        )
        return self.permission_manager.evaluate_permission(base_eval)

    def _evaluate_execute_code(self, tool_name: str, args: Mapping[str, Any]) -> SecurityEvaluation:
        code = args.get("code") or ""
        if not isinstance(code, str) or not code.strip():
            return SecurityEvaluation(
                decision=PermissionDecision.DENY,
                risk_level=RiskLevel.HIGH,
                operation=OperationType.EXECUTE,
                reason="execute_code requires a non-empty 'code' string.",
                tool_name=tool_name,
                target="",
                rule_id="CODE_EMPTY",
            )

        # Inspect embedded paths in code string
        is_delete = bool(_CODE_DELETE_RE.search(code))
        op = OperationType.DELETE if is_delete else OperationType.EXECUTE
        for match in _CODE_STRING_PATH_RE.finditer(code):
            cand_path = match.group(1)
            fs_eval = self.filesystem_guard.evaluate_path_operation(cand_path, op, tool_name=tool_name)
            if not fs_eval.allowed:
                return SecurityEvaluation(
                    decision=PermissionDecision.DENY,
                    risk_level=fs_eval.risk_level,
                    operation=op,
                    reason=f"execute_code references forbidden path '{cand_path}': {fs_eval.reason}",
                    tool_name=tool_name,
                    target=cand_path,
                    rule_id=fs_eval.rule_id,
                )

        if _CODE_SUBPROCESS_RE.search(code):
            return SecurityEvaluation(
                decision=PermissionDecision.DENY,
                risk_level=RiskLevel.CRITICAL,
                operation=OperationType.EXECUTE,
                reason="Direct subprocess/os.system/ctypes execution inside execute_code is blocked; use the terminal tool.",
                tool_name=tool_name,
                target="execute_code:subprocess",
                rule_id="CODE_SUBPROCESS_BLOCKED",
            )

        base_eval = SecurityEvaluation(
            decision=PermissionDecision.ALLOW,
            risk_level=RiskLevel.HIGH if is_delete else RiskLevel.MEDIUM,
            operation=op,
            reason="Programmatic tool execution script validated.",
            tool_name=tool_name,
            target=f"execute_code ({len(code)} chars)",
            rule_id="CODE_DELETE" if is_delete else "CODE_OK",
        )
        return self.permission_manager.evaluate_permission(
            base_eval, requires_confirmation_hint=is_delete or self.config.require_confirmation_for_execute
        )

    def _evaluate_bridge_tool_call(self, args: Mapping[str, Any]) -> SecurityEvaluation:
        underlying_name = str(args.get("name") or args.get("tool_name") or "").strip()
        raw_underlying_args = args.get("arguments") if "arguments" in args else args.get("args")
        if isinstance(raw_underlying_args, str):
            try:
                underlying_args = json.loads(raw_underlying_args)
            except ValueError:
                underlying_args = {}
        elif isinstance(raw_underlying_args, Mapping):
            underlying_args = dict(raw_underlying_args)
        else:
            underlying_args = {}

        if not underlying_name:
            return SecurityEvaluation(
                decision=PermissionDecision.DENY,
                risk_level=RiskLevel.HIGH,
                operation=OperationType.OTHER,
                reason="Bridge tool_call is missing target tool 'name'.",
                tool_name="tool_call",
                target="",
                rule_id="BRIDGE_MISSING_NAME",
            )
        return self.evaluate_tool_call(underlying_name, underlying_args)

    def evaluate_tool_call(
        self,
        tool_name: str,
        args: Optional[Mapping[str, Any]] = None,
    ) -> SecurityEvaluation:
        """Evaluate any Hermes tool call and return a :class:`SecurityEvaluation`."""
        clean_name = str(tool_name or "").strip()
        safe_args: Mapping[str, Any] = args if isinstance(args, Mapping) else {}

        if not self.config.enabled:
            return SecurityEvaluation(
                decision=PermissionDecision.ALLOW,
                risk_level=RiskLevel.LOW,
                operation=OperationType.OTHER,
                reason="APA Security Guard is disabled via configuration.",
                tool_name=clean_name,
                rule_id="GUARD_DISABLED",
            )

        if self.config.fail_closed_reason:
            return SecurityEvaluation(
                decision=PermissionDecision.DENY,
                risk_level=RiskLevel.CRITICAL,
                operation=OperationType.OTHER,
                reason=self.config.fail_closed_reason,
                tool_name=clean_name,
                rule_id="CONFIG_FAIL_CLOSED",
            )

        if not clean_name:
            return SecurityEvaluation(
                decision=PermissionDecision.DENY,
                risk_level=RiskLevel.HIGH,
                operation=OperationType.OTHER,
                reason="Tool name must be non-empty.",
                tool_name="",
                rule_id="TOOL_NAME_EMPTY",
            )

        # Unwrap deferred tool_call bridge immediately
        if clean_name == "tool_call":
            return self._evaluate_bridge_tool_call(safe_args)

        if clean_name in _READ_FILE_TOOLS:
            return self._evaluate_read_or_search(clean_name, safe_args, default_to_workspace=False)
        if clean_name in _SEARCH_FILE_TOOLS:
            return self._evaluate_read_or_search(clean_name, safe_args, default_to_workspace=True)
        if clean_name in _WRITE_FILE_TOOLS:
            return self._evaluate_write_or_patch(clean_name, safe_args)
        if clean_name in _DELETE_FILE_TOOLS:
            return self._evaluate_delete(clean_name, safe_args)
        if clean_name in _TERMINAL_TOOLS:
            return self._evaluate_terminal(clean_name, safe_args)
        if clean_name in _PROCESS_TOOLS:
            return self._evaluate_process(clean_name, safe_args)
        if clean_name == "execute_code":
            return self._evaluate_execute_code(clean_name, safe_args)
        if clean_name in _DELEGATE_TOOLS:
            base = SecurityEvaluation(
                decision=PermissionDecision.ALLOW,
                risk_level=RiskLevel.MEDIUM,
                operation=OperationType.DELEGATE,
                reason="Subagent delegation request validated (child tool calls remain guarded).",
                tool_name=clean_name,
                target=str(safe_args.get("goal") or safe_args.get("prompt") or "subagent")[:120],
                rule_id="DELEGATE_OK",
            )
            return self.permission_manager.evaluate_permission(base)
        if clean_name in _NETWORK_TOOLS:
            target_url = str(safe_args.get("url") or safe_args.get("query") or clean_name)[:200]
            base = SecurityEvaluation(
                decision=PermissionDecision.ALLOW,
                risk_level=RiskLevel.LOW,
                operation=OperationType.NETWORK,
                reason="Network tool validated.",
                tool_name=clean_name,
                target=target_url,
                rule_id="NETWORK_OK",
            )
            return self.permission_manager.evaluate_permission(base)

        if clean_name in ("skill_view", "view_skill"):
            base_eval = self.skills_guard.evaluate_skill_view(clean_name, safe_args)
            return self.permission_manager.evaluate_permission(base_eval)
        if clean_name in ("skill_manage", "manage_skill"):
            base_eval = self.skills_guard.evaluate_skill_manage(clean_name, safe_args)
            return self.permission_manager.evaluate_permission(
                base_eval, requires_confirmation_hint=base_eval.requires_confirmation
            )

        # Generic fallback for other tools (todo, memory, session_search, tool_search, etc.):
        # Still inspect any explicit path or command argument if present!
        if "command" in safe_args and isinstance(safe_args.get("command"), str):
            return self._evaluate_terminal(clean_name, safe_args)
        if _extract_primary_path(safe_args):
            return self._evaluate_read_or_search(clean_name, safe_args, default_to_workspace=False)

        base = SecurityEvaluation(
            decision=PermissionDecision.ALLOW,
            risk_level=RiskLevel.LOW,
            operation=OperationType.OTHER,
            reason="Standard non-filesystem tool allowed.",
            tool_name=clean_name,
            target=clean_name,
            rule_id="TOOL_DEFAULT_OK",
        )
        return self.permission_manager.evaluate_permission(base)
