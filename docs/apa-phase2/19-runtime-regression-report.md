# APA Phase 2.12.8 — Runtime Regression Report

## 1. Executive Summary

Full regression testing, plugin boundary verification, Ruff lint, and Hermes Code Health ratchets were executed against the local repository (`C:\Users\firdaus\APA`, branch `apa-development`) using the Hermes Python `3.14.9` runtime environment.

- **Overall Regression Status**: **PASS (`50 / 50` tests passed across the 4 specified suites; `53 / 53` including `tests/providers/test_plugin_discovery.py`)**
- **Boundary Lint (`scripts/boundary_lint.py --check`)**: **PASS (`0` new crossings)**
- **Ruff Lint (`scripts/check --only ruff`)**: **PASS (`ok`)**
- **Code Health (`scripts/check --only health`)**: **PASS (`0 blocking, 0 advisory`)**
- **Hermes Core Modifications**: **`0` files**

---

## 2. Pytest Suite Results

| Test Suite | Total Tests | Passed | Failed | Skipped | Status |
|---|---:|---:|---:|---:|---|
| `tests/agent/test_antigravity_provider.py` | 9 | 9 | 0 | 0 | **PASS** |
| `tests/plugins/test_apa_security.py` | 24 | 24 | 0 | 0 | **PASS** |
| `tests/agent/test_acp_client.py` | 5 | 5 | 0 | 0 | **PASS** |
| `tests/agent/test_external_process_streaming.py` | 12 | 12 | 0 | 0 | **PASS** |
| `tests/providers/test_plugin_discovery.py` *(additional)* | 3 | 3 | 0 | 0 | **PASS** |
| **Total** | **53** | **53** | **0** | **0** | **PASS** |

---

## 3. Individual Test Cases Verified

### 3.1 `tests/agent/test_antigravity_provider.py` (9/9 PASS)
- `test_antigravity_provider_discovered_and_registered` — **PASSED**
- `test_antigravity_enables_streaming_in_turn_api_call` — **PASSED**
- `test_process_manager_preserves_windows_env_and_strips_unrelated_secrets` — **PASSED**
- `test_process_manager_version_and_live_models` — **PASSED**
- `test_acp_transport_session_reuse_and_compression_reset` — **PASSED**
- `test_streaming_incremental_chunks_and_tool_call_lookahead` — **PASSED**
- `test_multiple_tool_calls_and_permission_denial` — **PASSED**
- `test_auth_error_detection_and_classification` — **PASSED**
- `test_process_crash_recovery_and_print_fallback` — **PASSED**

### 3.2 `tests/plugins/test_apa_security.py` (24/24 PASS)
- `TestGroupAPermissions::test_read_permission_allow_and_deny` — **PASSED**
- `TestGroupAPermissions::test_write_permission_allow_and_deny` — **PASSED**
- `TestGroupAPermissions::test_delete_permission_denied_by_default_and_confirmation_when_enabled` — **PASSED**
- `TestGroupAPermissions::test_execute_permission_allow_and_deny` — **PASSED**
- `TestGroupAPermissions::test_confirmation_never_auto_approved_and_callback_behavior` — **PASSED**
- `TestGroupAPermissions::test_malformed_configuration_fails_closed` — **PASSED**
- `TestGroupBFilesystem::test_allowed_workspace_access` — **PASSED**
- `TestGroupBFilesystem::test_access_outside_workspace_blocked` — **PASSED**
- `TestGroupBFilesystem::test_path_traversal_and_special_namespaces_blocked` — **PASSED**
- `TestGroupBFilesystem::test_sensitive_files_and_directories_blocked` — **PASSED**
- `TestGroupBFilesystem::test_symlink_escape_blocked` — **PASSED**
- `TestGroupBFilesystem::test_windows_junction_escape_blocked` — **PASSED**
- `TestGroupBFilesystem::test_self_tampering_and_git_hook_protection` — **PASSED**
- `TestGroupCTerminal::test_safe_commands_allowed` — **PASSED**
- `TestGroupCTerminal::test_destructive_powershell_and_shell_commands_blocked` — **PASSED**
- `TestGroupCTerminal::test_command_chaining_and_subshell_injection_blocked` — **PASSED**
- `TestGroupCTerminal::test_suspicious_executables_and_obfuscated_powershell_blocked` — **PASSED**
- `TestGroupCTerminal::test_terminal_filesystem_boundary_escape_blocked` — **PASSED**
- `TestGroupDToolIntegration::test_pre_and_post_tool_call_interception_and_audit` — **PASSED**
- `TestGroupDToolIntegration::test_confirmation_directive_integration` — **PASSED**
- `TestGroupDToolIntegration::test_audit_logger_redacts_all_credential_types` — **PASSED**
- `TestGroupEBypassResistance::test_real_plugin_discovery_and_handle_function_call_blocks_before_execution` — **PASSED**
- `TestGroupEBypassResistance::test_antigravity_acp_and_print_mode_tool_calls_cannot_bypass_security_guard` — **PASSED**
- `TestGroupEBypassResistance::test_subagent_delegation_policy_enforcement` — **PASSED**

### 3.3 `tests/agent/test_acp_client.py` (5/5 PASS)
- `test_subclass_launch_fallback_reads_its_env_vars_in_order` — **PASSED**
- `test_model_entry_disabled_hook_owns_vendor_filtering` — **PASSED**
- `test_errors_carry_the_subclass_label_and_early_exit_hook` — **PASSED**
- `test_malformed_vendor_model_options_fall_back_to_the_session_default` — **PASSED**
- `test_deprecated_alias_keeps_a_frozen_out_of_tree_subclass_working` — **PASSED**

### 3.4 `tests/agent/test_external_process_streaming.py` (12/12 PASS)
- `test_streaming_capability_preserves_positional_plugin_construction` — **PASSED**
- `test_external_process_can_opt_into_streaming` *(10 parametrized cases)* — **PASSED**
- `test_discovered_process_provider_delivers_progress_before_completion` — **PASSED**

---

## 4. Boundary Lint, Ruff, and Code Health Output

```text
module_to_core: 836
core_to_module: 20

scripts/check: 1 checks, ok

=== health (ok) ===
Allowed by comment (reviewers: check the reasons):
  plugins/model-providers/antigravity/acp_transport.py:494  HX012  AGYACPTransport._ensure_reader_thread_locked  -- JSON-RPC frame demux thread; accesses no profile ContextVars
  plugins/model-providers/antigravity/client.py:436  BLE001  AntigravityClient.stream_completion._worker  -- forward worker exception to consumer generator
  plugins/model-providers/antigravity/client.py:439  HX012  AntigravityClient.stream_completion  -- turn stream producer; pushes events to local queue only
  plugins/model-providers/antigravity/process_manager.py:370  HX012  AGYProcessManager._start_stderr_drain  -- pure pipe drain into in-memory deque; touches no profile state

code health: 17 files vs 77a30e912799: 0 blocking, 0 advisory (2.1s)

scripts/check: 1 checks, ok
```
