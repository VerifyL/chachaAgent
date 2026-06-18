"""
capabilities/sandbox.py
Sandbox — 安全命令执行工具（继承 BaseTool）。

用法:
    sandbox = Sandbox()
    result = await sandbox.execute(command="ls -la", timeout=30)

安全链: LLM 调用 → PolicyEngine 黑名单 → Hook → subprocess → 输出截断
"""

import asyncio
import re
import subprocess
from typing import Optional

from capabilities.base import BaseTool

MAX_OUTPUT_CHARS = 100_000

# ANSI 转义码清洗正则
_ANSI_RE = re.compile(r'\x1b\[[0-9;]*[a-zA-Z]|\x1b\]0;.*?\x07')


class Sandbox(BaseTool):
    """安全命令执行器"""

    name = "bash"
    description = "在沙箱中执行命令（安全限制 + 超时 + 输出截断）"
    parameters = {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "要执行的命令"},
            "timeout": {"type": "number", "description": "超时秒数，默认 60"},
        },
        "required": ["command"],
    }
    risk = "high"
    requires_approval = True

    async def execute(self, command: str, timeout: float = 60) -> str:
        """异步执行命令（subprocess 在线程池中运行）。"""
        timeout = min(timeout, 300)  # 硬上限 5 分钟

        try:
            proc = await asyncio.to_thread(
                subprocess.run,
                command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=timeout,
            )

            output = proc.stdout or ""
            if proc.stderr:
                output += f"\n[stderr]\n{proc.stderr}"

            # ANSI 清洗
            output = self._clean_ansi(output)

            output = output.strip() or f"(exit={proc.returncode})"

            if len(output) > MAX_OUTPUT_CHARS:
                output = output[:MAX_OUTPUT_CHARS] + "\n... [输出截断]"

            return output

        except subprocess.TimeoutExpired:
            return f"[错误] 命令超时（>{timeout}s）"
        except Exception as e:
            return f"[错误] {type(e).__name__}: {e}"

    @staticmethod
    def _clean_ansi(text: str) -> str:
        """清洗 ANSI 转义码（颜色/光标控制等）"""
        return _ANSI_RE.sub('', text)
