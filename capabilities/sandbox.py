"""
capabilities/sandbox.py
Sandbox - Safe command execution tool (BaseTool).

Usage:
    sandbox = Sandbox()
    result = await sandbox.execute(command="ls -la", timeout=30)

Security chain: LLM -> PolicyEngine blacklist -> Hook -> subprocess -> output truncation

P0-1 Security hardening:
  1. shell=False + shlex.split() - eliminate command injection
  2. start_new_session=True + os.killpg() - process group isolation
  3. Environment variable whitelist - only pass safe vars
P2-8 Resource limits:
  4. CPU time limit (60s) + memory limit (256MB)
"""

import asyncio
import os
import re
import resource
import shlex
import signal
import subprocess
import sys
from typing import List, Optional, Union

from capabilities.base import BaseTool

MAX_OUTPUT_CHARS = 100_000

_ANSI_RE = re.compile(r'\x1b\[[0-9;]*[a-zA-Z]|\x1b\]0;.*?\x07')

_ENV_WHITELIST = {
    'PATH', 'HOME', 'USER', 'LOGNAME',
    'LANG', 'LC_ALL', 'LC_CTYPE', 'LC_MESSAGES', 'LC_TIME',
    'TZ', 'TERM', 'SHELL',
    'PWD', 'OLDPWD',
    'TMPDIR', 'TMP', 'TEMP',
    'XDG_RUNTIME_DIR', 'XDG_CACHE_HOME',
    'VIRTUAL_ENV', 'CONDA_PREFIX',
    'NODE_PATH',
    'JAVA_HOME', 'GOPATH',
}

# P2-8: Resource limits
_CPU_LIMIT_SECONDS = 60
_MEMORY_LIMIT_MB = 256


class Sandbox(BaseTool):
    name = "bash"
    description = "Execute command in sandbox (security limits + timeout + output truncation)"
    parameters = {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "Command to execute"},
            "timeout": {"type": "number", "description": "Timeout seconds, default 60"},
        },
        "required": ["command"],
    }
    risk = "high"
    requires_approval = True

    async def execute(self, command, timeout: float = 60) -> str:
        timeout = min(timeout, 300)

        if isinstance(command, str):
            try:
                cmd_list = shlex.split(command)
            except ValueError as e:
                return f"[error] Command parse failed: {e}"
        else:
            cmd_list = list(command)

        if not cmd_list:
            return "[error] Empty command"

        filtered_env = self._build_filtered_env()

        popen_kwargs = {
            'args': cmd_list,
            'stdout': subprocess.PIPE,
            'stderr': subprocess.PIPE,
            'text': True,
            'env': filtered_env,
        }
        if sys.platform != 'win32':
            popen_kwargs['start_new_session'] = True
            popen_kwargs['preexec_fn'] = self._set_resource_limits

        try:
            proc = subprocess.Popen(**popen_kwargs)
        except FileNotFoundError:
            return f"[error] Command not found: {cmd_list[0]}"
        except PermissionError:
            return f"[error] Permission denied: {cmd_list[0]}"
        except OSError as e:
            return f"[error] Cannot execute: {e}"

        try:
            stdout, stderr = await asyncio.wait_for(
                asyncio.to_thread(proc.communicate),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            self._kill_process_tree(proc)
            return f"[error] Command timed out (>{timeout}s)"
        except Exception as e:
            self._kill_process_tree(proc)
            return f"[error] {type(e).__name__}: {e}"

        output = stdout or ""
        if stderr:
            output += f"\n[stderr]\n{stderr}"

        output = self._clean_ansi(output)
        output = output.strip() or f"(exit={proc.returncode})"

        if len(output) > MAX_OUTPUT_CHARS:
            output = output[:MAX_OUTPUT_CHARS] + "\n... [output truncated]"

        return output

    @staticmethod
    def _build_filtered_env() -> dict:
        filtered = {}
        for key in _ENV_WHITELIST:
            value = os.environ.get(key)
            if value is not None:
                filtered[key] = value
        return filtered

    @staticmethod
    def _kill_process_tree(proc) -> None:
        if sys.platform == 'win32':
            try:
                subprocess.run(
                    ['taskkill', '/F', '/T', '/PID', str(proc.pid)],
                    capture_output=True, timeout=10,
                )
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        else:
            try:
                pgid = os.getpgid(proc.pid)
                os.killpg(pgid, signal.SIGKILL)
            except (ProcessLookupError, OSError):
                pass
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                    proc.wait(timeout=5)
                except Exception:
                    pass

    @staticmethod
    def _clean_ansi(text: str) -> str:
        return _ANSI_RE.sub('', text)

    @staticmethod
    def _set_resource_limits() -> None:
        try:
            resource.setrlimit(resource.RLIMIT_CPU, (_CPU_LIMIT_SECONDS, _CPU_LIMIT_SECONDS))
        except (ValueError, OSError):
            pass
        try:
            mem_bytes = _MEMORY_LIMIT_MB * 1024 * 1024
            resource.setrlimit(resource.RLIMIT_AS, (mem_bytes, mem_bytes))
        except (ValueError, OSError):
            pass
