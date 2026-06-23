"""
capabilities/builtins/file_outline.py
FileOutline — 文件骨架提取工具。

读取文件并提取类/函数/接口签名（不读实现），
帮助 LLM 快速了解文件结构而不消耗大量 token。
"""

import ast
import logging
import os
import re
import tokenize
from pathlib import Path
from typing import Optional

from capabilities.base import BaseTool

logger = logging.getLogger(__name__)

MAX_FILE_SIZE = 1024 * 1024  # 1MB
_MAX_OUTPUT_LINES = 300


class FileOutlineTool(BaseTool):
    """提取文件骨架：file_outline(path) → 类/函数/接口签名"""

    name = "file_outline"
    description = (
        "提取文件的类、函数、接口签名（不读实现体）。"
        "用于快速了解文件结构后决定是否需要 read_file 深入。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "文件路径（相对于项目根目录或绝对路径）",
            },
        },
        "required": ["path"],
    }
    risk = "low"
    requires_approval = False

    def __init__(self, root: Optional[Path] = None):
        self._root = Path(root).resolve() if root else Path.cwd().resolve()

    async def execute(self, path: str) -> str:
        # 1. 路径解析 + containment
        raw = (Path(path).resolve() if Path(path).is_absolute()
               else (self._root / path).resolve())
        try:
            raw.relative_to(self._root)
        except ValueError:
            return "[错误] 访问被拒绝: 路径超出项目根目录"

        if not raw.exists():
            return f"[错误] 文件不存在: {path}"
        if not raw.is_file():
            return f"[错误] 不是文件: {path}"

        # 2. 大小检查
        try:
            fsize = raw.stat().st_size
        except OSError as e:
            return f"[错误] 无法访问文件: {e}"
        if fsize > MAX_FILE_SIZE:
            return "[错误] 文件过大，请用 read_file 手动查看"

        # 3. 按扩展名分发
        suffix = raw.suffix.lower()
        lang_map = {
            ".py": "python", ".go": "go", ".rs": "rust",
            ".java": "java", ".kt": "kotlin",
            ".ts": "typescript", ".tsx": "typescript",
            ".js": "javascript", ".jsx": "javascript",
            ".swift": "swift", ".rb": "ruby",
            ".php": "php", ".scala": "scala",
        }
        lang = lang_map.get(suffix)
        if lang:
            outline = _outline_regex(raw, suffix, lang)
            if outline:
                return outline
        if suffix in (".md", ".rst", ".txt"):
            return _outline_markup(raw)
        elif suffix in (".json", ".yaml", ".yml", ".toml", ".ini", ".cfg"):
            return _outline_config(raw)
        elif suffix in (".html", ".css"):
            return _outline_text(raw, f"{len(raw.read_bytes())} bytes")
        else:
            return _outline_text(raw, f"{fsize // 1024}KB")


