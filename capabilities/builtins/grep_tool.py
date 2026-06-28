"""grep 工具 — 正则文本搜索。rg 优先，Python re fallback。"""

import re
import shutil
import subprocess
from pathlib import Path

from capabilities.base import BaseTool
from capabilities.result import ToolResult


class GrepTool(BaseTool):
    """全局文本搜索工具 — rg 优先，Python re fallback。"""

    name = "grep"
    description = "在项目中搜索匹配正则模式的内容。返回 file:line:content 格式。"
    risk = "low"            # 只读搜索

    parameters = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "正则搜索模式"},
            "path": {"type": "string", "description": "搜索目录，默认项目根"},
            "glob": {"type": "string", "description": "文件过滤模式，如 *.py"},
        },
        "required": ["pattern"],
    }

    SKIP_DIRS = {".git", ".chacha_agent", "node_modules", "__pycache__", ".venv",
                 ".chacha", "build", "dist", ".mypy_cache", ".pytest_cache"}
    MAX_MATCHES = 500
    MAX_FILE_SIZE = 20 * 1024 * 1024  # 20MB
    TIMEOUT_RG = 30
    TIMEOUT_PY = 10

    # ── execute ──────────────────────────────────────────────

    async def execute(self, pattern: str, path: str = ".", glob: str = "*") -> ToolResult:
        # 1. 安全检查
        resolved = self._resolve(path)
        if not resolved.exists():
            return ToolResult(
                status="error", error=f"目录不存在: {path}",
                error_type="file_not_found", content="",
                data={"path": path}
            )

        # 2. 选择引擎
        rg_path = shutil.which("rg")
        if rg_path:
            return self._grep_rg(rg_path, pattern, resolved, glob)
        else:
            return self._grep_python(pattern, resolved, glob)

    # ── rg 引擎 ──────────────────────────────────────────────

    def _grep_rg(self, rg_path: str, pattern: str, search_dir: Path, glob: str) -> ToolResult:
        args = [
            rg_path,
            "--no-heading",
            "--line-number",
            "--glob", glob,
            "--max-count", str(self.MAX_MATCHES + 1),
            "--max-filesize", str(self.MAX_FILE_SIZE),
            "--trim",
            pattern,
            str(search_dir),
        ]

        try:
            p = subprocess.run(args, capture_output=True, text=True, timeout=self.TIMEOUT_RG)
        except subprocess.TimeoutExpired:
            return ToolResult(
                status="error", error="rg 超时 (30s)。请缩小搜索范围。",
                error_type="timeout", content="",
                data={"pattern": pattern, "engine": "rg"}
            )

        stdout = p.stdout.strip()
        stderr = p.stderr.strip()

        if p.returncode > 1:
            err = stderr.splitlines()[-1] if stderr else f"rg 错误 (code={p.returncode})"
            return ToolResult(
                status="error", error=err,
                error_type="internal_error", content="",
                data={"pattern": pattern, "stderr": stderr, "engine": "rg"}
            )

        if not stdout:
            return ToolResult(
                status="success",
                content="未找到匹配",
                data={"matches": 0, "files": 0, "pattern": pattern, "path": str(self._rel(search_dir)),
                      "glob": glob, "engine": "rg"}
            )

        lines = stdout.splitlines()
        truncated = len(lines) > self.MAX_MATCHES
        if truncated:
            lines = lines[:self.MAX_MATCHES]

        content = "\n".join(lines)
        if truncated:
            content += f"\n────────────────\n共 {len(lines)}+ 条匹配，仅显示前 {self.MAX_MATCHES} 条。请缩小范围。"

        files = sorted(set(l.split(":")[0] for l in lines))

        return ToolResult(
            status="success",
            content=content,
            data={
                "matches": len(lines),
                "files": len(files),
                "pattern": pattern,
                "path": str(self._rel(search_dir)),
                "glob": glob,
                "engine": "rg",
                "truncated_count": truncated,
            }
        )

    # ── Python re fallback ───────────────────────────────────

    def _grep_python(self, pattern: str, search_dir: Path, glob: str) -> ToolResult:
        try:
            regex = re.compile(pattern)
        except re.error as e:
            return ToolResult(
                status="error", error=f"正则语法错误: {e}",
                error_type="invalid_argument", content="",
                data={"pattern": pattern, "engine": "python"}
            )

        matches: list[str] = []
        files_matched: set[str] = set()

        try:
            for file_path in search_dir.rglob(glob):
                if self._should_skip(file_path):
                    continue
                if not file_path.is_file():
                    continue
                if file_path.stat().st_size > self.MAX_FILE_SIZE:
                    continue
                try:
                    text = file_path.read_text("utf-8")
                except (UnicodeDecodeError, PermissionError):
                    continue

                if b"\x00" in file_path.read_bytes()[:512]:
                    continue

                for line_no, line in enumerate(text.splitlines(), 1):
                    if regex.search(line):
                        rel = str(self._rel(file_path))
                        matches.append(f"{rel}:{line_no}:{line}")
                        files_matched.add(rel)
                        if len(matches) >= self.MAX_MATCHES + 1:
                            break
                if len(matches) >= self.MAX_MATCHES + 1:
                    break
        except (OSError, PermissionError) as e:
            return ToolResult(
                status="error", error=str(e),
                error_type="internal_error", content="",
                data={"pattern": pattern, "engine": "python"}
            )

        if not matches:
            return ToolResult(
                status="success",
                content="未找到匹配",
                data={"matches": 0, "files": 0, "pattern": pattern, "path": str(self._rel(search_dir)),
                      "glob": glob, "engine": "python"}
            )

        truncated = len(matches) > self.MAX_MATCHES
        if truncated:
            matches = matches[:self.MAX_MATCHES]

        content = "\n".join(matches)
        if truncated:
            content += f"\n────────────────\n共 {len(matches)}+ 条匹配，仅显示前 {self.MAX_MATCHES} 条。请缩小范围。"

        return ToolResult(
            status="success",
            content=content,
            data={
                "matches": len(matches),
                "files": len(files_matched),
                "pattern": pattern,
                "path": str(self._rel(search_dir)),
                "glob": glob,
                "engine": "python",
                "truncated_count": truncated,
            }
        )

    # ── helpers ──────────────────────────────────────────────

    def _resolve(self, path: str) -> Path:
        raw = Path(path)
        if raw.is_absolute():
            resolved = raw.resolve()
        else:
            resolved = (self.project_root / raw).resolve()
        if not resolved.is_relative_to(self.project_root.resolve()):
            raise ValueError(f"路径越界: {path}")
        return resolved

    def _rel(self, full: Path) -> Path:
        return full.resolve().relative_to(self.project_root.resolve())

    def _should_skip(self, p: Path) -> bool:
        parts = set(p.parts)
        return bool(parts & self.SKIP_DIRS)
