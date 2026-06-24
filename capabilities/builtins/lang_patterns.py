"""
capabilities/builtins/lang_patterns.py
共享的多语言符号匹配模式。

被 `chunk_streamer.py`（符号跳转）和 `file_outline.py`（骨架提取）共用。
"""

import re
from pathlib import Path
from typing import List, Tuple

# ====== 语言扩展名 → 语言名映射 ======

LANG_MAP = {
    ".py": "python", ".go": "go", ".rs": "rust",
    ".java": "java", ".kt": "kotlin",
    ".ts": "typescript", ".tsx": "typescript",
    ".js": "javascript", ".jsx": "javascript",
    ".swift": "swift", ".rb": "ruby",
    ".php": "php", ".scala": "scala",
}

# ====== 跨语言符号匹配模式 ======
# 每条: (regex, kind) — kind 用于说明匹配的符号类型

LANG_PATTERNS = {
    "go": [
        (r"^func\s+(\w+)\s*\([^)]*\)", "func"),
        (r"^type\s+(\w+)\s+struct", "struct"),
        (r"^type\s+(\w+)\s+interface", "interface"),
        (r"^func\s+\([^)]+\)\s+(\w+)\s*\([^)]*\)", "method"),
    ],
    "rust": [
        (r"^\s*fn\s+(\w+)\s*\([^)]*\)", "fn"),
        (r"^\s*struct\s+(\w+)", "struct"),
        (r"^\s*enum\s+(\w+)", "enum"),
        (r"^\s*trait\s+(\w+)", "trait"),
        (r"^\s*impl\s+(\w+)", "impl"),
        (r"^\s*fn\s+(\w+)\s*\(&?self[^)]*\)", "method"),
    ],
    "java": [
        (r"(public|private|protected)?\s*(static|abstract|final)?\s*(class|interface|enum)\s+(\w+)", "type"),
        (r"(public|private|protected)?\s*(static|final)?\s*\w+\s+(\w+)\s*\([^)]*\)", "method"),
    ],
    "kotlin": [
        (r"^(class|interface|object|enum class)\s+(\w+)", "type"),
        (r"^fun\s+(\w+)\s*\([^)]*\)", "fun"),
    ],
    "typescript": [
        (r"^(export\s+)?(class|interface|type|enum)\s+(\w+)", "type"),
        (r"^(export\s+)?(function|async function)\s+(\w+)", "func"),
        (r"^(export\s+)?(const)\s+(\w+)\s*[:=]\s*\(?[^)]*\)?\s*=>", "const"),
    ],
    "javascript": [
        (r"^(class)\s+(\w+)", "class"),
        (r"^(function|async function)\s+(\w+)", "func"),
        (r"^(const)\s+(\w+)\s*[:=]\s*\(?[^)]*\)?\s*=>", "const"),
    ],
    "swift": [
        (r"^(class|struct|enum|protocol)\s+(\w+)", "type"),
        (r"^func\s+(\w+)\s*\([^)]*\)", "func"),
    ],
    "ruby": [
        (r"^(class|module)\s+(\w+)", "type"),
        (r"^def\s+(\w+)", "def"),
    ],
    "php": [
        (r"^(class|interface|trait|abstract class|final class)\s+(\w+)", "type"),
        (r"function\s+(\w+)\s*\(", "func"),
    ],
    "scala": [
        (r"^(class|object|trait|case class)\s+(\w+)", "type"),
        (r"^def\s+(\w+)\s*\([^)]*\)", "def"),
    ],
}


def get_lang(suffix: str) -> str:
    """扩展名 → 语言名。"""
    return LANG_MAP.get(suffix.lower(), "")


def resolve_text(content: str, symbol: str) -> int:
    """纯文本搜索符号（适用于 Markdown/配置等）。"""
    lines = content.split("\n")
    for lineno, line in enumerate(lines, 1):
        if symbol in line:
            return lineno
    return 0


def resolve_regex(content: str, symbol: str, patterns: List[Tuple[str, str]]) -> int:
    """正则匹配符号行号。"""
    lines = content.split("\n")
    for lineno, line in enumerate(lines, 1):
        stripped = line.strip()
        if not stripped or stripped.startswith(("//", "#", "/*", "*", "///")):
            continue
        for regex, _kind in patterns:
            m = re.search(regex, stripped)
            if m:
                for g in m.groups():
                    if g == symbol:
                        return lineno
    return 0
