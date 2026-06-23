"""
capabilities/builtins/git_tools.py
Git Tools — 只读 Git 操作工具集。

提供三个只读工具（均为 low risk，无需审批）：
  - git_diff:   查看工作区 / 暂存区 / 某文件的 diff
  - git_log:    查看提交历史（支持文件过滤）
  - git_status: 详细 git status（含分支跟踪、untracked）

全部依赖 GitContextProvider 的 git 执行能力，异常容错降级。
"""

import logging
from pathlib import Path
from typing import Optional

from capabilities.base import BaseTool
from capabilities.builtins.git_context import GitContextProvider

logger = logging.getLogger(__name__)

_DEFAULT_MAX_DIFF_LINES = 150
_DEFAULT_MAX_LOG_LINES = 30


class GitDiffTool(BaseTool):
    """查看 git diff — 工作区/暂存区/指定文件"""

    name = "git_diff"
    description = (
        "查看 git diff 变更详情。默认显示工作区未暂存的变更（unstaged）。"
        "可指定 staged=true 查看已暂存变更，指定 path 过滤特定文件。"
        "指定 from_ref + to_ref 比较两个分支/commit/标签（如 main..feature）。"
        "指定 to_ref 单独比较工作区与某个 ref（如 HEAD~3）。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "限定文件/目录路径（相对于项目根目录），可选",
            },
            "staged": {
                "type": "boolean",
                "description": "是否查看已暂存变更（--staged），默认 false",
            },
            "from_ref": {
                "type": "string",
                "description": "起始 ref（分支名/commit hash/tag），与 to_ref 配合使用",
            },
            "to_ref": {
                "type": "string",
                "description": "目标 ref（分支名/commit hash/tag），与 from_ref 配合使用",
            },
        },
    }
    risk = "low"
    requires_approval = False

    def __init__(self, root: Optional[Path] = None):
        self._root = Path(root).resolve() if root else Path.cwd().resolve()
        self._provider = GitContextProvider(
            project_root=self._root,
            max_status_lines=_DEFAULT_MAX_DIFF_LINES,
        )

    async def execute(
        self,
        path: str = "",
        staged: bool = False,
        from_ref: str = "",
        to_ref: str = "",
    ) -> str:
        """执行 git diff / 系统 diff（非 git 仓库 fallback）。

        优先级：
          1. from_ref + to_ref → git diff a..b / diff -ur
          2. to_ref 单独 → git diff <ref>
          3. staged             → git diff --staged
          4. 默认              → git diff（工作区 vs HEAD）

        非 git 仓库：
          - from_ref + to_ref 都存在 → fallback 到系统 diff -ur（目录）/ diff -u（文件）
          - 其他模式返回 error（无 git 上下文无法比较）
        """
        import json
        import shlex
        import subprocess

        is_git = self._provider._is_git_repo()

        # === 非 git 仓库 fallback：仅支持 from_ref + to_ref ===
        if not is_git:
            if from_ref and to_ref:
                return self._system_diff(from_ref, to_ref, path)
            if path:
                return json.dumps({
                    "error": "not_a_git_repo",
                    "hint": "非 git 仓库下仅 from_ref + to_ref 模式可用（使用系统 diff）",
                }, ensure_ascii=False)
            return json.dumps({
                "error": "not_a_git_repo",
                "hint": "非 git 仓库下仅 from_ref + to_ref 模式可用（使用系统 diff）",
            }, ensure_ascii=False)

        # === git 仓库 ===
        cmd = ["git", "diff"]
        refs_label = None

        if from_ref and to_ref:
            cmd.append(f"{shlex.quote(from_ref)}..{shlex.quote(to_ref)}")
            staged = False  # ref 模式忽略 staged
            refs_label = f"{from_ref}..{to_ref}"
        elif to_ref:
            cmd.append(shlex.quote(to_ref))
            staged = False
            refs_label = to_ref
        elif staged:
            cmd.append("--staged")

        if path:
            cmd.append("--")
            cmd.append(path)

        try:
            result = subprocess.run(
                cmd,
                cwd=str(self._root),
                capture_output=True, text=True,
                timeout=_DEFAULT_MAX_DIFF_LINES / 10 + 3,
            )
            if result.returncode != 0:
                return json.dumps({
                    "error": "command_failed",
                    "stderr": result.stderr.strip(),
                }, ensure_ascii=False)
            output = result.stdout
            response_base: dict = {
                "tool": "git_diff",
            }
            if refs_label:
                response_base["from_ref"] = from_ref
                response_base["to_ref"] = to_ref
            else:
                response_base["staged"] = staged
            response_base["path"] = path or None

            if not output.strip():
                response_base["output"] = ""
                response_base["clean"] = True
                return json.dumps(response_base, ensure_ascii=False)
            lines = output.split("\n")
            response_base["total_lines"] = len(lines)
            if len(lines) > _DEFAULT_MAX_DIFF_LINES:
                response_base["output"] = "\n".join(lines[:_DEFAULT_MAX_DIFF_LINES])
                response_base["truncated"] = True
                response_base["shown_lines"] = _DEFAULT_MAX_DIFF_LINES
            else:
                response_base["output"] = output
                response_base["truncated"] = False
            return json.dumps(response_base, ensure_ascii=False)
        except subprocess.TimeoutExpired:
            return json.dumps({"error": "command_timeout"}, ensure_ascii=False)
        except Exception as e:
            logger.debug("git diff 执行失败: %s", e)
            return json.dumps({"error": "exception", "message": str(e)}, ensure_ascii=False)

    def _system_diff(self, from_ref: str, to_ref: str, path: str = "") -> str:
        """非 git 仓库 fallback：使用系统 diff 命令比较两个文件/目录。

        Args:
            from_ref: 旧文件/目录路径
            to_ref: 新文件/目录路径
            path: 如果在 from_ref/to_ref 的特定子路径下查找，可选
        """
        import json
        import subprocess
        from pathlib import Path

        def _resolve(ref: str) -> Path:
            p = Path(ref)
            if not p.is_absolute():
                p = self._root / p
            if path and p.is_dir():
                p = p / path
            return p.resolve()

        from_path = _resolve(from_ref)
        to_path = _resolve(to_ref)

        if not from_path.exists() and not to_path.exists():
            return json.dumps({
                "error": "paths_not_found",
                "from_ref": str(from_path),
                "to_ref": str(to_path),
            }, ensure_ascii=False)

        # 目录用 -ur，单文件用 -u
        is_dir_diff = from_path.is_dir() or to_path.is_dir()
        cmd = ["diff", "-ur" if is_dir_diff else "-u",
               str(from_path), str(to_path)]

        try:
            result = subprocess.run(
                cmd,
                capture_output=True, text=True,
                timeout=_DEFAULT_MAX_DIFF_LINES / 10 + 3,
            )
            # diff 返回码：0=无差异，1=有差异，>1=错误
            if result.returncode > 1:
                return json.dumps({
                    "error": "command_failed",
                    "stderr": result.stderr.strip(),
                    "from_ref": str(from_path),
                    "to_ref": str(to_path),
                }, ensure_ascii=False)
            output = result.stdout
            response: dict = {
                "tool": "git_diff",
                "backend": "system_diff",
                "from_ref": str(from_path),
                "to_ref": str(to_path),
                "clean": result.returncode == 0 and not output.strip(),
            }
            if not output.strip():
                response["output"] = ""
                return json.dumps(response, ensure_ascii=False)
            lines = output.split("\n")
            response["total_lines"] = len(lines)
            if len(lines) > _DEFAULT_MAX_DIFF_LINES:
                response["output"] = "\n".join(lines[:_DEFAULT_MAX_DIFF_LINES])
                response["truncated"] = True
                response["shown_lines"] = _DEFAULT_MAX_DIFF_LINES
            else:
                response["output"] = output
                response["truncated"] = False
            return json.dumps(response, ensure_ascii=False)
        except subprocess.TimeoutExpired:
            return json.dumps({"error": "command_timeout"}, ensure_ascii=False)
        except Exception as e:
            logger.debug("系统 diff 执行失败: %s", e)
            return json.dumps({"error": "exception", "message": str(e)}, ensure_ascii=False)


