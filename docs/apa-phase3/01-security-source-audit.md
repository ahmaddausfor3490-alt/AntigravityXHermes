# APA Phase 3.1 — Legacy Security Source Audit

## 1. Executive Summary

An exhaustive technical audit was conducted on the legacy APA security subsystem located at `D:\Antigravity-Hermes\security\` and its adjacent configuration and runtime event scrubbing modules (`D:\Antigravity-Hermes\config\settings.py`, `D:\Antigravity-Hermes\config.yaml`, `D:\Antigravity-Hermes\runtime\events.py`, `D:\Antigravity-Hermes\runtime\coordinator.py`).

All legacy security source files were verified present and intact on disk. Rather than porting the legacy standalone execution engines (`SecureFileSystemEngine`, `ProcessExecutionEngine`)—which would duplicate or replace Hermes Agent's native tool handlers—the audit extracts the pure policy, boundary, path/symlink validation, command risk classification, permission evaluation, and credential-redacted JSONL audit logging into a modular, zero-core-edit Hermes plugin (`plugins/apa-security/`).

---

## 2. Legacy Security Inventory & Classification

| Legacy File (`D:\Antigravity-Hermes\...`) | Primary Class / Symbol | Responsibility in Legacy APA | Classification | Target in `plugins/apa-security/` |
|---|---|---|---|---|
| `security/__init__.py` | Package exports | Exports `SecurityManager` | **Adaptable** | `plugins/apa-security/__init__.py` (`register(ctx)` plugin entrypoint) |
| `security/manager.py` | `SecurityManager` | Central facade coordinating boundary, policy, permission, backup, and audit | **Adaptable** | `policy_engine.py` (`PolicyEngine`) + `tool_interceptor.py` (`ToolInterceptor`) |
| `security/policy.py` | `PolicyChecker`, `ActionSeverity` | Classifies actions into `LOW`, `MEDIUM`, `HIGH`, `CRITICAL`; checks `blocked_commands` and `blocked_paths` | **Adaptable** | `models.py` (`RiskLevel`, `OperationType`, `PermissionDecision`) + `policy_engine.py` |
| `security/permissions.py` | `PermissionManager` | Maps (`action_type`, `severity`) against `allow_read`, `allow_write`, `allow_delete`, `allow_execute`, `require_confirmation_for_write`, `require_confirmation_for_delete` | **Reusable / Adaptable** | `permission_manager.py` (`PermissionManager`) with fail-closed defaults and explicit confirmation support |
| `security/boundary.py` | `BoundaryValidator` | Rejects null bytes (`\x00`), Windows UNC paths (`\\server\share`), and verifies `Path.resolve()` stays inside `workspace_root` | **Adaptable** | `path_validator.py` (`PathValidator`) — enhanced with Windows NT-namespace device path guards (`\??\`, `\\.\`, `\\?\UNC\`, `\\?\GLOBALROOT`) and `os.path.realpath()` symlink/junction escape detection |
| `security/filesystem.py` | `SecureFileSystemEngine` | Enforces protected filenames (`.env*`, `id_rsa`, `credentials.json`), extensions (`.pem`, `.key`, `.pfx`, `.p12`, `.kdbx`), directories (`.git`, `.ssh`, `.aws`, `.gnupg`), and `os.path.realpath()` containment | **Adaptable** | `filesystem_guard.py` (`FilesystemGuard`) — extracted as a pre-execution validator for Hermes file tools (`read_file`, `write_file`, `patch`, `search_files`) instead of replacing Hermes's file handlers |
| `security/process.py` | `ProcessExecutionEngine`, `CommandRiskCategory` | Classifies shell commands into `SAFE_READ`, `SAFE_DEV`, `HIGH_RISK_SYSTEM`, `NETWORK_ACCESS`, `DESTRUCTIVE` using compiled regex patterns; enforces working directory boundary | **Adaptable** | `command_validator.py` (`CommandValidator`) — expanded with PowerShell-specific destructive cmdlets, command chaining/subshell injection detection, encoded command detection, and shell path extraction |
| `security/audit.py` | `AuditLogger`, `MASK_RULES` | Appends JSONL audit entries (`timestamp`, `action_type`, `target`, `status`, `user_approved`, `details`) with regex credential scrubbing (`ya29.*`, `ghp_*`, `AKIA*`, `Bearer`, `sk-*`) | **Reusable** | `audit_logger.py` (`AuditLogger`) — combined with Hermes `agent.redact.redact_sensitive_text` and PEM/URI scrubbing |
| `runtime/events.py` | `scrub_sensitive_data()` | Scrubs PEM private keys, URI passwords (`://user:pass@`), Bearer/Basic headers, OAuth tokens, and secret JSON/env fields | **Reusable** | Merged into `audit_logger.py` (`scrub_sensitive_value`, `scrub_dict`) |
| `config/settings.py` & `config.yaml` | `SecurityConfig`, `WorkspaceConfig` | Pydantic configuration model for `workspace.root`, `allow_*`, `require_confirmation_*`, `blocked_commands`, `blocked_paths` | **Adaptable** | `config.py` (`APASecurityConfig`) — reads Hermes `config.yaml` (`security.apa` and `plugins.entries.apa-security.settings`) with fail-closed schema validation |
| `security/backup.py` | `BackupManager` | Copies files to `.backups/` prior to mutation | **Deprecated / Replace** | Replaced by Hermes native `CheckpointManager` and git worktree/session mechanisms |

---

## 3. Detailed Component Audit

