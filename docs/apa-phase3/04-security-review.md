# APA Phase 3.7 — Security Review & Threat Model

## 1. Executive Summary

This document presents the formal threat model and security architecture review for **APA Security Guard (`plugins/apa-security/`)** integrated with **Hermes Agent** and the **Google Antigravity Provider (`plugins/model-providers/antigravity/`)**.

---

## 2. Protected Assets

| Asset ID | Asset | Sensitivity | Location / Scope |
|---|---|---|---|
| **A1** | Google OAuth & Antigravity Credentials | **CRITICAL** | Managed exclusively by official `agy.exe` credential store; never accessed by APA |
| **A2** | Hermes Profile Secrets & Dotenv Files | **CRITICAL** | `<HERMES_HOME>/.env`, `.env.*`, `auth.json`, `oauth_creds.json`, `credentials.json` |
| **A3** | Cryptographic Private Keys & Certificates | **CRITICAL** | `~/.ssh/*`, `~/.aws/*`, `~/.gnupg/*`, `*.pem`, `*.key`, `*.pfx`, `*.p12`, `*.kdbx` |
| **A4** | Host Operating System & Outside-Workspace Files | **HIGH** | All files/directories outside `config.workspace_root` (e.g., `C:\Windows\System32`, user documents) |
| **A5** | APA Security Guard Code & Policy Config | **CRITICAL** | `plugins/apa-security/*`, `<HERMES_HOME>/config.yaml`, `.git/hooks/*` |
| **A6** | Security Audit Trail | **HIGH** | `<HERMES_HOME>/logs/apa_security_audit.jsonl` |

---

## 3. Threat Actors

| Actor | Description | Capabilities |
|---|---|---|
| **TA1: Prompt-Injected Model** | The primary or auxiliary LLM manipulated via indirect prompt injection (malicious web page, untrusted repository file, or external message) | Can emit arbitrary tool calls (`read_file`, `write_file`, `patch`, `terminal`, `execute_code`, `delegate_task`, `tool_call`) with crafted arguments |
| **TA2: Malicious Workspace Artifact** | Untrusted repository containing symlinks, Windows NTFS junctions (`mklink /J`), or scripts designed to escape the workspace when read/written | Can plant directory junctions or relative path references inside the workspace |
| **TA3: Subagent / Background Task** | A delegated child agent (`delegate_task`) or background shell session (`process`) attempting to perform actions denied to the parent turn | Can invoke tools or receive stdin via `process(action="submit")` |

---

## 4. Attack Surfaces & Trust Boundaries