class GitLogTool(BaseTool):
    """查看 git 提交历史"""

    name = "git_log"
    description = (
        "查看 git 提交历史。默认显示最近 5 条 oneline 格式。"
        "可指定 n 调整数量、指定 path 过滤涉及某文件的提交。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "n": {
                "type": "integer",
                "description": "显示最近 N 条提交，默认 5",
            },
            "path": {
                "type": "string",
                "description": "限定文件/目录路径（相对于项目根目录），可选",
            },
            "oneline": {
                "type": "boolean",
                "description": "是否使用 oneline 格式（默认 true）",
            },
        },
    }
    risk = "low"
    requires_approval = False

    def __init__(self, root: Optional[Path] = None):
        self._root = Path(root).resolve() if root else Path.cwd().resolve()
        self._provider = GitContextProvider(project_root=self._root)

    async def execute(self, n: int = 5, path: str = "", oneline: bool = True) -> str:
        """执行 git log。"""
        import json
        import subprocess

        if not self._provider._is_git_repo():
            return json.dumps({"error": "not_a_git_repo"}, ensure_ascii=False)

        n = max(1, min(n, 50))  # 限制范围 1~50
        cmd = ["git", "log"]
        if oneline:
            cmd.append("--oneline")
        cmd.append(f"-{n}")
        if path:
            cmd.append("--")
            cmd.append(path)

        try:
            result = subprocess.run(
                cmd,
                cwd=str(self._root),
                capture_output=True, text=True,
                timeout=10,
            )
            if result.returncode != 0:
                return json.dumps({
                    "error": "command_failed",
                    "stderr": result.stderr.strip(),
                }, ensure_ascii=False)
            output = result.stdout
            if not output.strip():
                return json.dumps({
                    "tool": "git_log",
                    "n": n,
                    "commits": [],
                }, ensure_ascii=False)

            commit_list: list[dict] = []
            for line in output.strip().split("\n"):
                if oneline:
                    parts = line.split(" ", 1)
                    commit_list.append({
                        "hash": parts[0],
                        "message": parts[1] if len(parts) > 1 else "",
                    })
                else:
                    commit_list.append({"raw": line})

            lines = output.split("\n")
            response: dict = {
                "tool": "git_log",
                "n": n,
                "commits": commit_list,
            }
            if len(lines) > _DEFAULT_MAX_LOG_LINES:
                response["truncated"] = True
                response["total_commits"] = len(lines)
                response["commits"] = commit_list[:_DEFAULT_MAX_LOG_LINES]
            return json.dumps(response, ensure_ascii=False)
        except subprocess.TimeoutExpired:
            return json.dumps({"error": "command_timeout"}, ensure_ascii=False)
        except Exception as e:
            logger.debug("git log 执行失败: %s", e)
            return json.dumps({"error": "exception", "message": str(e)}, ensure_ascii=False)