### 3.1 `BoundaryValidator` (`D:\Antigravity-Hermes\security\boundary.py`)
- **Strengths**:
  - Rejects null bytes (`\x00`) before `Path()` construction.
  - Detects UNC paths (`\\` or `//` prefix) on Windows to prevent SMB hash-leakage (NTLM relay) and remote share traversal.
  - Uses `Path.resolve()` and checks `target == self.root or self.root in target.parents`.
- **Gaps Identified & Addressed for Phase 3**:
  1. **Windows NT Device Namespace Bypass**: Paths starting with `\\.\`, `\??\`, `\\?\UNC\`, or `\\?\GLOBALROOT` can reference raw devices (`CON`, `NUL`, `PhysicalDrive0`) or bypass naive UNC checks. `PathValidator` integrates Hermes's `agent.file_safety.get_read_block_error` plus explicit raw-prefix rejection before path resolution.
  2. **Symlink & Windows Junction Escape on Non-Existent Child Paths**: When creating a new file inside a directory symlink or Windows NTFS junction (`mklink /J`), `PathValidator` resolves both the target path and its nearest existing ancestor via `os.path.realpath()` and verifies containment within the canonical workspace root.

### 3.2 `SecureFileSystemEngine` (`D:\Antigravity-Hermes\security\filesystem.py`)
- **Strengths**:
  - Comprehensive protected filename list (`.env`, `.env.local`, `.env.production`, `.env.development`, `id_rsa`, `id_ed25519`, `credentials.json`, `secrets.yaml`, `secrets.json`, `.npmrc`, `.pypirc`, `.netrc`).
  - Protected extension list (`.pem`, `.key`, `.pfx`, `.p12`, `.kdbx`, `.keystore`, `.jks`).
  - Protected directory segments (`.ssh`, `.aws`, `.gnupg`, `.kube`).
- **Gaps Identified & Addressed for Phase 3**:
  1. **Hermes & Antigravity Credential Stores**: Added protection for `.hermes`, `.gemini`, `antigravity-cli`, `auth.json`, `oauth_creds.json`, `.sqlite`/`.db` token stores, and `apa_security_audit.jsonl`.
  2. **Self-Tampering Protection**: Added explicit write/patch/delete denial for `plugins/apa-security` and security policy configuration files so a prompt-injected model cannot disable or weaken the guard during a turn.

### 3.3 `ProcessExecutionEngine` (`D:\Antigravity-Hermes\security\process.py`)
- **Strengths**:
  - Regex classification across `DESTRUCTIVE` (`rm -rf`, `Remove-Item -Recurse -Force`, `format`, `diskpart`, `reg delete`, `bcdedit`, `Stop-Computer`, `Clear-EventLog`, fork bombs), `HIGH_RISK_SYSTEM` (`net user`, `sc`, `schtasks`, `Set-ExecutionPolicy`, `takeown`, `icacls`), and `NETWORK_ACCESS` (`curl`, `Invoke-WebRequest`, `ssh`, `scp`, `nc`).
- **Gaps Identified & Addressed for Phase 3**:
  1. **Command Chaining & Subshell Injection**: Legacy regexes checked individual patterns without inspecting compound operators (`;`, `&&`, `||`, `|`, `` ` ``, `$()`). `CommandValidator` splits and inspects every segment of chained commands, blocks obfuscated execution (`-EncodedCommand` / `-enc`, `Invoke-Expression` / `iex`, `DownloadString`), and flags compound chaining on sensitive operations.
  2. **Filesystem Bypass via Terminal**: A model blocked from reading/deleting a file via `read_file` or `write_file` might attempt `cat ~/.ssh/id_rsa`, `Get-Content ..\.env`, or `Remove-Item ..\outside.txt` via the `terminal` tool. `CommandValidator` extracts path tokens from terminal commands and validates every extracted path through `FilesystemGuard` and `PathValidator`.

### 3.4 `PolicyChecker` & `PermissionManager` (`D:\Antigravity-Hermes\security\policy.py`, `permissions.py`)
- **Strengths**:
  - Clear 3-state permission decision (`ALLOW`, `DENY`, `REQUIRE_CONFIRMATION`) and 4-level risk severity (`LOW`, `MEDIUM`, `HIGH`, `CRITICAL`).
- **Gaps Identified & Addressed for Phase 3**:
  1. **Fail-Closed Validation**: Legacy config relied on trusted defaults. `APASecurityConfig` validates all setting types and policy modes (`strict`, `standard`, `permissive`), defaulting to fail-closed (`DENY`) whenever configuration is malformed or an unknown high-risk operation is encountered.
  2. **Hermes Approval Integration**: `REQUIRE_CONFIRMATION` maps cleanly to Hermes's `pre_tool_call` `{"action": "approve", ...}` directive (which invokes `tools.approval.request_tool_approval` fail-closed) or blocks immediately when unattended/configured to deny unconfirmed actions.

### 3.5 `AuditLogger` & `scrub_sensitive_data` (`D:\Antigravity-Hermes\security\audit.py`, `runtime\events.py`)
- **Strengths**:
  - Structured JSONL audit logging with `validate_log_file()` integrity verification and multi-pattern regex scrubbing.
- **Adaptation for Phase 3**:
  - Combines legacy `MASK_RULES` and `scrub_sensitive_data()` with Hermes's `agent.redact.redact_sensitive_text(force=True)` and writes profile-scoped audit records to `get_hermes_home() / "logs" / "apa_security_audit.jsonl"` (or custom configured path) with thread-safe locking.
