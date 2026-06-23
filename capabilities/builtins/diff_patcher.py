"""
capabilities/builtins/diff_patcher.py
ApplyPatchTool — 调系统 patch 应用 unified diff，支持多位置编辑。

与 Claude Code apply_patch 对齐：
- 接收 unified diff 字符串
- 调系统 patch -p1 应用
- 干跑模式先检查，无误再应用
- 自动 invalidate 受影响文件的 stream_reader 行索引
"""

import json
import logging
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from capabilities.base import BaseTool

logger = logging.getLogger(__name__)

MAX_DIFF_SIZE = 500_000  # 500KB diff 上限


class ApplyPatchTool(BaseTool):
    """应用 unified diff：apply_patch(diff) → 调用系统 patch"""

    name = "apply_patch"
    description = (
        "应用 unified diff 补丁到项目文件。"
        "支持多文件、多位置编辑，一次调用完成。"
        "diff 必须是 unified diff 格式（git diff / diff -u 输出）。"
        "自动干跑检查，无误后再应用。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "diff": {
                "type": "string",
                "description": "unified diff 格式补丁（git diff 输出风格）",
            },
            "dry_run": {
                "type": "boolean",
                "description": "仅检查是否可以干净应用，不做实际修改（默认 false）",
            },
        },
        "required": ["diff"],
    }
    risk = "medium"
    requires_approval = False

    def __init__(self, root: Optional[Path] = None):
        self._root = Path(root).resolve() if root else Path.cwd().resolve()

    async def execute(self, diff: str, dry_run: bool = False) -> str:
        if len(diff) > MAX_DIFF_SIZE:
            return json.dumps(
                {"error": "diff_too_large", "message": f"diff 过大 ({len(diff)} 字符，上限 {MAX_DIFF_SIZE})"},
                ensure_ascii=False,
            )

        if not diff.strip():
            return json.dumps(
                {"error": "empty_diff", "message": "diff 内容为空"},
                ensure_ascii=False,
            )

        # 写入临时文件
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                suffix=".patch",
                delete=False,
                encoding="utf-8",
            ) as tf:
                tf.write(diff)
                patch_file = tf.name
        except OSError as e:
            return json.dumps(
                {"error": "tempfile_error", "message": str(e)},
                ensure_ascii=False,
            )

        try:
            # --dry-run 先检查
            check_cmd = [
                "patch",
                "-p1",
                "--dry-run",
                "--force",
                "--ignore-whitespace",
                "-i", patch_file,
                "-d", str(self._root),
            ]
            check = subprocess.run(
                check_cmd,
                capture_output=True,
                text=True,
                timeout=30,
            )

            if check.returncode != 0:
                return json.dumps({
                    "error": "patch_check_failed",
                    "message": "补丁无法干净应用（已执行 --dry-run 检查）",
                    "stderr": check.stderr.strip()[-2000:],
                    "stdout": check.stdout.strip()[-2000:],
                }, ensure_ascii=False)

            if dry_run:
                return json.dumps({
                    "dry_run": True,
                    "message": "补丁可以干净应用 ✓",
                    "stdout": check.stdout.strip()[-2000:],
                }, ensure_ascii=False)

            # 实际应用
            apply_cmd = [
                "patch",
                "-p1",
                "--force",
                "--ignore-whitespace",
                "-i", patch_file,
                "-d", str(self._root),
            ]
            result = subprocess.run(
                apply_cmd,
                capture_output=True,
                text=True,
                timeout=30,
            )

            if result.returncode != 0:
                return json.dumps({
                    "error": "patch_apply_failed",
                    "message": "补丁应用失败",
                    "stderr": result.stderr.strip()[-2000:],
                    "stdout": result.stdout.strip()[-2000:],
                }, ensure_ascii=False)

            # invalidate 受影响文件的索引
            affected = _parse_affected_files(diff, self._root)

            return json.dumps({
                "applied": True,
                "message": "补丁已成功应用 ✓",
                "files_patched": affected,
                "stdout": result.stdout.strip()[-2000:],
            }, ensure_ascii=False)

        except subprocess.TimeoutExpired:
            return json.dumps(
                {"error": "timeout", "message": "patch 执行超时"},
                ensure_ascii=False,
            )
        finally:
            # 清理临时文件
            try:
                os.unlink(patch_file)
            except OSError:
                pass


def _parse_affected_files(diff: str, root: Path) -> list:
    """从 diff 头部提取受影响文件路径。"""
    files = []
    for line in diff.splitlines():
        if line.startswith("--- ") and not line.startswith("--- /dev/null"):
            f = line[4:].split("\t")[0].strip()
            if f:
                files.append(f)
        elif line.startswith("+++ ") and not line.startswith("+++ /dev/null"):
            f = line[4:].split("\t")[0].strip()
            if f and f not in files:
                files.append(f)

    # Invalidate stream_reader 索引
    try:
        from capabilities.builtins.stream_reader import _invalidate
        for f in files:
            _invalidate(str(root / f))
    except Exception:
        pass

    return files
