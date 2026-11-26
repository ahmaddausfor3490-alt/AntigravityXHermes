# APA Phase 3.6 — Security Test Report

## 1. Executive Summary

Automated security and regression testing was executed against `plugins/apa-security/` and the full Antigravity + Hermes integration stack using Python `3.14.9` and `pytest 9.2.0` on Windows 10/11 x64.

- **Security Test Suite (`tests/plugins/test_apa_security.py`)**: **24 / 24 PASSED (0 failed, 0 skipped)**
- **Antigravity Regression Suite (`tests/agent/test_antigravity_provider.py`, `tests/providers/test_plugin_discovery.py`, `tests/agent/test_acp_client.py`, `tests/agent/test_external_process_streaming.py`)**: **29 / 29 PASSED**
- **Combined Total**: **53 / 53 PASSED**

---

## 2. Detailed Security Test Matrix (`tests/plugins/test_apa_security.py`)

### Test Group A — Permission & Configuration Validation (`TestGroupAPermissions`)

| Test Case | Scenario Verified | Expected Outcome | Result |
|---|---|---|---|
| `test_read_permission_allow_and_deny` | `read_file` with `allow_read=True` vs `allow_read=False` | Allowed when enabled; blocked with `PERM_READ_DISABLED` when disabled | **PASS** |
| `test_write_permission_allow_and_deny` | `write_file` with `allow_write=True` vs `allow_write=False` | Allowed when enabled; blocked with `PERM_WRITE_DISABLED` when disabled | **PASS** |
| `test_delete_permission_denied_by_default_and_confirmation_when_enabled` | `delete_file` under default policy (`allow_delete=False`) vs enabled (`allow_delete=True`) | Denied by default (`PERM_DELETE_DISABLED`); returns `REQUIRE_CONFIRMATION` (`PERM_CONFIRM_DELETE`) when enabled | **PASS** |
| `test_execute_permission_allow_and_deny` | `terminal` with `allow_execute=True` vs `allow_execute=False` | Allowed when enabled; blocked with `PERM_EXECUTE_DISABLED` when disabled | **PASS** |
| `test_confirmation_never_auto_approved_and_callback_behavior` | `REQUIRE_CONFIRMATION` without callback, with `True` callback, with `False` callback, and with raising callback | Never auto-approved; `True` -> `ALLOW (user_approved=True)`; `False` or exception -> fail-closed `DENY` | **PASS** |
| `test_malformed_configuration_fails_closed` | Invalid `policy_mode` and non-boolean `allow_read` in `APASecurityConfig.from_mapping` | `strict=True` raises `ValueError`; `strict=False` locks into `CONFIG_FAIL_CLOSED` and denies all calls | **PASS** |

---

### Test Group B — Filesystem Security (`TestGroupBFilesystem`)

| Test Case | Scenario Verified | Expected Outcome | Result |
|---|---|---|---|
| `test_allowed_workspace_access` | `read_file` and `write_file` on `README.md`, `src/app.py`, `.env.example`, `src/new_module.py` inside workspace | `ALLOW` (`FS_OK`) | **PASS** |
| `test_access_outside_workspace_blocked` | `read_file` targeting sibling `outside/secret.txt` | `DENY` (`PATH_OUTSIDE_WORKSPACE`) | **PASS** |
| `test_path_traversal_and_special_namespaces_blocked` | `..`, `../outside/secret.txt`, `src/../../outside/secret.txt`, null byte `README.md\x00.txt`, NT device `\\.\PhysicalDrive0`, `\??\C:\Windows\win.ini`, `\\?\UNC\...`, UNC `\\192.168.1.99\share\...` | `DENY` (`PATH_TRAVERSAL_OUTSIDE_WORKSPACE`, `PATH_NULL_BYTE`, `PATH_NT_NAMESPACE`, `PATH_UNC_SHARE`) | **PASS** |
| `test_sensitive_files_and_directories_blocked` | `.env`, `.env.local`, `.env.production`, `auth.json`, `oauth_creds.json`, `id_rsa`, `server.pem`, `tls.key`, `cert.pfx`, `.ssh/authorized_keys`, `.aws/credentials`, `.gemini/settings.json` | `DENY` (`CRITICAL` risk) | **PASS** |
| `test_symlink_escape_blocked` | Symlink inside workspace resolving to `outside/secret.txt` | `DENY` (`PATH_SYMLINK_ESCAPE`) | **PASS** |
| `test_windows_junction_escape_blocked` | Real Windows NTFS directory junction (`cmd.exe /c mklink /J`) inside workspace pointing to `outside/` for both existing file read and new file write | `DENY` (`PATH_SYMLINK_ESCAPE`) | **PASS** |
| `test_self_tampering_and_git_hook_protection` | `write_file` targeting `plugins/apa-security/policy_engine.py` and `.git/hooks/pre-commit` | `DENY` (`FS_SELF_TAMPER_PROTECTION`, `FS_GIT_TAMPER`) | **PASS** |

