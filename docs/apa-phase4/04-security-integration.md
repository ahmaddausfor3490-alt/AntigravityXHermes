# APA Phase 4.4 — Skills Security Integration Report

**Project:** APA (Antigravity Personal Agent)  
**Repository:** `C:\Users\firdaus\APA`  
**Branch:** `apa-development`  
**Date:** November 16, 2026  
**Status:** Completed & Fully Verified  

---

## 1. Executive Summary

Phase 4.4 establishes the **Skills Security Integration** layer for APA. Building upon the Phase 4.2 Registry and Phase 4.3 Discovery, Loader, and Validation layers, this phase connects the APA Skills subsystem directly with the **APA Security Guard** (`plugins/apa-security`), enforcing strict operation classifications, root containment boundaries, mutation locks, and command execution oversight.

Key capabilities delivered:
1. **Strict Operation Classification:**
   - `skill_view` mapped strictly to `OperationType.READ`.
   - `skill_manage` actions `create`, `edit`, `patch`, and `write_file` mapped strictly to `OperationType.WRITE`.
   - `skill_manage` actions `delete` and `remove_file` mapped strictly to `OperationType.DELETE`.
   - Batch operations evaluated on a per-sub-operation basis with fail-closed semantics; mixed batches containing a deletion are aggregated to `OperationType.DELETE` and never demoted.
2. **Approved Skill Root Authorization:**
   - Resolves canonical skill roots: project workspace skills (`workspace/skills`, `workspace/.hermes/skills`), user profile skills (`~/.hermes/skills`), and explicitly configured custom roots (`allowed_skill_roots`).
   - The parent `.hermes` directory remains strictly protected (`.env`, `config.yaml`, `auth.json`, etc.); only the dedicated `skills/` subdirectory is accessible.
3. **Skill Mutation & Pinned Skill Protection:**
   - Skills configured in `pinned_skills` (e.g. `apa-core`, `antigravity-guide`) are locked against modification, patching, overwriting, and deletion. Read-only viewing remains allowed.
   - Deletion operations enforce user confirmation requirements.
