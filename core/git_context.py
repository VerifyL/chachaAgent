"""
core/git_context.py
Git Context Hook — 可插拔的 Git 感知模块。

注册为 PRE_CONTEXT_ASSEMBLY 钩子后，每轮 LLM 调用前自动注入 git 状态：
  - 当前分支
  - 工作区变更摘要 (git status --short)
  - 暂存区统计 (git diff --staged --stat)
  - 最近提交 (git log --oneline -3)

设计原则：
  1. 纯只读、零副作用 — 不修改仓库状态
  2. 性能极轻 — 单次采集 < 50ms，异常容错降级
  3. 可插拔 — 通过 HookOrchestrator.register() 一行注册/注销
  4. LLM 天然感知 — 注入为 CONTEXT 而非工具调用结果

注入到 LLM 上下文的效果（JSON 格式）:
    {
      "git_context": {
        "branch": "feature/git-aware (based on main)",
        "working_tree": {"files_changed": 2, "details": ["M core/context_manager.py", "?? core/git_context.py"]},
        "staging_area": {"files_staged": 1, "details": [...]},
        "recent_commits": [{"hash": "a1b2c3d", "message": "feat: add git context provider"}]
      }
    }
"""

import json
import logging
import subprocess
from pathlib import Path
from typing import Optional

from core.models.hook import HookContext, HookResult

logger = logging.getLogger(__name__)

# ========================= 配置 =========================

DEFAULT_RECENT_COMMITS = 3
DEFAULT_MAX_STATUS_LINES = 20
DEFAULT_TIMEOUT = 5.0  # 秒


