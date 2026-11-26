"""AGY CLI process manager for the Google Antigravity model provider plugin.

Manages executable resolution, version verification, Windows-safe environment
sanitization, persistent ``agy acp --stdio`` subprocess lifecycle, orphan
prevention, stderr diagnostics, and official CLI authentication delegation.
"""

from __future__ import annotations

import atexit
from collections import deque
from dataclasses import dataclass
import logging
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time
from typing import IO, Sequence

from agent.redact import redact_sensitive_text

logger = logging.getLogger(__name__)

COMMAND_ENV_VARS: tuple[str, ...] = ("ANTIGRAVITY_CLI_PATH", "AGY_CLI_PATH")
ARGS_ENV_VAR = "ANTIGRAVITY_ACP_ARGS"
DEFAULT_ACP_ARGS: tuple[str, ...] = ("acp", "--stdio")

# Essential OS and Windows environment variables required for agy.exe + OAuth.
_PRESERVED_ENV_KEYS: tuple[str, ...] = (
    "SYSTEMROOT",
    "WINDIR",
    "COMSPEC",
    "PATHEXT",
    "PATH",
    "LOCALAPPDATA",
    "APPDATA",
    "USERPROFILE",
    "HOME",
    "HOMEDRIVE",
    "HOMEPATH",
    "PROGRAMDATA",
    "PROGRAMFILES",
    "PROGRAMFILES(X86)",
    "COMMONPROGRAMFILES",
    "TEMP",
    "TMP",
    "TMPDIR",
    "OS",
    "USERNAME",
    "USER",
    "LOGNAME",
    "COMPUTERNAME",
    "NUMBER_OF_PROCESSORS",
    "PROCESSOR_ARCHITECTURE",
    "LANG",
    "LC_ALL",
    "TERM",
    "COLORTERM",
    "NO_COLOR",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "no_proxy",
    "all_proxy",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE",
    "NODE_EXTRA_CA_CERTS",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "XDG_CACHE_HOME",
    "XDG_RUNTIME_DIR",
    "DBUS_SESSION_BUS_ADDRESS",
    "DISPLAY",
    "WAYLAND_DISPLAY",
)

_PRESERVED_ENV_PREFIXES: tuple[str, ...] = ("AGY_", "ANTIGRAVITY_")


@dataclass(frozen=True)
class AGYVersionInfo:
    """Resolved AGY executable path and version string."""

    executable: str
    version: str
    raw_output: str


@dataclass(frozen=True)
class AGYAuthStatus:
    """Authentication status reported through official AGY CLI surfaces."""

    installed: bool
    authenticated: bool
    executable: str | None
    version: str | None
    detail: str
    models: tuple[str, ...] = ()


def windows_creation_flags() -> int:
    """Return Windows subprocess creation flags that suppress console windows."""
    if sys.platform == "win32":
        return int(getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000))
    return 0


