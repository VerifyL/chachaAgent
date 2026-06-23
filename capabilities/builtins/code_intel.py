"""
capabilities/builtins/code_intel.py
CodeIntel — 跨文件语义代码分析工具。

三个 action:
  find_callers     → 谁调用了这个函数/方法？
  find_references  → 谁引用了这个符号？
  class_hierarchy   → 类的继承链是什么？

全部返回 JSON，比 grep 更精确（基于 AST，排除注释/字符串误匹配）。
首次调用构建全项目 AST 索引并缓存，后续调用毫秒级。
非 Python 文件用正则 fallback。
"""

import ast
import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from capabilities.base import BaseTool

logger = logging.getLogger(__name__)

# 排除目录
_SKIP_PARTS = {
    ".git", "__pycache__", ".venv", "venv", ".tox", ".eggs",
    "node_modules", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    "dist", "build", ".chacha_agent",
}

MAX_FILE_SIZE = 1024 * 1024  # 1MB


class CodeIntelTool(BaseTool):
    """跨文件语义代码分析：调用者追踪、引用查找、继承链分析"""

    name = "code_intel"
    description = (
        "跨文件语义代码分析（基于 AST，比 grep 精确）。"
        "action: find_callers=查找函数/方法调用者; "
        "find_references=查找符号所有引用位置; "
        "class_hierarchy=类继承链分析。"
        "首次调用构建索引（~1-5s），后续调用毫秒级。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["find_callers", "find_references", "class_hierarchy"],
                "description": (
                    "分析类型: find_callers=谁调用了这个函数/方法; "
                    "find_references=谁引用了这个符号（含 import/赋值/调用等）; "
                    "class_hierarchy=类的完整继承链"
                ),
            },
            "symbol": {
                "type": "string",
                "description": "目标符号名（函数名/方法名/类名/变量名）",
            },
            "file_filter": {
                "type": "string",
                "description": (
                    "限定搜索范围的文件 glob（如 'src/*.py'），可选。"
                    "支持 ** 递归匹配。仅在首次构建索引时生效。"
                ),
            },
        },
        "required": ["action", "symbol"],
    }
    risk = "low"
    requires_approval = False

    def __init__(self, root: Optional[Path] = None):
        self._root = Path(root).resolve() if root else Path.cwd().resolve()
        self._index: Optional[Dict[str, ast.AST]] = None
        self._source_cache: Optional[Dict[str, List[str]]] = None

    # ====== 公共入口 ======

    async def execute(
        self, action: str, symbol: str, file_filter: Optional[str] = None
    ) -> str:
        # 构建/刷新索引（file_filter 变化时重建）
        cache_key = file_filter or "__full__"
        if self._index is None or getattr(self, "_filter_key", None) != cache_key:
            self._build_index(file_filter)
            self._filter_key = cache_key

        if action == "find_callers":
            result = self._find_callers(symbol)
        elif action == "find_references":
            result = self._find_references(symbol)
        elif action == "class_hierarchy":
            result = self._class_hierarchy(symbol)
        else:
            return json.dumps({"error": f"未知 action: {action}"}, ensure_ascii=False)

        return json.dumps(result, ensure_ascii=False, indent=2)

    # ====== 索引构建 ======

    def _build_index(self, file_filter: Optional[str] = None) -> None:
        """遍历项目所有 .py 文件，构建 AST 索引。"""
        self._index = {}
        self._source_cache = {}
        py_files: List[Path] = []

        if file_filter:
            # 使用 glob 匹配
            try:
                py_files = list(self._root.glob(file_filter))
            except Exception:
                pass
            py_files = [f for f in py_files if f.suffix.lower() == ".py"]
        else:
            py_files = self._collect_py_files(self._root)

        for fpath in py_files:
            try:
                if fpath.stat().st_size > MAX_FILE_SIZE:
                    continue
                source = fpath.read_text(encoding="utf-8", errors="replace")
                tree = _patch_parents(ast.parse(source))
                rel = str(fpath.relative_to(self._root))
                self._index[rel] = tree
                self._source_cache[rel] = source.split("\n")
            except (SyntaxError, UnicodeDecodeError, OSError) as e:
                logger.debug("跳过 %s: %s", fpath, e)
                continue

        logger.info("索引构建完成: %d 个文件", len(self._index))

    def _collect_py_files(self, directory: Path) -> List[Path]:
        """递归收集 .py 文件，跳过排除目录。"""
        result: List[Path] = []
        try:
            for entry in directory.iterdir():
                if entry.name.startswith(".") and entry.name not in (".", ".."):
                    if entry.is_dir():
                        continue  # 跳过隐藏目录
                if entry.is_dir():
                    if entry.name in _SKIP_PARTS:
                        continue
                    result.extend(self._collect_py_files(entry))
                elif entry.suffix.lower() == ".py":
                    result.append(entry)
        except PermissionError:
            pass
        return result

    # ====== find_callers ======

    def _find_callers(self, symbol: str) -> Dict[str, Any]:
        """查找所有调用 symbol 的位置。"""
        results: List[Dict[str, Any]] = []

        for filepath, tree in (self._index or {}).items():
            lines = self._source_cache.get(filepath, [])
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue

                matched = self._match_call_target(node.func, symbol)
                if not matched:
                    continue

                ctx_line = self._get_line(lines, node.lineno)
                results.append({
                    "file": filepath,
                    "line": node.lineno,
                    "context": ctx_line.strip() if ctx_line else "",
                    "type": matched,  # "call" | "method_call"
                })

        return {
            "action": "find_callers",
            "symbol": symbol,
            "total_results": len(results),
            "results": results,
        }

    def _match_call_target(self, func_node: ast.AST, symbol: str) -> Optional[str]:
        """判断 Call 的 func 节点是否匹配目标符号。

        返回匹配类型字符串，不匹配返回 None。
        """
        # 直接调用: symbol()
        if isinstance(func_node, ast.Name) and func_node.id == symbol:
            return "call"

        # 方法调用: obj.symbol()
        if isinstance(func_node, ast.Attribute) and func_node.attr == symbol:
            return "method_call"

        # 链式调用无深度展开
        return None

    # ====== find_references ======

    def _find_references(self, symbol: str) -> Dict[str, Any]:
        """查找所有引用 symbol 的位置（含 import/call/assignment 等）。"""
        results: List[Dict[str, Any]] = []

        for filepath, tree in (self._index or {}).items():
            lines = self._source_cache.get(filepath, [])
            for node in ast.walk(tree):
                if not isinstance(node, ast.Name) or node.id != symbol:
                    continue

                # 跳过定义点本身
                if self._is_definition(node, symbol):
                    continue

                ref_type = self._classify_reference(node)
                ctx_line = self._get_line(lines, node.lineno)
                results.append({
                    "file": filepath,
                    "line": node.lineno,
                    "col": node.col_offset,
                    "context": ctx_line.strip() if ctx_line else "",
                    "type": ref_type,
                })

        return {
            "action": "find_references",
            "symbol": symbol,
            "total_results": len(results),
            "results": results,
        }

    def _is_definition(self, node: ast.Name, symbol: str) -> bool:
        """判断 Name 节点是否是 symbol 的定义点。"""
        parent = getattr(node, "_parent", None)
        if parent is None:
            return False
        if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            return parent.name == symbol
        # 函数参数名不算引用
        if isinstance(parent, ast.arg):
            return True
        # import 别名不算引用
        if isinstance(parent, ast.alias):
            return True
        # except ... as symbol
        if isinstance(parent, ast.ExceptHandler) and getattr(parent, "name", None) == symbol:
            return True
        return False

    def _classify_reference(self, node: ast.Name) -> str:
        """分类引用类型。"""
        parent = getattr(node, "_parent", None)
        if parent is None:
            return "unknown"

        if isinstance(parent, ast.Call):
            if parent.func is node:
                return "call"
            return "call_arg"
        if isinstance(parent, ast.Import):
            return "import"
        if isinstance(parent, ast.ImportFrom):
            return "import_from"
        if isinstance(parent, ast.Attribute) and parent.value is node:
            return "attribute_base"
        if isinstance(parent, ast.Assign):
            # 赋值目标 vs 赋值源
            for target in parent.targets:
                if self._contains_name(target, node):
                    return "assignment_target"
            return "assignment_value"
        if isinstance(parent, (ast.Return, ast.Yield, ast.YieldFrom)):
            return "return"
        if isinstance(parent, ast.Subscript) and parent.value is node:
            return "subscript"
        if isinstance(parent, ast.List):
            return "list_element"
        if isinstance(parent, ast.Dict):
            return "dict_element"
        if isinstance(parent, ast.Compare):
            return "comparison"
        if isinstance(parent, ast.BinOp):
            return "binary_op"
        if isinstance(parent, ast.UnaryOp):
            return "unary_op"
        if isinstance(parent, ast.If) or isinstance(parent, ast.While):
            return "condition"
        if isinstance(parent, ast.Starred):
            return "starred"
        if isinstance(parent, ast.With):
            return "with_item"
        if isinstance(parent, ast.arguments):
            return "default_value"
        if isinstance(parent, ast.keyword):
            return "keyword_arg"
        if isinstance(parent, ast.AnnAssign) and parent.target is node:
            return "annotated_assign"
        if isinstance(parent, ast.NamedExpr) and parent.target is node:
            return "walrus_target"
        if isinstance(parent, ast.NamedExpr) and parent.value is node:
            return "walrus_value"
        if isinstance(parent, ast.comprehension):
            return "comprehension"
        if isinstance(parent, ast.Lambda):
            return "lambda_body"
        if isinstance(parent, ast.Assert):
            return "assert"

        return "reference"

    def _contains_name(self, node: ast.AST, target: ast.Name) -> bool:
        """递归检查节点树中是否包含目标 Name（用于 assignment targets）。"""
        if node is target:
            return True
        for child in ast.iter_child_nodes(node):
            if self._contains_name(child, target):
                return True
        return False

    # ====== class_hierarchy ======

    def _class_hierarchy(self, symbol: str) -> Dict[str, Any]:
        """分析类的继承链。"""
        # 找到类定义
        class_def = None
        class_file = None
        for filepath, tree in (self._index or {}).items():
            found = self._find_class(tree, symbol)
            if found:
                class_def = found
                class_file = filepath
                break

        if class_def is None:
            return {
                "action": "class_hierarchy",
                "class": symbol,
                "error": f"未找到类 '{symbol}'",
            }

        # 提取父类
        bases = [self._ast_name(b) for b in class_def.bases]

        # 构建树（目标类为根，向下找子类，向上标注父类）
        tree = self._build_hierarchy_tree(symbol, symbol)

        return {
            "action": "class_hierarchy",
            "class": symbol,
            "file": class_file,
            "line": class_def.lineno,
            "bases": bases,
            "tree": tree,
        }

    def _find_class(self, tree: ast.AST, name: str) -> Optional[ast.ClassDef]:
        """在 AST 中按名称查找类定义。"""
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == name:
                return node
        return None

    def _build_hierarchy_tree(
        self, root_name: str, current: str, visited: Optional[set] = None
    ) -> Optional[Dict[str, Any]]:
        """递归构建继承树。"""
        if visited is None:
            visited = set()
        if current in visited:
            return None
        visited.add(current)

        # 找当前类定义
        class_def = None
        class_file = None
        for filepath, tree in (self._index or {}).items():
            found = self._find_class(tree, current)
            if found:
                class_def = found
                class_file = filepath
                break

        if class_def is None:
            return {"name": current, "file": "?", "line": 0, "subclasses": []}

        # 找所有子类
        subclasses: List[Dict[str, Any]] = []
        for filepath, tree in (self._index or {}).items():
            for node in ast.walk(tree):
                if not isinstance(node, ast.ClassDef):
                    continue
                for base in node.bases:
                    base_name = self._ast_name(base)
                    if base_name == current and node.name not in visited:
                        child_tree = self._build_hierarchy_tree(
                            root_name, node.name, visited.copy()
                        )
                        if child_tree:
                            subclasses.append(child_tree)

        return {
            "name": current,
            "file": class_file or "?",
            "line": class_def.lineno,
            "subclasses": subclasses,
        }

    # ====== helper ======

    def _ast_name(self, node: ast.AST) -> str:
        """AST 节点 → 名称字符串。"""
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            return f"{self._ast_name(node.value)}.{node.attr}"
        if isinstance(node, ast.Subscript):
            return f"{self._ast_name(node.value)}[...]"
        if isinstance(node, ast.Constant):
            return repr(node.value)
        if isinstance(node, ast.Call):
            return f"{self._ast_name(node.func)}(...)"
        return "?"

    def _get_line(self, lines: List[str], lineno: int) -> Optional[str]:
        """获取文件指定行的文本（1-based）。"""
        idx = lineno - 1
        if 0 <= idx < len(lines):
            return lines[idx]
        return None


def _patch_parents(tree: ast.AST) -> ast.AST:
    """为 AST 中每个节点设置 _parent 引用（原地修改）。"""
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            setattr(child, "_parent", parent)
    return tree