class GitContextProvider:
    """Git 上下文采集器 — 纯数据层，不依赖 Hook 模型。

    可由 Hook 调用，也可独立使用（如 CLI / 测试）。
    """

    def __init__(
        self,
        project_root: Path,
        recent_commits: int = DEFAULT_RECENT_COMMITS,
        max_status_lines: int = DEFAULT_MAX_STATUS_LINES,
        timeout: float = DEFAULT_TIMEOUT,
    ):
        self._root = Path(project_root)
        self._recent_commits = recent_commits
        self._max_status_lines = max_status_lines
        self._timeout = timeout

    # ====== 公开接口 ======

    def refresh(self) -> Optional[str]:
        """采集完整 git 上下文（单次调用，所有子命令容错降级）。

        Returns:
            JSON 字符串；若不在 git 仓库则返回 None。
        """
        if not self._is_git_repo():
            return None

        data: dict = {"git_context": {}}

        # 1. 分支
        branch = self._get_branch()
        data["git_context"]["branch"] = branch or "unknown"

        # 2. 工作区状态
        status = self._get_status()
        if status is not None:
            if status.strip():
                lines = status.strip().split("\n")
                data["git_context"]["working_tree"] = {
                    "files_changed": len(lines),
                    "details": lines[:self._max_status_lines],
                }
                if len(lines) > self._max_status_lines:
                    data["git_context"]["working_tree"]["truncated"] = True
                    data["git_context"]["working_tree"]["total_lines"] = len(lines)
            else:
                data["git_context"]["working_tree"] = {
                    "files_changed": 0,
                    "details": [],
                }
        else:
            data["git_context"]["working_tree"] = None

        # 3. 暂存区
        staged = self._get_staged_stat()
        if staged is not None:
            staged_lines = staged.strip().split("\n") if staged.strip() else []
            data["git_context"]["staging_area"] = {
                "files_staged": len(staged_lines),
                "details": staged_lines[:5],
            }
        else:
            data["git_context"]["staging_area"] = None

        # 4. 最近提交
        commits = self._get_recent_commits()
        if commits is not None:
            commit_list: list[dict] = []
            for line in commits.strip().split("\n"):
                if line.strip():
                    parts = line.strip().split(" ", 1)
                    if len(parts) == 2:
                        commit_list.append({"hash": parts[0], "message": parts[1]})
                    else:
                        commit_list.append({"hash": parts[0], "message": ""})
            data["git_context"]["recent_commits"] = commit_list
        else:
            data["git_context"]["recent_commits"] = None

        return json.dumps(data, ensure_ascii=False)
    # ====== 内部子命令 ======

    def _is_git_repo(self) -> bool:
        """检测是否为 git 仓库（含子目录）。"""
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--git-dir"],
                cwd=str(self._root),
                capture_output=True, text=True,
                timeout=self._timeout,
            )
            return result.returncode == 0
        except Exception:
            return False

    def _get_branch(self) -> Optional[str]:
        """当前分支名（含 tracking 信息）。"""
        try:
            # 分支名
            result = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=str(self._root),
                capture_output=True, text=True,
                timeout=self._timeout,
            )
            if result.returncode != 0:
                return None
            branch = result.stdout.strip()

            # upstream tracking
            upstream_result = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "@{upstream}"],
                cwd=str(self._root),
                capture_output=True, text=True,
                timeout=self._timeout,
            )
            if upstream_result.returncode == 0 and upstream_result.stdout.strip():
                upstream = upstream_result.stdout.strip().split("/", 1)[-1]
                return f"{branch} (based on {upstream})"
            return branch
        except Exception as e:
            logger.debug("git branch 采集失败: %s", e)
            return None

    def _get_status(self) -> Optional[str]:
        """工作区状态 (git status --short)。"""
        try:
            result = subprocess.run(
                ["git", "status", "--short"],
                cwd=str(self._root),
                capture_output=True, text=True,
                timeout=self._timeout,
            )
            if result.returncode != 0:
                return None
            return result.stdout
        except Exception as e:
            logger.debug("git status 采集失败: %s", e)
            return None

    def _get_staged_stat(self) -> Optional[str]:
        """暂存区变更统计 (git diff --staged --stat)。"""
        try:
            result = subprocess.run(
                ["git", "diff", "--staged", "--stat"],
                cwd=str(self._root),
                capture_output=True, text=True,
                timeout=self._timeout,
            )
            if result.returncode != 0:
                return None
            return result.stdout
        except Exception as e:
            logger.debug("git staged stat 采集失败: %s", e)
            return None

    def _get_recent_commits(self) -> Optional[str]:
        """最近 N 条提交 (git log --oneline -N)。"""
        try:
            result = subprocess.run(
                ["git", "log", "--oneline", f"-{self._recent_commits}"],
                cwd=str(self._root),
                capture_output=True, text=True,
                timeout=self._timeout,
            )
            if result.returncode != 0:
                return None
            return result.stdout
        except Exception as e:
            logger.debug("git log 采集失败: %s", e)
            return None


# ========================= Hook 适配器 =========================

class GitContextHook:
    """PRE_CONTEXT_ASSEMBLY 钩子适配器。

    用法:
        hook = GitContextHook(project_root)
        orchestrator.register(
            "git-context", HookPoint.PRE_CONTEXT_ASSEMBLY,
            hook, priority=10,
        )
    """

    def __init__(
        self,
        project_root: Path,
        recent_commits: int = DEFAULT_RECENT_COMMITS,
        max_status_lines: int = DEFAULT_MAX_STATUS_LINES,
        timeout: float = DEFAULT_TIMEOUT,
    ):
        self._provider = GitContextProvider(
            project_root=project_root,
            recent_commits=recent_commits,
            max_status_lines=max_status_lines,
            timeout=timeout,
        )

    async def __call__(self, ctx: HookContext) -> HookResult:
        """Hook 入口 — 采集 git 状态并注入到上下文。"""
        try:
            text = self._provider.refresh()
            if text:
                return HookResult.continue_(additional_context=text)
            else:
                # 非 git 仓库，静默跳过
                return HookResult.continue_()
        except Exception as e:
            logger.warning("GitContextHook 执行失败: %s", e)
            return HookResult.continue_(
                message=f"Git context hook failed: {e}",
            )
