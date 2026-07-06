"""
core/context/static_rule_loader.py
StaticRuleLoader — 分层加载 CHACHA.md。

加载顺序（下层→上层，上层内容追加到下层之后）：
  1. ~/.chacha/CHACHA.md          用户级全局规则
  2. {project_root}/CHACHA.md     项目级规则
  3. {project_root}/{sub_dir}/CHACHA.md  子目录级规则

支持 @import 指令递归加载被引用的文件。

用法:
    loader = StaticRuleLoader(project_root="/path/to/project")
    rules = loader.load()               # 加载所有层
    rules = loader.load(sub_dir="src")  # 含子目录规则
"""

import logging
import os
import re
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

DEFAULT_HOME_RULE = Path.home() / ".chacha" / "CHACHA.md"
IMPORT_PATTERN = re.compile(r"@import\s+(.+)", re.IGNORECASE)
_LOADED: set[str] = set()  # 防止循环 import


class StaticRuleLoader:
    """分层 CHACHA.md 加载器"""

    def __init__(self, project_root: Optional[Path] = None):
        self._project_root = project_root or Path.cwd()

    # ====== 公开接口 ======

    def load(self, sub_dir: Optional[str] = None) -> str:
        """加载所有层规则。sub_dir=None 时只加载用户+项目级。"""
        parts: list[str] = []

        # 1. 用户级
        text = self._read(DEFAULT_HOME_RULE)
        if text:
            parts.append(text)

        # 2. 项目级
        text = self._read(self._project_root / "CHACHA.md")
        if text:
            parts.append(text)

        # 3. 子目录级
        if sub_dir:
            text = self._read(self._project_root / sub_dir / "CHACHA.md")
            if text:
                parts.append(text)

        return "\n".join(parts)

    def load_file(self, path: Path) -> Optional[str]:
        """加载单个 CHACHA.md 文件（含 @import 展开）。"""
        return self._read(path)

    # ====== 内部 ======

    def _read(self, path: Path) -> Optional[str]:
        """读取文件，展开 @import。返回 None 表示文件不存在。"""
        if not path.exists():
            logger.debug("规则文件不存在: %s", path)
            return None

        try:
            raw = path.read_text(encoding="utf-8")
        except Exception as e:
            logger.warning("读取规则文件失败: %s - %s", path, e)
            return None

        return self._expand_imports(raw, path.parent)

    def _expand_imports(self, text: str, base_dir: Path) -> str:
        """展开 @import 指令"""
        lines = text.split("\n")
        result: list[str] = []

        for line in lines:
            match = IMPORT_PATTERN.match(line.strip())
            if match:
                import_path = match.group(1).strip()
                # 支持相对路径和绝对路径
                if import_path.startswith("/") or import_path.startswith("~"):
                    resolved = Path(os.path.expanduser(import_path))
                else:
                    resolved = base_dir / import_path

                abs_path = str(resolved.resolve())
                if abs_path in _LOADED:
                    logger.warning("@import 循环引用: %s", import_path)
                    continue
                _LOADED.add(abs_path)

                imported = self._read(resolved)
                if imported:
                    result.append(imported)
                else:
                    logger.warning("@import 文件不存在: %s", import_path)
            else:
                result.append(line)

        return "\n".join(result)