def build_agy_env(base_env: dict[str, str] | None = None) -> dict[str, str]:
    """Construct a sanitized environment preserving Windows and AGY variables."""
    source = os.environ if base_env is None else base_env
    lookup = {k.upper(): (k, v) for k, v in source.items()}
    env: dict[str, str] = {}

    for key in _PRESERVED_ENV_KEYS:
        found = lookup.get(key.upper())
        if found is not None and found[1]:
            env[found[0]] = found[1]
        elif source.get(key):
            env[key] = source[key]

    for k, v in source.items():
        upper = k.upper()
        if any(upper.startswith(prefix) for prefix in _PRESERVED_ENV_PREFIXES) and v:
            env[k] = v

    if sys.platform == "win32":
        env.setdefault("SYSTEMROOT", os.environ.get("SYSTEMROOT", r"C:\Windows"))
        env.setdefault("PATHEXT", os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD"))
        if "HOME" not in env and "USERPROFILE" in env:
            env["HOME"] = env["USERPROFILE"]

    return env


def _candidate_windows_paths(env: dict[str, str]) -> list[Path]:
    """Return standard Windows installation paths for agy.exe."""
    candidates: list[Path] = []
    local_app_data = env.get("LOCALAPPDATA") or os.environ.get("LOCALAPPDATA", "")
    user_profile = env.get("USERPROFILE") or os.environ.get("USERPROFILE", "")
    app_data = env.get("APPDATA") or os.environ.get("APPDATA", "")

    if local_app_data:
        candidates.append(Path(local_app_data) / "agy" / "bin" / "agy.exe")
        candidates.append(Path(local_app_data) / "Programs" / "Antigravity" / "bin" / "agy.exe")
        candidates.append(Path(local_app_data) / "Programs" / "agy" / "agy.exe")
    if user_profile:
        candidates.append(Path(user_profile) / ".local" / "bin" / "agy.exe")
        candidates.append(Path(user_profile) / "AppData" / "Local" / "agy" / "bin" / "agy.exe")
    if app_data:
        candidates.append(Path(app_data) / "agy" / "bin" / "agy.exe")
    return candidates


def resolve_agy_executable(
    command: str | None = None,
    env: dict[str, str] | None = None,
) -> str | None:
    """Locate the official ``agy`` / ``agy.exe`` binary."""
    source_env = os.environ if env is None else env
    search_path = source_env.get("PATH") or os.environ.get("PATH")

    raw_candidates: list[str] = []
    if command and command.strip():
        raw_candidates.append(command.strip())
    for env_key in COMMAND_ENV_VARS:
        val = source_env.get(env_key, "").strip()
        if val:
            raw_candidates.append(val)
    raw_candidates.extend(["agy", "agy.exe"])

    for candidate in raw_candidates:
        expanded = os.path.expandvars(os.path.expanduser(candidate))
        candidate_path = Path(expanded)
        if candidate_path.is_file():
            return str(candidate_path)
        found = shutil.which(expanded, path=search_path)
        if found:
            return found

    for win_path in _candidate_windows_paths(dict(source_env)):
        if win_path.is_file():
            return str(win_path)
    return None


def parse_models_output(output: str) -> list[str]:
    """Parse ``agy models`` tab-delimited output into model IDs."""
    models: list[str] = []
    seen: set[str] = set()
    for raw_line in (output or "").splitlines():
        line = raw_line.strip()
        if not line or line.startswith(("#", "Available", "Loading", "Error", "Warning")):
            continue
        model_id = line.split("\t", 1)[0].split()[0].strip()
        if model_id and "/" not in model_id and model_id not in seen:
            seen.add(model_id)
            models.append(model_id)
    return models


class AGYProcessManager:
    """Manages the ``agy acp --stdio`` subprocess and CLI utility commands."""

    def __init__(
        self,
        command: str | None = None,
        args: Sequence[str] | None = None,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        self._explicit_command = (command or "").strip() or None
        self._args = tuple(args) if args is not None else DEFAULT_ACP_ARGS
        self._cwd = str(cwd) if cwd is not None else None
        self._custom_env = dict(env) if env else None
        self._lock = threading.RLock()
        self._proc: subprocess.Popen[str] | None = None
        self._stderr_lines: deque[str] = deque(maxlen=50)
        self._stderr_thread: threading.Thread | None = None
        self._version_cache: AGYVersionInfo | None = None
        self._restart_count = 0
        atexit.register(self.stop)

    @property
    def args(self) -> tuple[str, ...]:
        return self._args

    @property
    def restart_count(self) -> int:
        return self._restart_count

    def resolve_executable(self) -> str:
        """Return the resolved path to ``agy`` or raise ``FileNotFoundError``."""
        resolved = resolve_agy_executable(self._explicit_command, self._custom_env)
        if not resolved:
            raise FileNotFoundError(
                "Google Antigravity CLI ('agy') was not found on PATH or standard install locations. "
                "Install Antigravity CLI or set ANTIGRAVITY_CLI_PATH to the path of agy.exe."
            )
        return resolved

    def build_environment(self) -> dict[str, str]:
        """Build the sanitized subprocess environment."""
        merged = dict(os.environ)
        if self._custom_env:
            merged.update(self._custom_env)
        return build_agy_env(merged)

    def _cli_command_prefix(self) -> list[str]:
        """Return base executable prefix (supporting python wrapper scripts in tests)."""
        executable = self.resolve_executable()
        if self._args and str(self._args[0]).endswith(".py"):
            return [executable, str(self._args[0])]
        return [executable]

    def verify_version(self, timeout: float = 10.0, force: bool = False) -> AGYVersionInfo:
        """Run ``agy --version`` and return structured version metadata."""
        if self._version_cache is not None and not force:
            return self._version_cache
        prefix = self._cli_command_prefix()
        result = subprocess.run(
            [*prefix, "--version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=self.build_environment(),
            timeout=timeout,
            check=False,
            creationflags=windows_creation_flags(),
        )
        combined = f"{result.stdout or ''}\n{result.stderr or ''}".strip()
        if result.returncode != 0:
            raise RuntimeError(
                f"AGY version check failed (exit {result.returncode}): {redact_sensitive_text(combined)}"
            )
        first_line = combined.splitlines()[0].strip() if combined else "unknown"
        info = AGYVersionInfo(executable=prefix[0], version=first_line, raw_output=combined)
        self._version_cache = info
        return info

    def is_running(self) -> bool:
        """Return True if the managed ACP subprocess is currently alive."""
        with self._lock:
            return self._proc is not None and self._proc.poll() is None

    def start(self) -> subprocess.Popen[str]:
        """Launch or reuse the persistent ``agy acp --stdio`` subprocess."""
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                return self._proc

            self._cleanup_process_locked()
            executable = self.resolve_executable()
            env = self.build_environment()
            cwd = self._cwd or os.getcwd()
            self._stderr_lines.clear()

            proc = subprocess.Popen(
                [executable, *self._args],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                cwd=cwd,
                env=env,
                creationflags=windows_creation_flags(),
            )
            self._proc = proc
            self._start_stderr_drain(proc)
            return proc

    def restart(self) -> subprocess.Popen[str]:
        """Terminate any existing process and start a fresh ``agy acp --stdio`` child."""
        with self._lock:
            self._restart_count += 1
            self._cleanup_process_locked()
            return self.start()

    def stop(self, timeout: float = 3.0) -> None:
        """Gracefully terminate the managed subprocess and prevent orphan children."""
        with self._lock:
            self._cleanup_process_locked(timeout=timeout)

    def stdin(self) -> IO[str]:
        """Return the active subprocess stdin stream."""
        with self._lock:
            if self._proc is None or self._proc.poll() is not None or self._proc.stdin is None:
                raise RuntimeError("AGY ACP subprocess is not running.")
            return self._proc.stdin

    def stdout(self) -> IO[str]:
        """Return the active subprocess stdout stream."""
        with self._lock:
            if self._proc is None or self._proc.poll() is not None or self._proc.stdout is None:
                raise RuntimeError("AGY ACP subprocess is not running.")
            return self._proc.stdout

    def recent_stderr(self) -> str:
        """Return redacted recent stderr lines for diagnostics."""
        with self._lock:
            raw = "\n".join(self._stderr_lines).strip()
        return redact_sensitive_text(raw) if raw else ""

    def _start_stderr_drain(self, proc: subprocess.Popen[str]) -> None:
        stderr_pipe = proc.stderr
        if stderr_pipe is None:
            return

        def _drain() -> None:
            try:
                for line in iter(stderr_pipe.readline, ""):
                    cleaned = line.rstrip("\r\n")
                    if cleaned:
                        self._stderr_lines.append(cleaned)
            except (OSError, ValueError) as exc:
                logger.debug("AGY stderr reader closed: %s", exc)

        # Daemon thread only drains pipe into deque; no Hermes profile ContextVar access.
        thread = threading.Thread(  # health: allow HX012 -- pure pipe drain into in-memory deque; touches no profile state
            target=_drain,
            name="agy-acp-stderr",
            daemon=True,
        )
        thread.start()
        self._stderr_thread = thread

    def _cleanup_process_locked(self, timeout: float = 3.0) -> None:
        proc = self._proc
        self._proc = None
        if proc is None:
            return

        for stream in (proc.stdin, proc.stdout, proc.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError as exc:
                    logger.debug("Ignored stream close error on AGY stop: %s", exc)

        if proc.poll() is not None:
            return

        try:
            proc.terminate()
            proc.wait(timeout=max(0.2, timeout))
        except subprocess.TimeoutExpired:
            self._force_kill_process(proc)
        except OSError as exc:
            logger.debug("Ignored terminate error on AGY stop: %s", exc)
            self._force_kill_process(proc)

    def _force_kill_process(self, proc: subprocess.Popen[str]) -> None:
        if sys.platform == "win32" and proc.pid:
            try:
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                    capture_output=True,
                    text=True,
                    timeout=3.0,
                    check=False,
                    creationflags=windows_creation_flags(),
                )
            except (OSError, subprocess.SubprocessError) as exc:
                logger.debug("taskkill fallback failed for PID %s: %s", proc.pid, exc)
        try:
            proc.kill()
            proc.wait(timeout=2.0)
        except (OSError, subprocess.SubprocessError) as exc:
            logger.debug("proc.kill cleanup completed with notice: %s", exc)

    def fetch_live_models(self, timeout: float = 15.0) -> list[str]:
        """Query ``agy models`` and return available model IDs."""
        prefix = self._cli_command_prefix()
        result = subprocess.run(
            [*prefix, "models"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=self.build_environment(),
            timeout=timeout,
            check=False,
            creationflags=windows_creation_flags(),
        )
        if result.returncode != 0:
            stderr_msg = redact_sensitive_text((result.stderr or result.stdout or "").strip())
            raise RuntimeError(f"agy models exited with {result.returncode}: {stderr_msg}")
        return parse_models_output(result.stdout or "")

    def check_auth_status(self, timeout: float = 10.0) -> AGYAuthStatus:
        """Check AGY installation and authentication status via official CLI commands."""
        executable = resolve_agy_executable(self._explicit_command, self._custom_env)
        if not executable:
            return AGYAuthStatus(
                installed=False,
                authenticated=False,
                executable=None,
                version=None,
                detail="AGY CLI not found. Install agy or set ANTIGRAVITY_CLI_PATH.",
            )
        try:
            ver_info = self.verify_version(timeout=timeout)
            version_str = ver_info.version
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            version_str = None
            logger.debug("AGY version check failed during auth status: %s", exc)

        try:
            models = self.fetch_live_models(timeout=timeout)
            if models:
                return AGYAuthStatus(
                    installed=True,
                    authenticated=True,
                    executable=executable,
                    version=version_str,
                    detail=f"Authenticated via AGY CLI ({len(models)} models available).",
                    models=tuple(models),
                )
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            detail = redact_sensitive_text(str(exc))
            lowered = detail.lower()
            if any(tok in lowered for tok in ("login", "auth", "unauthorized", "unauthenticated", "sign in")):
                return AGYAuthStatus(
                    installed=True,
                    authenticated=False,
                    executable=executable,
                    version=version_str,
                    detail="Not logged in. Run 'agy login' to authenticate with Google OAuth.",
                )
            logger.debug("AGY live model probe returned non-fatal status: %s", detail)

        return AGYAuthStatus(
            installed=True,
            authenticated=True,
            executable=executable,
            version=version_str,
            detail="AGY CLI installed (run 'agy login' if prompted during session).",
        )

    def run_login(self, timeout: float = 300.0) -> int:
        """Delegate interactive Google OAuth login to ``agy login``."""
        prefix = self._cli_command_prefix()
        result = subprocess.run(
            [*prefix, "login"],
            env=self.build_environment(),
            timeout=timeout,
            check=False,
        )
        return int(result.returncode)

    def run_logout(self, timeout: float = 30.0) -> int:
        """Delegate credential sign-out to ``agy logout``."""
        prefix = self._cli_command_prefix()
        result = subprocess.run(
            [*prefix, "logout"],
            env=self.build_environment(),
            timeout=timeout,
            check=False,
        )
        return int(result.returncode)

    def run_print_prompt(
        self,
        prompt_text: str,
        *,
        model: str | None = None,
        reasoning_effort: str | None = None,
        timeout: float = 180.0,
    ) -> dict[str, object]:
        """Execute ``agy -p`` in JSON output mode as a secondary fallback transport."""
        prefix = self._cli_command_prefix()
        cmd: list[str] = [*prefix, "--output-format", "json"]
        if model:
            cmd.extend(["--model", model])
        if reasoning_effort:
            cmd.extend(["--effort", reasoning_effort])
        cmd.extend(["-p", prompt_text])

        started = time.monotonic()
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=self._cwd or os.getcwd(),
            env=self.build_environment(),
            timeout=timeout,
            check=False,
            creationflags=windows_creation_flags(),
        )
        duration_ms = int((time.monotonic() - started) * 1000)
        if result.returncode != 0:
            err_text = redact_sensitive_text((result.stderr or result.stdout or "").strip())
            raise RuntimeError(f"AGY print fallback failed (exit {result.returncode}): {err_text}")
        return {
            "stdout": result.stdout or "",
            "stderr": redact_sensitive_text(result.stderr or ""),
            "duration_ms": duration_ms,
        }
