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

# 默认宪法模板 — 无 CHACHA.md 时兜底
DEFAULT_RULE_TEMPLATE = """\
# ChachaAgent 默认宪法
# 安装时自动创建于 ~/.chacha/CHACHA.md，可在项目根覆盖为 {project}/CHACHA.md

## 代码规范
- 优先使用项目已有的代码风格和约定
- 新增代码保持与周围代码一致的缩进、命名、注释风格
- 函数和类必须添加文档说明
- 不要引入未使用的导入或变量

## 安全规则
- 永远不要执行未经确认的破坏性操作（rm -rf、DROP TABLE、force push 等）
- 不要在代码中硬编码密钥、密码或 Token
- 修改密码、权限、认证逻辑前必须告知用户
- 涉及网络请求时必须验证 URL 合法性
- 下载和执行第三方代码前必须告知用户

## 修改策略
- 优先使用精确编辑而非重写整个文件
- 每次修改范围尽量小，避免不相关改动
- 修改完成后列出变更清单
- 修改前先阅读并理解文件内容
- 保持向后兼容，除非用户明确要求

## 工具使用规则
- **行动与声明必须一致**：回复中说「已修复」「已修改」「已执行」时，同一轮必须有对应的 write/edit/bash 工具调用。
  绝不在未执行工具的情况下声称已完成操作。
- 工具返回结果前，不要声称操作已完成；工具失败时如实告知，绝不编造结果。
- 回复中总结已完成工作时，仅总结本轮实际执行的工具调用及结果，不要从上下文中「继承」前一轮的操作。

## 回复风格
- 回复简洁直接，避免冗长解释
- 优先展示代码和结果，再补充说明
- 不确定时主动询问，不要猜测
- 使用用户提问的语言回复

## 回复前检查
有重要偏好/决策/错误修复时调用 memory 记录，无则跳过。
记录格式: 📝 类型: 简述（如 user-preferences / project-decisions / errors-fixed / lessons-learned / project-progress）
"""


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

        if parts:
            return "\n".join(parts)
        return DEFAULT_RULE_TEMPLATE

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