4. **Symlink, Windows NTFS Junction & Path Traversal Defense:**
   - Realpath resolution detects symlink and NTFS directory junction escapes outside authorized boundaries.
   - Traversal sequences (`..`), Windows device namespaces (`\\.\`, `\\?\`), and embedded null bytes are rejected immediately with critical risk ratings.
5. **Script Execution & Command Controls:**
   - Skill script execution is routed strictly through Hermes `terminal` tool without parallel subprocess engines, preserving `CommandValidator` and `FilesystemGuard` protection against destructive shell commands and credential exfiltration.
6. **Audit Logging & Multi-Layer Credential Redaction:**
   - All skill operations (pre-tool and post-tool) are logged to the JSONL audit trail with multi-layer scrubbing of API keys, Google OAuth tokens, GitHub tokens, passwords, and sensitive fields.
7. **Zero Hermes Core Modification:**
   - Implemented entirely within `plugins/apa-security` and `plugins/apa-skills` via standard Hermes plugin hooks and interceptors. Zero core files modified.

---

## 2. Architecture & Interception Flow

```
+----------------------------------------------------------------------------------------------------+
|                                    Hermes Agent Turn Loop                                          |
+----------------------------------------------------------------------------------------------------+
                                                  |
                         (skill_view / skill_manage / terminal)
                                                  v
+----------------------------------------------------------------------------------------------------+
|                                       APA Security Guard                                           |
|                                    (plugins/apa-security)                                          |
|                                                                                                    |
|  +----------------------------------------------------------------------------------------------+  |
|  |                             ToolInterceptor (pre_tool_call hook)                             |  |
|  +----------------------------------------------------------------------------------------------+  |
|                                                 |                                                  |
|                                                 v                                                  |
|  +----------------------------------------------------------------------------------------------+  |
|  |                                         PolicyEngine                                         |  |
|  |                                                                                              |  |
|  |    +-----------------------------+                     +----------------------------------+  |  |
|  |    |     SkillsSecurityGuard     |                     |        PermissionManager         |  |  |
|  |    |                             |                     |                                  |  |  |
|  |    | - evaluate_skill_view()     |--(Base Evaluation)->| - allow_read / allow_write / ... |  |  |
|  |    | - evaluate_skill_manage()   |                     | - Confirmation Gate              |  |  |
|  |    | - Root Containment Check    |                     | - Mode (Strict/Standard/Perm)    |  |  |
|  |    | - Symlink/Junction Escapes  |                     +----------------------------------+  |  |
|  |    | - Pinned Skill Protection   |                                      |                     |  |
|  |    | - Payload Pattern Analysis  |                                      v                     |  |
|  |    +-----------------------------+                            (Final Auth Decision)           |  |
|  +----------------------------------------------------------------------------------------------+  |
|                                                 |                                                  |
|                        +------------------------+------------------------+                         |
|                        |                                                 |                         |
|                 (ALLOW / CONFIRM)                                     (DENY)                       |
|                        |                                                 |                         |
|                        v                                                 v                         |
|               Execute Hermes Tool                           Block Tool Call Fail-Closed            |
|                        |                                                 |                         |
|                        +------------------------+------------------------+                         |
|                                                 |                                                  |
|                                                 v                                                  |
|  +----------------------------------------------------------------------------------------------+  |
|  |                          AuditLogger (scrub_audit_payload redaction)                         |  |
|  |                             persists JSONL to apa_security_audit.jsonl                       |  |
|  +----------------------------------------------------------------------------------------------+  |
+----------------------------------------------------------------------------------------------------+
```

---

## 3. Security Implementation Details

### 3.1 SkillsSecurityGuard (`plugins/apa-security/skills_guard.py`)
- **Approved Roots Resolution (`get_approved_skill_roots`):**
  - Project workspace roots: `workspace/skills`, `workspace/.hermes/skills`.
  - Profile root: `~/.hermes/skills` via `hermes_constants:get_skills_dir()`.
  - Configured custom roots: `config.allowed_skill_roots`.
- **Containment & Reparse Point Inspection (`_check_target_containment_and_escapes`):**
  - Resolves disk targets through `_resolve_real_target()`.
  - Detects symlinks and Windows NTFS directory junctions escaping approved boundaries, returning `SKILL_SYMLINK_ESCAPE` or `SKILL_JUNCTION_ESCAPE`.
- **Operation Classification & Batch Aggregation (`evaluate_skill_manage`):**
  - Evaluates individual sub-operations independently.
  - Fail-closed: returns immediate denial on the first violating operation.
  - Aggregates operations: `OperationType.DELETE` takes precedence over `WRITE` and `READ`.
- **Pinned Skill Enforcement (`is_pinned_or_protected`):**
  - Cross-references `config.pinned_skills`.
  - Blocks `edit`, `patch`, `write_file`, `delete`, and `remove_file` with `SKILL_PINNED_PROTECTED`.
- **Hazardous Pattern Scanning (`_check_content_payload`):**
  - Inspects file payloads for destructive patterns (e.g., `Invoke-Expression`, encoded PowerShell, `rm -rf /`, `curl | bash`, disk formatting).

### 3.2 Configuration Extensions (`plugins/apa-security/config.py`)
- Added `allowed_skill_roots: tuple[Path, ...]` to define explicit external skill roots.
- Added `pinned_skills: frozenset[str]` to register immutable skills.
- Supported mapping ingestion via `_parse_collection_fields()` with path resolution and error tracking.

### 3.3 Policy Engine Wiring (`plugins/apa-security/policy_engine.py`)
- Routed `skill_view` and `view_skill` through `skills_guard.evaluate_skill_view()`.
- Routed `skill_manage` and `manage_skill` through `skills_guard.evaluate_skill_manage()`.
- Linked evaluations to `PermissionManager` to enforce capability toggles and confirmation requirements.

---

## 4. Test Suite Execution & Verification

The Phase 4.4 automated security test suite was authored in `tests/plugins/test_apa_skills_security.py` covering all 19 mandatory security scenarios.

### 4.1 Test Case Matrix

| # | Test Identifier | Focus Area | Result |
| :---: | :--- | :--- | :---: |
| 1 | `test_skill_view_allowed` | Read operation inside approved root allowed | **PASSED** |
| 2 | `test_skill_view_outside_approved_root_blocked` | Access outside approved roots blocked | **PASSED** |
| 3 | `test_skill_manage_create_classified_as_write` | `create` classified as WRITE; disabled when write=False | **PASSED** |
| 4 | `test_skill_manage_edit_classified_as_write` | `edit` classified as WRITE | **PASSED** |
| 5 | `test_skill_manage_patch_classified_as_write` | `patch` classified as WRITE | **PASSED** |
| 6 | `test_skill_manage_delete_classified_as_delete` | `delete` classified as DELETE; disabled when delete=False | **PASSED** |
| 7 | `test_skill_manage_remove_file_classified_as_delete` | `remove_file` classified as DELETE | **PASSED** |
| 8 | `test_mixed_batch_operation_classification` | Mixed batch classified as DELETE; fail-closed on bad op | **PASSED** |
| 9 | `test_delete_requires_confirmation` | Deletions require confirmation; user approval/denial gate | **PASSED** |
| 10 | `test_pinned_skill_protection` | Modifications and deletions of pinned skills denied | **PASSED** |
| 11 | `test_path_traversal_rejection` | Traversal in name or file path denied (`..`) | **PASSED** |
| 12 | `test_symlink_escape_rejection` | Symlinks escaping approved roots blocked | **PASSED** |
| 13 | `test_windows_junction_escape_rejection` | NTFS directory junctions escaping roots blocked | **PASSED** |
| 14 | `test_sensitive_file_access_blocked` | Access to `.env`, `config.yaml`, `id_rsa`, `*.pem` blocked | **PASSED** |
| 15 | `test_skill_script_command_validation` | Terminal execution governed by `CommandValidator` | **PASSED** |
| 16 | `test_credential_access_attempt_blocked` | Exfiltration attempts via command/file access blocked | **PASSED** |
| 17 | `test_unauthorized_skill_root_blocked` | Installation/access in arbitrary roots blocked | **PASSED** |
| 18 | `test_audit_logging_and_secret_redaction` | Full operation audit with credential scrubbing | **PASSED** |
| 19 | `test_security_guard_bypass_attempts_blocked` | Null bytes, device namespaces, malformed payloads blocked | **PASSED** |

### 4.2 Comprehensive Multi-Phase Regression Results

All 8 plugin test suites were executed concurrently:
```
tests/plugins/test_apa_skills_security.py   ................... [ 19 passed ]
tests/plugins/test_apa_skills_loader.py     ........            [  8 passed ]
tests/plugins/test_apa_skills_discovery.py  ......              [  6 passed ]
tests/plugins/test_apa_skills_validator.py  ...........         [ 11 passed ]
tests/plugins/test_apa_skills_registry.py   ...............     [ 15 passed ]
tests/plugins/test_apa_security.py          ....................[ 20 passed ]
tests/agent/test_antigravity_provider.py    ..............      [ 14 passed ]
tests/agent/test_acp_client.py              .....               [  5 passed ]

============================= 98 passed in 39.83s =============================
```

---

## 5. Quality Gate Status

1. **Ruff Linter:**
   - Command: `uv run python scripts/check --only ruff`
   - Output: `scripts/check: 1 checks, ok` (0 errors)
2. **Code Health Inspector:**
   - Command: `uv run python scripts/check --only health`
   - Output: `0 blocking, 0 advisory` (0 violations)
3. **Boundary Lint Verification:**
   - Command: `uv run python scripts/boundary_lint.py --check`
   - Output: `module_to_core: 839`, `core_to_module: 20` (exit code 0; 0 new crossings)
4. **Hermes Core Policy:**
   - Zero Hermes Core files modified or deleted.
