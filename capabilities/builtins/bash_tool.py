"""
bash_tool.py — BashTool: 执行 Shell 命令。

命令黑名单 + timeout + 项目根限制。
截断由 tool_executor 自动处理。
"""

import os
import re
import signal
import subprocess
from pathlib import Path
from typing import Optional

from capabilities.base import BaseTool
from capabilities.result import ToolResult


# ── 破坏性命令黑名单 ──
_BLOCKED_PATTERNS = [
    # 递归强制删除根目录
    r'\brm\s+.*-rf\s+/',
    r'\brm\s+.*-r\s+.*-f\s+/',
    # 权限提升
    r'\bsudo\b',
    # 世界可写根目录
    r'\bchmod\s+777\s+/',
    r'\bchmod\s+-R\s+777\s+/',
    # 格式化文件系统
    r'\bmkfs\.',
    # 直接写块设备
    r'\bdd\s+.*if=.*of=/dev/',
    # 重定向覆盖块设备
    r'>\s*/dev/sd[a-z]',
    # Fork 炸弹
    r':\(\)\s*\{.*:\|:&.*\};.*:',
    # 关机/重启
    r'\b(shutdown|reboot|halt|poweroff)\b',
    # 清空关键系统文件
    r'>\s*/etc/',
    r'>\s*/boot/',
]


class BashTool(BaseTool):
    """执行 Shell 命令。支持管道和重定向。"""

    name = "bash"
    description = "执行 Shell 命令。git/构建/测试等操作统一走 bash。"
    risk = "high"  # 可执行任意系统命令
    parameters = {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "要执行的 shell 命令",
            },
            "timeout": {
                "type": "integer",
                "description": "超时秒数，默认 60",
            },
            "workdir": {
                "type": "string",
                "description": "工作目录（相对项目根），默认为项目根",
            },
        },
        "required": ["command"],
    }

    async def execute(
        self,
        command: str,
        timeout: int = 60,
        workdir: str = "",
    ) -> ToolResult:
        timeout = int(timeout)
        # ── 1. 安全：黑名单检查 ──
        for pattern in _BLOCKED_PATTERNS:
            if re.search(pattern, command):
                return ToolResult(
                    status="error",
                    content="",
                    error=f"命令被安全策略拦截: {command}",
                    error_type="blocked",
                    data={"command": command, "matched_pattern": pattern},
                )

        # ── 2. 安全：工作目录必须在项目根内 ──
        cwd = self.project_root or Path.cwd()
        if workdir:
            cwd = (cwd / workdir).resolve()
            if self.project_root and not str(cwd).startswith(
                str(self.project_root.resolve())
            ):
                return ToolResult(
                    status="error",
                    content="",
                    error=f"工作目录越界: {workdir}",
                    error_type="path_out_of_bounds",
                    data={"workdir": workdir},
                )

        # ── 3. 执行（进程组方式，确保超时时清理所有子进程）──
        try:
            p = subprocess.Popen(
                command,
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cwd=str(cwd),
                preexec_fn=os.setsid,  # 新进程组
            )
            stdout, stderr_str = p.communicate(timeout=timeout)
            exit_code = p.returncode
        except subprocess.TimeoutExpired:
            # 超时：杀整个进程组，不留孤儿
            try:
                os.killpg(os.getpgid(p.pid), signal.SIGKILL)
            except (ProcessLookupError, OSError):
                pass
            try:
                stdout, stderr_str = p.communicate(timeout=1)
            except subprocess.TimeoutExpired:
                p.kill()
                stdout, stderr_str = p.communicate()
            return ToolResult(
                status="error",
                content=stdout if stdout else "",
                error=f"命令超时 ({timeout}s): {command}",
                error_type="timeout",
                data={
                    "command": command,
                    "timeout": timeout,
                    "workdir": str(cwd),
                },
            )
        except FileNotFoundError:
            return ToolResult(
                status="error",
                content="",
                error=f"命令未找到: {command}",
                error_type="command_not_found",
                data={"command": command},
            )

        # ── 4. 组装输出 ──
        output = stdout or ""
        if stderr_str:
            if output:
                output += "\n"
            output += stderr_str
        status = "success" if exit_code == 0 else "error"

        return ToolResult(
            status=status,
            content=output.rstrip() if output else "(无输出)",
            error=f"命令退出码 {exit_code}" if exit_code != 0 else None,
            error_type="exit_code_nonzero" if exit_code != 0 else None,
            data={
                "command": command,
                "exit_code": exit_code,
                "workdir": str(cwd),
            },
        )