---

### Test Group C — Terminal & PowerShell Security (`TestGroupCTerminal`)

| Test Case | Scenario Verified | Expected Outcome | Result |
|---|---|---|---|
| `test_safe_commands_allowed` | `echo 'hello world'`, `git status --short`, `python -m pytest tests/unit`, `Get-ChildItem src` | `ALLOW` (`CMD_OK`) | **PASS** |
| `test_destructive_powershell_and_shell_commands_blocked` | `Remove-Item -Recurse -Force C:\Users`, `rm -rf /`, `rd /s /q C:\Windows`, `format C: /q`, `diskpart`, `reg delete HKLM\...`, `bcdedit`, `vssadmin delete shadows`, `Stop-Computer -Force`, fork bomb `:(){ :|:& };:` | `DENY` (`CMD_RECURSIVE_FORCE_DELETE`, `CMD_DISK_FORMAT`, `CMD_REGISTRY_OR_BOOT_TAMPER`, `CMD_SYSTEM_POWER`, `CMD_FORK_BOMB`) | **PASS** |
| `test_command_chaining_and_subshell_injection_blocked` | `echo hello; Remove-Item -Recurse -Force ..`, `git status && rm file.txt`, `echo $(cat /etc/passwd)`, ``Write-Output `whoami` `` | `DENY` (`CMD_RECURSIVE_FORCE_DELETE`, `CMD_CHAINING_BLOCKED`, `CMD_SUBSHELL_INJECTION`) | **PASS** |
| `test_suspicious_executables_and_obfuscated_powershell_blocked` | `powershell.exe -EncodedCommand ...`, `pwsh -w hidden -ep bypass ...`, `Invoke-Expression ... DownloadString(...)`, `Start-Process cmd.exe -Verb RunAs`, `sudo`, `mshta.exe`, `certutil -urlcache`, `regsvr32` | `DENY` (`CMD_OBFUSCATED_POWERSHELL`, `CMD_PRIVILEGE_ESCALATION`, `CMD_SUSPICIOUS_LOLBIN`) | **PASS** |
| `test_terminal_filesystem_boundary_escape_blocked` | `Get-Content ..\outside\secret.txt`, `cat .env`, `type ~/.ssh/id_rsa` | `DENY` (blocked via shell path extraction + `FilesystemGuard`) | **PASS** |

---

### Test Group D — Tool Execution Integration & Audit Logging (`TestGroupDToolIntegration`)

| Test Case | Scenario Verified | Expected Outcome | Result |
|---|---|---|---|
| `test_pre_and_post_tool_call_interception_and_audit` | `on_pre_tool_call` and `on_post_tool_call` for allowed (`README.md`) and denied (`.env`) calls, plus `validate_log_file()` | Allowed returns `None` and logs `status="ok"`; denied returns `{"action": "block", ...}` and logs `status="blocked"` without duplicate entries | **PASS** |
| `test_confirmation_directive_integration` | `REQUIRE_CONFIRMATION` under `unattended_confirmation_action="approve_hook"` vs `"deny"` | Returns `{"action": "approve", ...}` or `{"action": "block", ...}` respectively | **PASS** |
| `test_audit_logger_redacts_all_credential_types` | Audit logging with Google OAuth (`ya29.*`, `1//*`), GitHub (`ghp_*`), AWS (`AKIA*`), OpenAI (`sk-*`), `Bearer` JWT, URL password, PEM private key, secret dict keys, and raw file `content` | 100% of secrets and raw file bodies redacted from `apa_security_audit.jsonl` | **PASS** |

---

### Test Group E — End-to-End Bypass Resistance (`TestGroupEBypassResistance`)

| Test Case | Scenario Verified | Expected Outcome | Result |
|---|---|---|---|
| `test_real_plugin_discovery_and_handle_function_call_blocks_before_execution` | Full E2E through `PluginManager.discover_and_load()` and `model_tools.handle_function_call()` for `terminal`, deferred `tool_call` bridge, `execute_code` subprocess, and `process(action="submit")`, asserting `_execute_tool` is never called when blocked | Denied calls return `[APA Security Guard] BLOCKED` and `_execute_tool.assert_not_called()` passes; allowed `read_file` executes normally | **PASS** |
| `test_antigravity_acp_and_print_mode_tool_calls_cannot_bypass_security_guard` | Malicious `<tool_call>` emitted by Antigravity (`read_file` on `../outside/secret.txt`) parsed via `extract_tool_calls_from_text` and routed through `handle_function_call` | Intercepted and blocked by `apa-security` before `_execute_tool` runs | **PASS** |
| `test_subagent_delegation_policy_enforcement` | `delegate_task` with `allow_subagents=False` vs `allow_subagents=True` | Blocked with `PERM_SUBAGENT_DISABLED` when disabled; allowed when enabled | **PASS** |
