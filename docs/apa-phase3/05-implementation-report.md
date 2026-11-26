# APA Phase 3 — Implementation Report

## 1. Executive Summary

Phase 3 integrates the legacy APA Security Guard (`D:\Antigravity-Hermes\security\*`) into the Hermes-based APA repository (`C:\Users\firdaus\APA`, branch `apa-development`) as a modular, zero-core-edit Hermes plugin at `plugins/apa-security/`.

- **Hermes Core Files Modified**: `0`
- **Antigravity Provider Regression Impact**: `0` (all Phase 2 and Phase 2.11 tests pass)
- **Boundary Lint (`scripts/boundary_lint.py --check`)**: `0` new crossings (all imports strictly use `hermes_cli/data/plugin_api.json`)
- **Code Health & Ruff (`scripts/check --only ruff`, `scripts/check --only health`)**: Passed (`0` blocking, `0` advisory)

---

## 2. Implemented Plugin Architecture (`plugins/apa-security/`)

```
plugins/apa-security/
├── plugin.yaml             # Manifest v2 declaring pre_tool_call, post_tool_call, and config_schema
├── __init__.py             # Plugin entrypoint: register(ctx) & get_active_interceptor()
├── models.py               # Enums & dataclasses: PermissionDecision, RiskLevel, OperationType, PolicyMode, AuditEvent
├── config.py               # APASecurityConfig with fail-closed validation & Hermes config.yaml loading
├── path_validator.py       # Canonical path resolution, NT-namespace/UNC rejection, symlink & NTFS junction escape guard
├── filesystem_guard.py     # Workspace containment, sensitive file/dir/ext rules, .git & self-tampering defense
├── command_validator.py    # PowerShell/shell destructive pattern blocker, command chaining/subshell guard, path extraction
├── permission_manager.py   # Operation permission toggles, confirmation gates, and fail-closed callback handling
├── policy_engine.py        # Central tool-call evaluator across read, write, delete, terminal, process, execute_code, tool_call
├── tool_interceptor.py     # Hermes pre_tool_call & post_tool_call hook handler
└── audit_logger.py         # Thread-safe JSONL audit logger with multi-layer credential & body scrubbing
```

---

## 3. Component Summary

| File | Key Classes / Functions | Security Responsibilities |
|---|---|---|
| `models.py` | `PermissionDecision`, `RiskLevel`, `OperationType`, `PolicyMode`, `SecurityEvaluation`, `AuditEvent` | Strongly typed security verdicts (`ALLOW`, `DENY`, `REQUIRE_CONFIRMATION`) and risk tiers (`LOW`, `MEDIUM`, `HIGH`, `CRITICAL`). |
| `config.py` | `APASecurityConfig.from_mapping`, `APASecurityConfig.load_from_hermes` | Reads `security.apa` and `plugins.entries.apa-security.settings`. Rejects invalid types/modes fail-closed (`CONFIG_FAIL_CLOSED`). |
| `path_validator.py` | `PathValidator.validate_path` | Blocks null bytes, Windows NT device paths (`\\.\`, `\??\`, `\\?\UNC\`), UNC shares, symlinks, and Windows NTFS junctions (`mklink /J`) escaping `workspace_root`. |
| `filesystem_guard.py` | `FilesystemGuard.evaluate_path_operation` | Blocks `.env*` (allowing `.env.example`), `auth.json`, `id_rsa`, `.pem`/`.key`/`.pfx`, `.ssh`/`.aws`/`.gemini`/`.hermes`, `.git` writes, and self-tampering on `plugins/apa-security/`. |
| `command_validator.py` | `CommandValidator.validate_command` | Blocks destructive commands (`Remove-Item -Recurse -Force`, `rm -rf`, `format`, `diskpart`, `reg delete`, `bcdedit`, `vssadmin`, `Stop-Computer`, fork bombs), obfuscated PowerShell (`-EncodedCommand`, `iex`, `DownloadString`), LOLBins (`mshta`, `certutil`, `regsvr32`), subshells (`$()`, backticks), dangerous chaining, and shell path escapes. |
| `permission_manager.py` | `PermissionManager.evaluate_permission` | Enforces `allow_read`, `allow_write`, `allow_delete`, `allow_execute`, `allow_network`, `allow_subagents`, and `require_confirmation_for_*`. Never auto-approves confirmation-gated actions. |
| `policy_engine.py` | `PolicyEngine.evaluate_tool_call` | Normalizes and evaluates all Hermes tools (`read_file`, `write_file`, `patch`, `search_files`, `delete_file`, `terminal`, `process`, `execute_code`, `delegate_task`, and unwrapped `tool_call`). |
| `tool_interceptor.py` | `ToolInterceptor.on_pre_tool_call`, `ToolInterceptor.on_post_tool_call` | Returns `{"action": "block", ...}` on `DENY`, `{"action": "approve", ...}` (or block) on `REQUIRE_CONFIRMATION`, and correlates post-execution outcomes. |
| `audit_logger.py` | `AuditLogger.log_event`, `scrub_sensitive_string`, `scrub_audit_payload` | Redacts OAuth tokens (`ya29.*`, `1//*`), GitHub/AWS/OpenAI keys, Bearer headers, URI passwords, PEM private keys, and raw file/code bodies before writing JSONL logs. |

---

## 4. Configuration Reference (`config.yaml`)

```yaml
plugins:
  enabled:
    - apa-security
  entries:
    apa-security:
      settings:
        enabled: true
        policy_mode: "standard"          # strict | standard | permissive
        workspace_root: ""               # defaults to active agent working directory
        enforce_workspace_boundary: true
        allow_read: true
        allow_write: true
        allow_delete: false
        allow_execute: true
        allow_network: true
        allow_subagents: true
        require_confirmation_for_write: false
        require_confirmation_for_delete: true
        require_confirmation_for_execute: false
        require_confirmation_for_high_risk: true
        unattended_confirmation_action: "approve_hook" # approve_hook | deny
        blocked_commands: []
        blocked_paths: []
        audit_enabled: true
```