class GitStatusTool(BaseTool):
    """查看详细 git 状态"""

    name = "git_status"
    description = (
        "查看详细 git 状态（含分支跟踪、工作区/暂存区变更列表）。"
        "Git Context 已自动注入简要状态，此工具用于查看完整详情。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "detailed": {
                "type": "boolean",
                "description": "是否显示详细 diff 统计（git status --verbose），默认 false",
            },
        },
    }
    risk = "low"
    requires_approval = False

    def __init__(self, root: Optional[Path] = None):
        self._root = Path(root).resolve() if root else Path.cwd().resolve()
        self._provider = GitContextProvider(project_root=self._root)

    async def execute(self, detailed: bool = False) -> str:
        """执行 git status（含分支信息）。"""
        import json
        import subprocess

        if not self._provider._is_git_repo():
            return json.dumps({"error": "not_a_git_repo"}, ensure_ascii=False)

        data: dict = {"tool": "git_status"}

        # 1. 分支
        branch_info = self._provider._get_branch()
        data["branch"] = branch_info or "unknown"

        # 2. status --short
        status = self._provider._get_status()
        if status is not None:
            if status.strip():
                data["working_tree"] = {
                    "clean": False,
                    "details": status.strip().split("\n"),
                }
            else:
                data["working_tree"] = {"clean": True, "details": []}
        else:
            data["working_tree"] = None

        # 3. 暂存区统计
        staged = self._provider._get_staged_stat()
        if staged is not None and staged.strip():
            data["staging_area"] = {
                "files_staged": len(staged.strip().split("\n")),
                "details": staged.strip().split("\n")[:10],
            }
        else:
            data["staging_area"] = None

        # 4. verbose
        if detailed:
            try:
                result = subprocess.run(
                    ["git", "status", "--branch", "--verbose"],
                    cwd=str(self._root),
                    capture_output=True, text=True,
                    timeout=10,
                )
                if result.returncode == 0:
                    detailed_output = result.stdout.strip()
                    detailed_lines = detailed_output.split("\n")
                    if len(detailed_lines) > 60:
                        data["verbose"] = {
                            "output": "\n".join(detailed_lines[:60]),
                            "truncated": True,
                            "total_lines": len(detailed_lines),
                        }
                    else:
                        data["verbose"] = {
                            "output": detailed_output,
                            "truncated": False,
                        }
                else:
                    data["verbose"] = {"error": result.stderr.strip()}
            except Exception as e:
                logger.debug("git status --verbose 失败: %s", e)
                data["verbose"] = {"error": str(e)}
        else:
            data["verbose"] = None

        return json.dumps(data, ensure_ascii=False)