```
┌─────────────────────────────────────────────────────────────────────────┐
│ UNTRUSTED INPUTS (Web Content, Repo Files, Indirect Prompt Injection)   │
└───────────────────────────────────┬─────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────┐
│ TRUST BOUNDARY 1: Model Output & Tool Call Bridge                       │
│ - Antigravity ACP (agy acp --stdio): AGY internal fs/* and permission   │
│   RPCs unconditionally rejected/cancelled at JSON-RPC transport layer   │
│ - Antigravity Print Mode (agy -p): <tool_call> parsed by bridge.py      │
└───────────────────────────────────┬─────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────┐
│ TRUST BOUNDARY 2: Hermes pre_tool_call Policy Gate (FAIL-CLOSED)        │
│ - plugins/apa-security/tool_interceptor.py                              │
│ - PolicyEngine -> FilesystemGuard + PathValidator + CommandValidator    │
│   + PermissionManager                                                   │
│ - Timeout or unhandled exception in pre_tool_call automatically blocks  │
└───────────────────────────────────┬─────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────┐
│ TRUST BOUNDARY 3: OS / Filesystem / Subprocess Execution                │
│ - Confined to canonical os.path.realpath(workspace_root)                │
│ - Post-execution audit logged with multi-layer secret redaction         │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## 5. Attack Scenarios & Mitigations

| # | Attack Scenario | Attack Vector | APA Security Guard Mitigation | Rule ID(s) |
|---|---|---|---|---|
| **S1** | **Prompt injection attempts destructive disk/file wipe** | Model calls `terminal(command="Remove-Item -Recurse -Force C:\\")` or `echo ok; rm -rf /` | `CommandValidator` splits chained segments and matches `_DESTRUCTIVE_PATTERNS`, blocking before subprocess creation. | `CMD_RECURSIVE_FORCE_DELETE`, `CMD_DISK_FORMAT` |
| **S2** | **Malicious tool call reads `.env` or SSH keys** | Model calls `read_file(path=".env")` or `terminal(command="cat ~/.ssh/id_rsa")` | `FilesystemGuard` blocks protected filenames/extensions/directories on both file tools and paths extracted from shell commands. | `FS_SENSITIVE_FILENAME`, `FS_PROTECTED_DIRECTORY` |
| **S3** | **Path traversal outside workspace** | Model calls `read_file(path="../../Outside/secret.txt")` or uses Windows NT device prefix `\\.\PhysicalDrive0` | `PathValidator` rejects null bytes, NT device prefixes (`\\.\`, `\??\`), and UNC shares, then verifies `os.path.realpath()` stays inside `workspace_root`. | `PATH_TRAVERSAL_OUTSIDE_WORKSPACE`, `PATH_NT_NAMESPACE`, `PATH_UNC_SHARE` |
| **S4** | **Symlink or Windows NTFS junction escape** | Workspace contains `mklink /J junction_out C:\Outside`; model calls `read_file` or `write_file` inside `junction_out` | `PathValidator._resolve_real_target` resolves `os.path.realpath()` on target and existing ancestors, detecting lexical-inside vs realpath-outside divergence. | `PATH_SYMLINK_ESCAPE` |
| **S5** | **Obfuscated PowerShell or LOLBin execution** | Model runs `powershell -EncodedCommand ...`, `Invoke-Expression`, `certutil -urlcache`, `mshta`, or `regsvr32` | `CommandValidator` blocks encoded PowerShell flags, `iex`/`DownloadString`, privilege escalation (`runas`/`sudo`), and LOLBin binaries. | `CMD_OBFUSCATED_POWERSHELL`, `CMD_SUSPICIOUS_LOLBIN`, `CMD_PRIVILEGE_ESCALATION` |
| **S6** | **Bypass via deferred `tool_call`, `execute_code`, or subagent** | Model wraps forbidden call inside `tool_call(name="read_file", ...)` or `execute_code(code="import subprocess...")` | `PolicyEngine` unwraps `tool_call`, inspects `execute_code` for `subprocess`/`os.system` and forbidden paths, and hooks every subagent's `handle_function_call`. | `CODE_SUBPROCESS_BLOCKED`, `PERM_SUBAGENT_DISABLED` |
| **S7** | **Self-tampering with security guard or git hooks** | Model attempts `write_file` or `patch` on `plugins/apa-security/*`, `apa_security_audit.jsonl`, or `.git/hooks/pre-commit` | `FilesystemGuard` enforces `immutable_security_paths` and `.git` write denial. | `FS_SELF_TAMPER_PROTECTION`, `FS_GIT_TAMPER` |
| **S8** | **Credential leakage via security audit logs** | Tool arguments or error messages contain OAuth tokens (`ya29.*`), API keys (`sk-*`, `ghp_*`, `AKIA*`), PEM keys, or passwords | `AuditLogger` scrubs all strings and dicts via `_EXTRA_MASK_RULES` + `redact_sensitive_text(force=True)` and omits raw file/code bodies (`[OMITTED_BODY]`). | `AuditLogger.log_event` |

---

## 6. Residual Risks & Operational Recommendations

1. **In-Workspace Allowed Script Execution**:
   - *Residual Risk*: When `allow_execute=True` and `require_confirmation_for_execute=False`, a Python script or test runner inside the workspace that is invoked via `terminal` executes with the OS user's permissions and could read/modify files using Python's own `open()` if not inspected statically.
   - *Mitigation*: Enable `policy_mode: "strict"` or `require_confirmation_for_execute: true` when working with untrusted repositories so every `terminal` and `execute_code` invocation requires human approval.
2. **Plugin Enablement in User Profiles**:
   - *Residual Risk*: In Hermes, bundled standalone plugins (`plugins/<name>/`) require `"apa-security"` in `plugins.enabled` in `<HERMES_HOME>/config.yaml` (or activation via `hermes plugins enable apa-security`).
   - *Mitigation*: Ensure APA profile provisioning includes `apa-security` in `plugins.enabled` and never lists it in `plugins.disabled`.
