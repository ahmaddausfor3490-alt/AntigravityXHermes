"""Shell and PowerShell command validator for APA Security Guard."""

from __future__ import annotations

import os
import re
import shlex

from .config import APASecurityConfig
from .filesystem_guard import FilesystemGuard
from .models import CommandValidationResult, OperationType, PolicyMode, RiskLevel

# Hard-blocked destructive / catastrophic patterns (CRITICAL -> immediate DENY)
_DESTRUCTIVE_PATTERNS: tuple[tuple[str, str, re.Pattern[str]], ...] = (
    (
        "CMD_DISK_FORMAT",
        "Disk formatting or partition manipulation command is forbidden.",
        re.compile(
            r"(?:\bformat(?:\.com|\.exe)?\s+[a-zA-Z]:|\bdiskpart\b|\bmkfs(?:\.\w+)?\b|\bfdisk\b|\bcipher(?:\.exe)?\s+/w\b)",
            re.IGNORECASE,
        ),
    ),
    (
        "CMD_RAW_DISK_WRITE",
        "Raw disk device write (dd / PhysicalDrive) is forbidden.",
        re.compile(r"(?:\bdd\s+if=|\\\\\.\\PhysicalDrive|/dev/(?:sd[a-z]|nvme\d+n\d+|disk\d+))", re.IGNORECASE),
    ),
    (
        "CMD_RECURSIVE_FORCE_DELETE",
        "Recursive forced deletion (Remove-Item -Recurse -Force / rm -rf / rd /s /q) is forbidden.",
        re.compile(
            r"(?:\brm\s+(?:-[a-zA-Z]*r[a-zA-Z]*f|-[a-zA-Z]*f[a-zA-Z]*r|--recursive\s+--force)\b"
            r"|\b(?:Remove-Item|ri|del|erase|rd|rmdir)\b[^\n;|]*(?:-Recurse\b[^\n;|]*-Force\b|-Force\b[^\n;|]*-Recurse\b|/s\s+/q|/q\s+/s|/f\s+/s\s+/q))",
            re.IGNORECASE,
        ),
    ),
    (
        "CMD_REGISTRY_OR_BOOT_TAMPER",
        "Registry deletion, shadow copy deletion, or boot configuration tampering is forbidden.",
        re.compile(
            r"\b(?:reg(?:\.exe)?\s+(?:delete|add\s+HKLM)|bcdedit|vssadmin\s+delete\s+shadows|wbadmin\s+delete|Clear-EventLog|wevtutil\s+cl)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "CMD_SYSTEM_POWER",
        "System shutdown, restart, or halt command is forbidden.",
        re.compile(r"\b(?:Stop-Computer|Restart-Computer|shutdown(?:\.exe)?\s+/[sr]|init\s+[06]|reboot|poweroff|halt)\b", re.IGNORECASE),
    ),
    (
        "CMD_FORK_BOMB",
        "Fork bomb pattern detected.",
        re.compile(r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:"),
    ),
    (
        "CMD_OBFUSCATED_POWERSHELL",
        "Obfuscated or encoded PowerShell execution (-EncodedCommand / Invoke-Expression / DownloadString) is forbidden.",
        re.compile(
            r"(?:\b(?:powershell|pwsh)(?:\.exe)?\b[^\n]*\s-(?:e|ec|enc|encodedcommand)\s+[A-Za-z0-9+/=]{8,}"
            r"|\b(?:Invoke-Expression|iex)\b"
            r"|\b(?:DownloadString|DownloadFile|FromBase64String)\b"
            r"|\b(?:powershell|pwsh)(?:\.exe)?\b[^\n]*\s-(?:w|windowstyle)\s+hidden\b"
            r"|\b(?:powershell|pwsh)(?:\.exe)?\b[^\n]*\s-(?:ep|executionpolicy)\s+bypass\b)",
            re.IGNORECASE,
        ),
    ),
    (
        "CMD_PRIVILEGE_ESCALATION",
        "Privilege escalation or elevation command (runas / sudo / -Verb RunAs) is forbidden.",
        re.compile(
            r"(?:\b(?:runas|sudo|su)\b|\bStart-Process\b[^\n]*-Verb\s+RunAs\b|\bSet-ExecutionPolicy\b)",
            re.IGNORECASE,
        ),
    ),
    (
        "CMD_SUSPICIOUS_LOLBIN",
        "Suspicious system binary / LOLBin execution (mshta, regsvr32, rundll32, certutil, bitsadmin, wmic) is forbidden.",
        re.compile(
            r"\b(?:mshta|regsvr32|rundll32|wscript|cscript)(?:\.exe)?\b"
            r"|\bcertutil(?:\.exe)?\s+-(?:urlcache|decode|encode)\b"
            r"|\bbitsadmin(?:\.exe)?\s+/transfer\b"
            r"|\bwmic(?:\.exe)?\s+process\s+call\s+create\b",
            re.IGNORECASE,
        ),
    ),
)

# High-risk system modification patterns (HIGH -> DENY in strict/standard or REQUIRE_CONFIRMATION)
_HIGH_RISK_SYSTEM_PATTERNS: tuple[tuple[str, str, re.Pattern[str]], ...] = (
    (
        "CMD_USER_OR_SERVICE_ADMIN",
        "System account, service, scheduled task, or ACL modification detected.",
        re.compile(
            r"\b(?:net(?:\.exe)?\s+(?:user|localgroup|share)|sc(?:\.exe)?\s+(?:create|config|delete|stop)"
            r"|schtasks(?:\.exe)?\s+/(?:create|delete|change)|takeown|icacls|chmod\s+[0-7]*777|chown)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "CMD_FIREWALL_OR_DEFENDER",
        "Firewall or Windows Defender modification command is forbidden.",
        re.compile(
            r"\b(?:Set-MpPreference|Add-MpPreference|netsh\s+advfirewall|ufw\s+disable|iptables\s+-F)\b",
            re.IGNORECASE,
        ),
    ),
)

_DELETE_CMD_RE = re.compile(
    r"^\s*(?:rm|del|erase|Remove-Item|ri|rmdir|rd|unlink)\b",
    re.IGNORECASE,
)
_WRITE_CMD_RE = re.compile(
    r"(?:^\s*(?:Set-Content|Add-Content|Out-File|New-Item|ni|cp|copy|Copy-Item|mv|move|Move-Item|ren|Rename-Item|touch|mkdir|md)\b"
    r"|(?:^|[^>&])>{1,2}(?![>&]))",
    re.IGNORECASE,
)
_NETWORK_CMD_RE = re.compile(
    r"\b(?:curl|wget|Invoke-WebRequest|iwr|Invoke-RestMethod|irm|ssh|scp|sftp|ftp|nc|ncat|netcat|telnet|nmap)\b",
    re.IGNORECASE,
)

# Command chaining / subshell tokens outside quotes
_CHAIN_SPLIT_RE = re.compile(r"\s*(?:&&|\|\||;|\r?\n)\s*")
_SUBSHELL_RE = re.compile(r"(?:\$\([^)]+\)|`[^`]+`)")

# Shell flags to skip when extracting path tokens
_NON_PATH_TOKEN_RE = re.compile(r"^(?:-[a-zA-Z0-9_-]+|/[a-zA-Z0-9?]+|[a-zA-Z0-9_]+=.*)$")
_COMMAND_VERBS_LOWER: frozenset[str] = frozenset(
    {
        "echo", "dir", "ls", "pwd", "Get-Location", "gl", "cd", "Set-Location",
        "cat", "type", "Get-Content", "gc", "Get-ChildItem", "gci", "Write-Output",
        "Write-Host", "Select-Object", "Where-Object", "ForEach-Object", "Sort-Object",
        "Measure-Object", "python", "python3", "pytest", "git", "node", "npm", "npx",
        "pip", "uv", "ruff", "mypy", "rg", "grep", "find", "which", "where", "whoami",
        "hostname", "ver", "uname", "env", "set", "Get-Item", "gi", "Test-Path",
        "Remove-Item", "ri", "rm", "del", "erase", "rd", "rmdir", "Set-Content",
        "Add-Content", "Out-File", "New-Item", "Copy-Item", "Move-Item", "cp", "mv",
        "copy", "move", "mkdir", "md", "cmd", "cmd.exe", "powershell", "powershell.exe",
        "pwsh", "pwsh.exe", "bash", "sh",
    }
)


def _split_chained_segments(command: str) -> list[str]:
    """Split a shell command on unquoted ``;``, ``&&``, ``||``, and newlines."""
    segments: list[str] = []
    current: list[str] = []
    in_single = False
    in_double = False
    i = 0
    n = len(command)
    while i < n:
        ch = command[i]
        if ch == "'" and not in_double:
            in_single = not in_single
            current.append(ch)
            i += 1
            continue
        if ch == '"' and not in_single:
            in_double = not in_double
            current.append(ch)
            i += 1
            continue
        if not in_single and not in_double:
            if ch in (";", "\n"):
                seg = "".join(current).strip()
                if seg:
                    segments.append(seg)
                current = []
                i += 1
                continue
            if i + 1 < n and command[i : i + 2] in ("&&", "||"):
                seg = "".join(current).strip()
                if seg:
                    segments.append(seg)
                current = []
                i += 2
                continue
        current.append(ch)
        i += 1
    tail = "".join(current).strip()
    if tail:
        segments.append(tail)
    return segments


def _looks_like_path_token(tok: str) -> bool:
    """Return True if a command argument token looks like a file or directory path."""
    if not tok or _NON_PATH_TOKEN_RE.match(tok):
        return False
    if tok.lower() in {v.lower() for v in _COMMAND_VERBS_LOWER}:
        return False
    if tok.startswith(("http://", "https://", "ws://", "wss://")):
        return False
    if ".." in tok or tok.startswith(("~", "/", "./", ".\\", "\\\\")):
        return True
    if re.match(r"^[A-Za-z]:[\\/]", tok):
        return True
    if "/" in tok or "\\" in tok:
        return True
    if tok.startswith(".env") or tok.lower() in {
        "auth.json", "id_rsa", "id_ed25519", "credentials.json", "secrets.yaml", "secrets.json", "oauth_creds.json",
    }:
        return True
    if os.path.splitext(tok)[1].lower() in {
        ".pem", ".key", ".pfx", ".p12", ".kdbx", ".env",
    }:
        return True
    return False


def _extract_candidate_paths(command: str) -> tuple[str, ...]:
    """Extract candidate file/directory paths referenced in *command*."""
    paths: list[str] = []
    for seg in _split_chained_segments(command):
        # Also split on pipe |
        for piped in seg.split("|"):
            piped_clean = piped.strip()
            if not piped_clean:
                continue
            try:
                tokens = shlex.split(piped_clean, posix=os.name == "posix")
            except ValueError:
                tokens = piped_clean.split()
            for idx, raw_tok in enumerate(tokens):
                tok = raw_tok.strip()
                if len(tok) >= 2 and tok[0] == tok[-1] and tok[0] in ("'", '"'):
                    tok = tok[1:-1]
                # Skip the leading command verb unless it is an explicit path to an executable
                if idx == 0 and not (
                    tok.startswith(("./", ".\\", "/", "~", ".."))
                    or re.match(r"^[A-Za-z]:[\\/]", tok)
                ):
                    continue
                # Strip redirection prefix like >file.txt or >>file.txt
                if tok.startswith((">>", ">", "<")):
                    tok = tok.lstrip("><").strip()
                if _looks_like_path_token(tok) and tok not in paths:
                    paths.append(tok)
    return tuple(paths)


class CommandValidator:
    """Inspects terminal/PowerShell commands for destructive patterns, chaining, and path escapes."""

    def __init__(self, config: APASecurityConfig, filesystem_guard: FilesystemGuard | None = None) -> None:
        self.config = config
        self.filesystem_guard = filesystem_guard or FilesystemGuard(config)

    def _check_custom_blocked_commands(self, command: str) -> tuple[bool, str]:
        cmd_lower = command.lower()
        for blocked in self.config.blocked_commands:
            if blocked.lower() in cmd_lower:
                return True, f"Command matches blocked_commands policy rule '{blocked}'."
        return False, ""

    def validate_command(self, command: str, *, cwd: str | None = None) -> CommandValidationResult:
        """Analyze *command* (and optional *cwd*) and return a :class:`CommandValidationResult`."""
        if not isinstance(command, str) or not command.strip():
            return CommandValidationResult(
                allowed=False,
                risk_level=RiskLevel.HIGH,
                operation=OperationType.EXECUTE,
                reason="Command must be a non-empty string.",
                rule_id="CMD_EMPTY",
            )

        if "\x00" in command:
            return CommandValidationResult(
                allowed=False,
                risk_level=RiskLevel.CRITICAL,
                operation=OperationType.EXECUTE,
                reason="Null byte detected in command string.",
                rule_id="CMD_NULL_BYTE",
            )

        # 0. Validate explicit working directory if provided
        if cwd:
            cwd_eval = self.filesystem_guard.evaluate_path_operation(cwd, OperationType.READ, tool_name="terminal")
            if not cwd_eval.allowed:
                return CommandValidationResult(
                    allowed=False,
                    risk_level=cwd_eval.risk_level,
                    operation=OperationType.EXECUTE,
                    reason=f"Invalid command working directory: {cwd_eval.reason}",
                    rule_id=cwd_eval.rule_id,
                )

        # 1. Check custom blocked_commands list
        is_custom_blocked, custom_reason = self._check_custom_blocked_commands(command)
        if is_custom_blocked:
            return CommandValidationResult(
                allowed=False,
                risk_level=RiskLevel.CRITICAL,
                operation=OperationType.EXECUTE,
                reason=custom_reason,
                rule_id="CMD_BLOCKED_POLICY",
            )

        # 2. Check hard-blocked destructive / obfuscated / privilege-escalation / LOLBin patterns
        for rule_id, reason, pattern in _DESTRUCTIVE_PATTERNS:
            if pattern.search(command):
                return CommandValidationResult(
                    allowed=False,
                    risk_level=RiskLevel.CRITICAL,
                    operation=OperationType.EXECUTE,
                    reason=reason,
                    rule_id=rule_id,
                )

        # 3. Check high-risk system / firewall / service modification patterns
        for rule_id, reason, pattern in _HIGH_RISK_SYSTEM_PATTERNS:
            if pattern.search(command):
                return CommandValidationResult(
                    allowed=False,
                    risk_level=RiskLevel.CRITICAL,
                    operation=OperationType.EXECUTE,
                    reason=reason,
                    rule_id=rule_id,
                )

        # 4. Inspect command chaining & subshell injection
        segments = _split_chained_segments(command)
        has_chaining = len(segments) > 1
        has_subshell = bool(_SUBSHELL_RE.search(command))

        if has_subshell:
            return CommandValidationResult(
                allowed=False,
                risk_level=RiskLevel.HIGH,
                operation=OperationType.EXECUTE,
                reason="Command substitution / subshell injection ($(...) or backticks) is blocked by APA Security Guard.",
                rule_id="CMD_SUBSHELL_INJECTION",
            )

        # Classify operation across all segments
        is_delete = any(_DELETE_CMD_RE.search(seg) for seg in segments)
        is_write = any(_WRITE_CMD_RE.search(seg) for seg in segments)
        is_network = any(_NETWORK_CMD_RE.search(seg) for seg in segments)

        if has_chaining and (is_delete or self.config.policy_mode == PolicyMode.STRICT):
            return CommandValidationResult(
                allowed=False,
                risk_level=RiskLevel.HIGH,
                operation=OperationType.DELETE if is_delete else OperationType.EXECUTE,
                reason="Chained shell command containing destructive/deletion operations or disallowed under strict policy.",
                rule_id="CMD_CHAINING_BLOCKED",
            )

        # 5. Extract candidate paths referenced in the command and validate via FilesystemGuard
        extracted_paths = _extract_candidate_paths(command)
        path_op = OperationType.DELETE if is_delete else (OperationType.WRITE if is_write else OperationType.READ)
        for cand_path in extracted_paths:
            fs_eval = self.filesystem_guard.evaluate_path_operation(cand_path, path_op, tool_name="terminal")
            if not fs_eval.allowed:
                return CommandValidationResult(
                    allowed=False,
                    risk_level=fs_eval.risk_level,
                    operation=path_op,
                    reason=f"Shell command references forbidden path '{cand_path}': {fs_eval.reason}",
                    rule_id=fs_eval.rule_id,
                    extracted_paths=extracted_paths,
                )

        # 6. Determine operation & risk level for allowed / confirmation-gated commands
        if is_delete:
            return CommandValidationResult(
                allowed=True,
                risk_level=RiskLevel.HIGH,
                operation=OperationType.DELETE,
                reason="Shell deletion command within workspace.",
                rule_id="CMD_DELETE",
                extracted_paths=extracted_paths,
                requires_confirmation=True,
            )

        if is_network:
            if not self.config.allow_network:
                return CommandValidationResult(
                    allowed=False,
                    risk_level=RiskLevel.HIGH,
                    operation=OperationType.NETWORK,
                    reason="Network command execution is disabled by security policy.",
                    rule_id="CMD_NETWORK_DISABLED",
                    extracted_paths=extracted_paths,
                )
            return CommandValidationResult(
                allowed=True,
                risk_level=RiskLevel.HIGH if has_chaining else RiskLevel.MEDIUM,
                operation=OperationType.NETWORK,
                reason="Network command validated.",
                rule_id="CMD_NETWORK",
                extracted_paths=extracted_paths,
                requires_confirmation=has_chaining or self.config.policy_mode == PolicyMode.STRICT,
            )

        if has_chaining:
            return CommandValidationResult(
                allowed=True,
                risk_level=RiskLevel.MEDIUM,
                operation=OperationType.EXECUTE,
                reason="Chained command validated within workspace.",
                rule_id="CMD_CHAINED_SAFE",
                extracted_paths=extracted_paths,
                requires_confirmation=self.config.require_confirmation_for_execute,
            )

        return CommandValidationResult(
            allowed=True,
            risk_level=RiskLevel.LOW if not is_write else RiskLevel.MEDIUM,
            operation=OperationType.EXECUTE,
            reason="Command validated.",
            rule_id="CMD_OK",
            extracted_paths=extracted_paths,
            requires_confirmation=self.config.require_confirmation_for_execute,
        )