def _outline_python(path: Path) -> str:
    """用 AST 解析 Python 文件骨架。"""
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return f"[错误] 读取失败: {e}"

    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        lines = source.split("\n")
        return f"[语法错误] {e}\n文件 {len(lines)} 行，请用 read_file 手动查看"

    lines = source.split("\n")
    total = len(lines)
    parts: list[str] = []

    # 模块文档
    doc = ast.get_docstring(tree)
    if doc:
        parts.append(f'"""\n{doc.strip()[:200]}"""')

    # 统计
    classes = [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]
    funcs = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    top_funcs = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]

    info = f"{path.name} | {total}行 | {len(classes)}个类 | {len(funcs)}个函数"
    parts.append(f"[文件] {info}")

    # 导入摘要
    imports = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            names = [alias.name for alias in node.names]
            imports.append(f"from {module} import {', '.join(names[:5])}")
            if len(node.names) > 5:
                imports[-1] += ", ..."
    if imports:
        parts.append(f"导入 ({len(imports)}): {imports[0]}" + (f" +{len(imports)-1}个" if len(imports) > 1 else ""))

    # 类定义（含方法签名）
    for cls in classes:
        cls_doc = ast.get_docstring(cls)
        methods = [n for n in cls.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
        props = [n for n in cls.body if isinstance(n, ast.Assign)]
        base_str = f"({', '.join(_node_name(b) for b in cls.bases)})" if cls.bases else ""
        line = f"\nclass {cls.name}{base_str}:  # L{cls.lineno}"
        if cls_doc:
            line += f" \"{cls_doc.strip()[:80]}\""
        line += f" [{len(methods)}方法, {len(props)}属性]"

        for m in methods[:15]:  # 最多显示 15 个方法
            sig = _sig_summary(m)
            m_doc = ast.get_docstring(m)
            line += f"\n  {sig}"
            if m_doc:
                line += f"  ← {m_doc.strip()[:60]}"
        if len(methods) > 15:
            line += f"\n  ... +{len(methods)-15}个方法"
        parts.append(line)

    # 顶层函数
    for fn in top_funcs:
        fn_doc = ast.get_docstring(fn)
        sig = _sig_summary(fn)
        line = f"\ndef {sig}  # L{fn.lineno}"
        if fn_doc:
            line += f" \"{fn_doc.strip()[:80]}\""
        parts.append(line)

    # 顶层模块级变量和常量
    assigns = [n for n in tree.body if isinstance(n, ast.Assign)]
    constants = []
    for a in assigns:
        for t in a.targets:
            if isinstance(t, ast.Name) and t.id.isupper():
                val = _const_val(a.value)
                constants.append(f"{t.id} = {val}")
    if constants:
        parts.append(f"\n常量 ({len(constants)}): {', '.join(constants[:10])}")
        if len(constants) > 10:
            parts[-1] += f" (+{len(constants)-10}个)"

    output = "\n".join(parts)

    # 行数控制
    if len(output.split("\n")) > _MAX_OUTPUT_LINES:
        output_lines = output.split("\n")
        output = "\n".join(output_lines[:_MAX_OUTPUT_LINES]) + f"\n... [截断，共 {len(output_lines)} 行]"

    return output


def _outline_markup(path: Path) -> str:
    """Markdown/文本文件：统计标题和长度。"""
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return f"[错误] 读取失败: {e}"
    lines = content.split("\n")
    headings = [l.strip() for l in lines if l.strip().startswith("#")]
    info = f"{path.name} | {len(lines)}行 | {len(content)}字符 | {len(headings)}个标题"
    parts = [f"[文件] {info}"]
    if headings:
        parts.append(f"标题 ({len(headings)}):")
        for h in headings[:20]:
            parts.append(f"  {h}")
        if len(headings) > 20:
            parts.append(f"  ... +{len(headings)-20}个")
    return "\n".join(parts)


def _outline_config(path: Path) -> str:
    """配置文件：只输出文件大小和节标题。"""
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return f"[错误] 读取失败: {e}"
    lines = content.split("\n")
    sections = [l.strip() for l in lines if l.strip().startswith("[")]
    info = f"{path.name} | {len(lines)}行 | {len(content)}字符 | {len(sections)}个节"
    parts = [f"[文件] {info}"]
    if sections:
        parts.append(f"节 ({len(sections)}):")
        for s in sections[:20]:
            parts.append(f"  {s}")
        if len(sections) > 20:
            parts[-1] += f" +{len(sections)-20}个"
    return "\n".join(parts)


def _outline_text(path: Path, size_str: str) -> str:
    """通用文本文件：只读前 20 行预览。"""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = [f.readline() for _ in range(20)]
    except Exception as e:
        return f"[错误] 读取失败: {e}"
    info = f"{path.name} | {size_str} | 预览前 20 行"
    parts = [f"[文件] {info}"]
    for i, line in enumerate(lines, 1):
        if line:
            parts.append(f"  {i}: {line.rstrip()[:100]}")
    return "\n".join(parts)


# ====== 多语言支持（正则级） ======

_LANG_PATTERNS = {
    "go": [
        (r"^func\s+(\w+)\s*\([^)]*\)", "func {1}"),
        (r"^type\s+(\w+)\s+struct", "struct {1}"),
        (r"^type\s+(\w+)\s+interface", "interface {1}"),
        (r"^func\s+\([^)]+\)\s+(\w+)\s*\([^)]*\)", "  func {1}"),
    ],
    "rust": [
        (r"^\s*fn\s+(\w+)\s*\([^)]*\)", "fn {1}"),
        (r"^\s*struct\s+(\w+)", "struct {1}"),
        (r"^\s*enum\s+(\w+)", "enum {1}"),
        (r"^\s*trait\s+(\w+)", "trait {1}"),
        (r"^\s*impl\s+(\w+)", "impl {1}"),
        (r"^\s*fn\s+(\w+)\s*\(&?self[^)]*\)", "  fn {1}"),
    ],
    "java": [
        (r"(public|private|protected)?\s*(static|abstract|final)?\s*(class|interface|enum)\s+(\w+)",
         r"\3 {4}"),
        (r"(public|private|protected)?\s*(static|final)?\s*\w+\s+(\w+)\s*\([^)]*\)",
         r"  {3}()"),
    ],
    "kotlin": [
        (r"^(class|interface|object|enum class)\s+(\w+)", r"{1} {2}"),
        (r"^fun\s+(\w+)\s*\([^)]*\)", "fun {1}"),
    ],
    "typescript": [
        (r"^(export\s+)?(class|interface|type|enum)\s+(\w+)", r"\2 {3}"),
        (r"^(export\s+)?(function|async function)\s+(\w+)", r"\2 {3}"),
        (r"^(export\s+)?(const)\s+(\w+)\s*[:=]\s*\(?[^)]*\)?\s*=>", r"\2 {3}"),
    ],
    "javascript": [
        (r"^(class)\s+(\w+)", r"{1} {2}"),
        (r"^(function|async function)\s+(\w+)", r"{1} {2}"),
        (r"^(const)\s+(\w+)\s*[:=]\s*\(?[^)]*\)?\s*=>", r"\1 {2}"),
        (r"^(module\.exports|export default)", "export"),
    ],
    "swift": [
        (r"^(class|struct|enum|protocol)\s+(\w+)", r"{1} {2}"),
        (r"^func\s+(\w+)\s*\([^)]*\)", "func {1}"),
    ],
    "ruby": [
        (r"^(class|module)\s+(\w+)", r"{1} {2}"),
        (r"^def\s+(\w+)", "def {1}"),
    ],
    "php": [
        (r"^(class|interface|trait|abstract class|final class)\s+(\w+)", r"{1} {2}"),
        (r"function\s+(\w+)\s*\(", "  function {1}"),
    ],
    "scala": [
        (r"^(class|object|trait|case class)\s+(\w+)", r"{1} {2}"),
        (r"^def\s+(\w+)\s*\([^)]*\)", "def {1}"),
    ],
}


def _outline_regex(path: Path, suffix: str, lang: str) -> Optional[str]:
    """用正则提取非 Python 文件的结构。"""
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None
    lines = content.split("\n")
    total = len(lines)
    patterns = _LANG_PATTERNS.get(lang)
    if not patterns:
        return None

    parts: list[str] = []
    parts.append(f"[文件] {path.name} | {total}行 | {lang}")
    seen = set()

    for lineno, line in enumerate(lines, 1):
        stripped = line.strip()
        if not stripped or stripped.startswith(("//", "#", "/*", "*", "///")):
            continue
        for regex, template in patterns:
            m = re.search(regex, stripped)
            if m:
                label = _apply_template(template, m)
                # 去重
                dedup_key = label.strip()
                if dedup_key not in seen:
                    seen.add(dedup_key)
                    indent = "  " if label.startswith("  ") else ""
                    parts.append(f"{indent}{label.strip()}  # L{lineno}")
                break

    return "\n".join(parts) if len(parts) > 1 else None


def _apply_template(template: str, m: re.Match) -> str:
    """将模板中的 {N} 替换为匹配组。"""
    result = template
    for i in range(1, 10):
        try:
            val = m.group(i)
        except (IndexError, ValueError):
            break
        if val:
            result = result.replace(f"{{{i}}}", val.strip())
    return result


# ====== 辅助 ======

def _node_name(node) -> str:
    """AST 节点 → 名称字符串。"""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_node_name(node.value)}.{node.attr}"
    if isinstance(node, ast.Subscript):
        return f"{_node_name(node.value)}[{_node_name(node.slice)}]"
    return "?"


def _sig_summary(node) -> str:
    """函数/方法 → 签名摘要。"""
    prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
    args = []
    for arg in node.args.args:
        arg_str = arg.arg
        if arg.annotation:
            arg_str += f": {_node_name(arg.annotation)}"
        args.append(arg_str)
    # *args
    if node.args.vararg:
        args.append(f"*{node.args.vararg.arg}")
    # **kwargs
    if node.args.kwarg:
        args.append(f"**{node.args.kwarg.arg}")

    sig = f"{prefix} {node.name}({', '.join(args)})"
    if node.returns:
        sig += f" -> {_node_name(node.returns)}"
    return sig


def _const_val(node) -> str:
    """常量赋值 → 值的摘要。"""
    if isinstance(node, ast.Constant):
        v = repr(node.value)
        return v[:40] + ("..." if len(v) > 40 else "")
    if isinstance(node, ast.List):
        return f"[{len(node.elts)} items]"
    if isinstance(node, ast.Dict):
        return f"{{{len(node.keys)} keys}}"
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        return _const_val(node.operand)
    return "?"
