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
    """跨文件语义代码分析：调用者追踪、引用查找、继承链分析、语义模式搜索"""

    name = "code_intel"
    description = (
        "跨文件语义代码分析（基于 AST，比 grep 精确）。"
        "action: find_callers=查找函数/方法调用者; "
        "find_references=查找符号所有引用位置; "
        "class_hierarchy=类继承链分析; "
        "find_patterns=按语义模式搜索（REST端点/DB查询/并发/异常处理/配置/自定义）。"
        "首次调用构建索引（~1-5s），后续调用毫秒级。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    "find_callers", "find_references", "class_hierarchy",
                    "find_patterns",
                ],
                "description": (
                    "分析类型: find_callers=谁调用了这个函数/方法; "
                    "find_references=谁引用了这个符号（含 import/赋值/调用等）; "
                    "class_hierarchy=类的完整继承链; "
                    "find_patterns=按语义模式搜索代码"
                ),
            },
            "symbol": {
                "type": "string",
                "description": (
                    "目标符号名。find_callers/find_references/class_hierarchy 必填; "
                    "find_patterns 可选（限定符号范围）"
                ),
            },
            "pattern": {
                "type": "string",
                "enum": [
                    "rest_endpoints", "db_queries", "concurrency",
                    "exception_handlers", "configuration", "custom",
                ],
                "description": (
                    "语义模式（find_patterns 必填）: "
                    "rest_endpoints=FastAPI/Flask/Django路由; "
                    "db_queries=SQLAlchemy/DjangoORM/raw SQL; "
                    "concurrency=threading/asyncio/multiprocessing; "
                    "exception_handlers=try/except + 异常装饰器; "
                    "configuration=os.environ/settings/config; "
                    "custom=使用 custom_pattern 自定义"
                ),
            },
            "custom_pattern": {
                "type": "object",
                "properties": {
                    "type": {
                        "type": "string",
                        "enum": ["decorator", "call", "import", "class_extend"],
                        "description": "匹配类型: decorator/call/import/class_extend",
                    },
                    "match": {
                        "type": "string",
                        "description": "正则表达式匹配目标（如 '@router\\.(get|post)'）",
                    },
                },
                "description": "自定义模式（pattern=custom 时必填）",
            },
            "file_filter": {
                "type": "string",
                "description": (
                    "限定搜索范围的文件 glob（如 'src/*.py'），可选。"
                    "支持 ** 递归匹配。仅在首次构建索引时生效。"
                ),
            },
        },
        "required": ["action"],
    }
    risk = "low"
    requires_approval = False

    def __init__(self, root: Optional[Path] = None):
        self._root = Path(root).resolve() if root else Path.cwd().resolve()
        self._index: Optional[Dict[str, ast.AST]] = None
        self._source_cache: Optional[Dict[str, List[str]]] = None

    # ====== 公共入口 ======

    async def execute(
        self, action: str, symbol: Optional[str] = None,
        pattern: Optional[str] = None,
        custom_pattern: Optional[Dict[str, str]] = None,
        file_filter: Optional[str] = None,
    ) -> str:
        # 构建/刷新索引（file_filter 变化时重建）
        cache_key = file_filter or "__full__"
        if self._index is None or getattr(self, "_filter_key", None) != cache_key:
            self._build_index(file_filter)
            self._filter_key = cache_key

        if action == "find_callers":
            if not symbol:
                return json.dumps(
                    {"error": "find_callers 需要 symbol 参数"}, ensure_ascii=False
                )
            result = self._find_callers(symbol)
        elif action == "find_references":
            if not symbol:
                return json.dumps(
                    {"error": "find_references 需要 symbol 参数"}, ensure_ascii=False
                )
            result = self._find_references(symbol)
        elif action == "class_hierarchy":
            if not symbol:
                return json.dumps(
                    {"error": "class_hierarchy 需要 symbol 参数"}, ensure_ascii=False
                )
            result = self._class_hierarchy(symbol)
        elif action == "find_patterns":
            result = self._find_patterns(pattern, custom_pattern, symbol)
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

    # ====== find_patterns ======

    # -- HTTP 方法名集合 --
    _HTTP_METHODS = frozenset({
        "get", "post", "put", "delete", "patch", "head", "options", "trace",
    })

    # -- DB 查询方法名 --
    _DB_METHODS = frozenset({
        "execute", "query", "add", "add_all", "commit", "rollback", "flush",
        "merge", "refresh", "bulk_save_objects", "scalars", "scalar",
    })
    _DJANGO_ORM_METHODS = frozenset({
        "filter", "get", "create", "all", "update", "delete", "exclude",
        "values", "values_list", "select_related", "prefetch_related",
        "annotate", "aggregate", "count", "exists", "first", "last",
        "latest", "earliest", "bulk_create", "bulk_update", "in_bulk",
    })

    # -- 并发相关 (模块, 类名) --
    _CONCURRENCY_PATTERNS = [
        # (模块路径片段, 类型, [类/函数名])
        ("threading", "call", {"Thread", "Lock", "RLock", "Condition", "Event",
                               "Semaphore", "BoundedSemaphore", "Timer", "Barrier"}),
        ("asyncio", "call", {"Lock", "Semaphore", "BoundedSemaphore", "Event",
                             "Condition", "Queue", "PriorityQueue", "LifoQueue"}),
        ("asyncio", "import", set()),
        ("concurrent.futures", "call", {"ThreadPoolExecutor", "ProcessPoolExecutor",
                                        "Future", "wait", "as_completed"}),
        ("multiprocessing", "call", {"Process", "Pool", "Queue", "JoinableQueue",
                                     "SimpleQueue", "Lock", "RLock", "Event",
                                     "Condition", "Semaphore", "BoundedSemaphore",
                                     "Pipe", "Value", "Array", "Manager"}),
        ("subprocess", "call", {"Popen", "run", "call", "check_call", "check_output"}),
    ]

    def _find_patterns(
        self, pattern: Optional[str], custom_pattern: Optional[Dict[str, str]],
        symbol: Optional[str],
    ) -> Dict[str, Any]:
        """find_patterns 调度器。"""
        if not pattern and not custom_pattern:
            return {"error": "find_patterns 需要 pattern 或 custom_pattern 参数"}
        if not pattern:
            pattern = "custom"

        if pattern == "rest_endpoints":
            result = self._pattern_rest_endpoints(symbol)
        elif pattern == "db_queries":
            result = self._pattern_db_queries(symbol)
        elif pattern == "concurrency":
            result = self._pattern_concurrency(symbol)
        elif pattern == "exception_handlers":
            result = self._pattern_exception_handlers(symbol)
        elif pattern == "configuration":
            result = self._pattern_configuration(symbol)
        elif pattern == "custom":
            result = self._pattern_custom(custom_pattern, symbol)
        else:
            return {"error": f"未知 pattern: {pattern}"}

        result["action"] = "find_patterns"
        result["pattern"] = pattern
        return result

    # ---- REST endpoints ----

    def _pattern_rest_endpoints(self, symbol: Optional[str]) -> Dict[str, Any]:
        """查找 REST API endpoint 定义 (FastAPI / Flask / Django)。"""
        results: List[Dict[str, Any]] = []

        for filepath, tree in (self._index or {}).items():
            lines = self._source_cache.get(filepath, [])
            # ---- FastAPI / Starlette 装饰器: @app.get, @router.post 等 ----
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if symbol and node.name != symbol:
                    continue

                for dec in node.decorator_list:
                    info = self._parse_http_decorator(dec, node, lines, filepath)
                    if info:
                        results.append(info)

            # ---- Django: path() / url() / re_path() 调用 ----
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                if isinstance(func, ast.Name) and func.id in ("path", "url", "re_path"):
                    route = self._extract_first_str_arg(node)
                    view_name = self._extract_view_name(node)
                    if route:
                        results.append({
                            "file": filepath,
                            "line": node.lineno,
                            "type": "django_url",
                            "route": route,
                            "view": view_name,
                            "context": self._get_or_empty(lines, node.lineno),
                        })

        return {"total_results": len(results), "results": results}

    def _parse_http_decorator(
        self, dec: ast.AST, func_node: ast.AST,
        lines: List[str], filepath: str,
    ) -> Optional[Dict[str, Any]]:
        """解析 HTTP 装饰器: @app.get('/path'), @router.post('/path'), @app.route('/path')"""
        if not isinstance(dec, ast.Call):
            return None

        # Flask: @app.route("/path", methods=["GET"])
        if (isinstance(dec.func, ast.Attribute)
                and dec.func.attr == "route"
                and isinstance(dec.func.value, ast.Name)):
            route = self._extract_first_str_arg(dec)
            method = self._extract_flask_methods(dec)
            return {
                "file": filepath,
                "line": dec.lineno,
                "function": func_node.name,
                "type": "flask_route",
                "http_method": method,
                "route": route,
                "context": self._get_or_empty(lines, dec.lineno),
            }

        # FastAPI/Starlette: @app.get, @router.post 等
        if (isinstance(dec.func, ast.Attribute)
                and dec.func.attr in self._HTTP_METHODS
                and isinstance(dec.func.value, ast.Name)):
            route = self._extract_first_str_arg(dec)
            http_method = dec.func.attr.upper()
            return {
                "file": filepath,
                "line": dec.lineno,
                "function": func_node.name,
                "type": "fastapi_route",
                "http_method": http_method,
                "route": route,
                "context": self._get_or_empty(lines, dec.lineno),
            }

        return None

    def _extract_first_str_arg(self, call_node: ast.Call) -> Optional[str]:
        """提取 Call 节点的第一个字符串参数。"""
        if call_node.args:
            first = call_node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                return first.value
            if isinstance(first, ast.JoinedStr):
                # f-string: 提取文字部分
                parts = []
                for p in first.values:
                    if isinstance(p, ast.Constant) and isinstance(p.value, str):
                        parts.append(p.value)
                if parts:
                    return "".join(parts)
        return None

    def _extract_flask_methods(self, call_node: ast.Call) -> str:
        """提取 Flask route 的 methods 关键字参数。"""
        for kw in call_node.keywords:
            if kw.arg == "methods" and isinstance(kw.value, ast.List):
                methods = []
                for elt in kw.value.elts:
                    if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                        methods.append(elt.value.upper())
                return ",".join(methods) if methods else "GET"
        return "GET"

    def _extract_view_name(self, call_node: ast.Call) -> str:
        """提取 Django path() 的 view 参数名。"""
        if len(call_node.args) >= 2:
            view_arg = call_node.args[1]
            if isinstance(view_arg, ast.Name):
                return view_arg.id
            if isinstance(view_arg, ast.Attribute):
                return self._ast_name(view_arg)
        return "?"

    # ---- DB queries ----

    def _pattern_db_queries(self, symbol: Optional[str]) -> Dict[str, Any]:
        """查找数据库查询: SQLAlchemy / Django ORM / raw SQL。"""
        results: List[Dict[str, Any]] = []

        for filepath, tree in (self._index or {}).items():
            lines = self._source_cache.get(filepath, [])
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                if symbol:
                    # 限定 symbol: 检查 Call 是否在目标函数/方法内
                    if not self._call_in_symbol(node, symbol):
                        continue

                info = self._classify_db_call(node, lines, filepath)
                if info:
                    results.append(info)

        return {"total_results": len(results), "results": results}

    def _classify_db_call(
        self, call: ast.Call, lines: List[str], filepath: str,
    ) -> Optional[Dict[str, Any]]:
        """分类数据库调用。"""
        func = call.func
        if not isinstance(func, ast.Attribute):
            return None

        # SQLAlchemy: session.execute(), session.query() 等
        if func.attr in self._DB_METHODS and isinstance(func.value, ast.Name):
            return {
                "file": filepath,
                "line": call.lineno,
                "type": "sqlalchemy",
                "method": func.attr,
                "target": func.value.id,
                "context": self._get_or_empty(lines, call.lineno),
            }

        # Django ORM: Model.objects.filter() / .get() / .create() 等
        if func.attr in self._DJANGO_ORM_METHODS:
            if isinstance(func.value, ast.Attribute) and func.value.attr == "objects":
                model_node = func.value.value
                model_name = (
                    model_node.id if isinstance(model_node, ast.Name)
                    else self._ast_name(model_node)
                )
                return {
                    "file": filepath,
                    "line": call.lineno,
                    "type": "django_orm",
                    "method": func.attr,
                    "model": model_name,
                    "context": self._get_or_empty(lines, call.lineno),
                }

        # Raw cursor: cursor.execute(), conn.execute()
        if func.attr == "execute":
            if isinstance(func.value, ast.Name) and func.value.id in (
                "cursor", "conn", "connection", "cur",
            ):
                sql = self._extract_first_str_arg(call)
                return {
                    "file": filepath,
                    "line": call.lineno,
                    "type": "raw_sql",
                    "method": "execute",
                    "sql_preview": sql[:80] if sql else "?",
                    "context": self._get_or_empty(lines, call.lineno),
                }

        return None

    def _call_in_symbol(self, call_node: ast.Call, symbol: str) -> bool:
        """检查 Call 节点是否在指定 symbol（函数/方法）内。"""
        parent = getattr(call_node, "_parent", None)
        while parent is not None:
            if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return parent.name == symbol
            parent = getattr(parent, "_parent", None)
        return False

    # ---- concurrency ----

    def _pattern_concurrency(self, symbol: Optional[str]) -> Dict[str, Any]:
        """查找并发/锁相关代码。"""
        results: List[Dict[str, Any]] = []

        for filepath, tree in (self._index or {}).items():
            lines = self._source_cache.get(filepath, [])

            for node in ast.walk(tree):
                # 导入: import threading / from threading import Lock
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    info = self._classify_concurrency_import(node, filepath, lines)
                    if info:
                        results.append(info)
                        continue

                # with 语句: with lock: / with ThreadPoolExecutor() as executor:
                if isinstance(node, ast.With):
                    info = self._classify_concurrency_with(node, filepath, lines)
                    if info:
                        results.append(info)

                # 调用: threading.Lock() / asyncio.create_task() 等
                if isinstance(node, ast.Call):
                    info = self._classify_concurrency_call(node, filepath, lines)
                    if info:
                        results.append(info)

            if symbol:
                results = [r for r in results if r.get("function") == symbol]

        return {"total_results": len(results), "results": results}

    def _classify_concurrency_import(
        self, node: ast.AST, filepath: str, lines: List[str],
    ) -> Optional[Dict[str, Any]]:
        """分类并发相关导入。"""
        if isinstance(node, ast.Import):
            for alias in node.names:
                name = alias.name
                for mod_prefix, ptype, _ in self._CONCURRENCY_PATTERNS:
                    if name == mod_prefix or name.startswith(mod_prefix + "."):
                        return {
                            "file": filepath, "line": node.lineno,
                            "type": "concurrency_import",
                            "module": name,
                            "context": self._get_or_empty(lines, node.lineno),
                        }
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                for mod_prefix, ptype, _ in self._CONCURRENCY_PATTERNS:
                    if node.module == mod_prefix or node.module.startswith(mod_prefix + "."):
                        names = [a.name for a in node.names]
                        return {
                            "file": filepath, "line": node.lineno,
                            "type": "concurrency_import_from",
                            "module": node.module,
                            "imports": names,
                            "context": self._get_or_empty(lines, node.lineno),
                        }
        return None

    def _classify_concurrency_with(
        self, node: ast.With, filepath: str, lines: List[str],
    ) -> Optional[Dict[str, Any]]:
        """分类并发相关 with 语句。"""
        for item in node.items:
            ctx_expr = item.context_expr
            name = self._ast_name(ctx_expr)
            for mod_prefix, ptype, names in self._CONCURRENCY_PATTERNS:
                for n in names:
                    if n in name:
                        return {
                            "file": filepath, "line": node.lineno,
                            "type": "concurrency_with",
                            "detail": name,
                            "context": self._get_or_empty(lines, node.lineno),
                        }
        return None

    def _classify_concurrency_call(
        self, node: ast.Call, filepath: str, lines: List[str],
    ) -> Optional[Dict[str, Any]]:
        """分类并发相关调用。"""
        func = node.func
        name = self._ast_name(func)

        # asyncio.create_task(...) / asyncio.gather(...) / asyncio.run(...)
        if name.startswith("asyncio."):
            task_methods = {"create_task", "gather", "run", "wait", "wait_for",
                           "as_completed", "to_thread", "shield", "timeout"}
            method = name.split(".", 1)[-1]
            if method in task_methods:
                return {
                    "file": filepath, "line": node.lineno,
                    "type": "concurrency_call",
                    "detail": name,
                    "context": self._get_or_empty(lines, node.lineno),
                }

        for mod_prefix, ptype, names in self._CONCURRENCY_PATTERNS:
            if ptype != "call":
                continue
            for n in names:
                if name.endswith("." + n) or name == n:
                    return {
                        "file": filepath, "line": node.lineno,
                        "type": "concurrency_call",
                        "detail": name,
                        "context": self._get_or_empty(lines, node.lineno),
                    }
        return None

    # ---- exception_handlers ----

    def _pattern_exception_handlers(self, symbol: Optional[str]) -> Dict[str, Any]:
        """查找异常处理代码: try/except + 异常装饰器。"""
        results: List[Dict[str, Any]] = []

        for filepath, tree in (self._index or {}).items():
            lines = self._source_cache.get(filepath, [])

            for node in ast.walk(tree):
                if isinstance(node, ast.Try):
                    for handler in node.handlers:
                        exc_type = (
                            self._ast_name(handler.type) if handler.type else "bare"
                        )
                        exc_name = handler.name or ""
                        results.append({
                            "file": filepath,
                            "line": node.lineno,
                            "type": "try_except",
                            "exception": exc_type,
                            "as_name": exc_name,
                            "context": self._get_or_empty(lines, node.lineno),
                        })

                # FastAPI/Starlette: @app.exception_handler(SomeException)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    for dec in node.decorator_list:
                        if isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute):
                            if dec.func.attr == "exception_handler":
                                exc = (self._ast_name(dec.args[0])
                                       if dec.args else "?")
                                results.append({
                                    "file": filepath,
                                    "line": dec.lineno,
                                    "type": "exception_decorator",
                                    "function": node.name,
                                    "exception": exc,
                                    "context": self._get_or_empty(lines, dec.lineno),
                                })

        if symbol:
            results = [r for r in results if r.get("function") == symbol]

        return {"total_results": len(results), "results": results}

    # ---- configuration ----

    def _pattern_configuration(self, symbol: Optional[str]) -> Dict[str, Any]:
        """查找配置/环境变量读取: os.environ / os.getenv / settings / config。"""
        results: List[Dict[str, Any]] = []

        for filepath, tree in (self._index or {}).items():
            lines = self._source_cache.get(filepath, [])

            for node in ast.walk(tree):
                # os.environ["KEY"] / os.environ.get("KEY")
                if isinstance(node, ast.Subscript):
                    name = self._ast_name(node.value)
                    if name in ("os.environ", "environ"):
                        key = self._extract_str_key(node.slice)
                        results.append({
                            "file": filepath, "line": node.lineno,
                            "type": "env_subscript",
                            "key": key,
                            "context": self._get_or_empty(lines, node.lineno),
                        })

                # os.getenv("KEY") / os.environ.get("KEY")
                if isinstance(node, ast.Call):
                    func = node.func
                    name = self._ast_name(func) if isinstance(func, (ast.Name, ast.Attribute)) else ""
                    if name in ("os.getenv", "os.environ.get", "getenv"):
                        key = self._extract_first_str_arg(node)
                        results.append({
                            "file": filepath, "line": node.lineno,
                            "type": "getenv_call",
                            "key": key,
                            "context": self._get_or_empty(lines, node.lineno),
                        })

                # settings.XXX / config.XXX / Config(...)
                if isinstance(node, ast.Attribute):
                    base = self._ast_name(node.value)
                    if base in ("settings", "config", "cfg", "conf"):
                        results.append({
                            "file": filepath, "line": node.lineno,
                            "type": "config_attr",
                            "detail": f"{base}.{node.attr}",
                            "context": self._get_or_empty(lines, node.lineno),
                        })

            if symbol:
                results = [r for r in results if r.get("context", "").find(symbol) >= 0]

        return {"total_results": len(results), "results": results}

    def _extract_str_key(self, slice_node: ast.AST) -> str:
        """提取 Subscript 的 key 字符串。"""
        if isinstance(slice_node, ast.Constant) and isinstance(slice_node.value, str):
            return slice_node.value
        if isinstance(slice_node, ast.Name):
            return slice_node.id
        return self._ast_name(slice_node)

    # ---- custom ----

    def _pattern_custom(
        self, custom_pattern: Optional[Dict[str, str]], symbol: Optional[str],
    ) -> Dict[str, Any]:
        """自定义模式匹配。"""
        if not custom_pattern or "match" not in custom_pattern:
            return {"total_results": 0, "results": [],
                    "error": "custom_pattern 需要 'match' 字段"}
        if "type" not in custom_pattern:
            return {"total_results": 0, "results": [],
                    "error": "custom_pattern 需要 'type' 字段"}

        pat_type = custom_pattern["type"]
        pat_regex = custom_pattern["match"]
        try:
            compiled = re.compile(pat_regex)
        except re.error as e:
            return {"total_results": 0, "results": [],
                    "error": f"正则表达式无效: {e}"}

        results: List[Dict[str, Any]] = []

        for filepath, tree in (self._index or {}).items():
            lines = self._source_cache.get(filepath, [])

            for node in ast.walk(tree):
                matched = False
                detail = ""
                extra: Dict[str, Any] = {}

                if pat_type == "decorator" and isinstance(
                    node, (ast.FunctionDef, ast.AsyncFunctionDef)
                ):
                    for dec in node.decorator_list:
                        dec_str = ast.unparse(dec) if hasattr(ast, "unparse") else self._ast_name(dec)
                        if compiled.search(dec_str):
                            matched = True
                            detail = dec_str

                elif pat_type == "call" and isinstance(node, ast.Call):
                    call_str = ast.unparse(node.func) if hasattr(ast, "unparse") else self._ast_name(node.func)
                    if compiled.search(call_str):
                        matched = True
                        detail = call_str

                elif pat_type == "import" and isinstance(node, (ast.Import, ast.ImportFrom)):
                    import_str = ast.unparse(node) if hasattr(ast, "unparse") else self._ast_name(node)
                    if compiled.search(import_str):
                        matched = True
                        detail = import_str

                elif pat_type == "class_extend" and isinstance(node, ast.ClassDef):
                    for base in node.bases:
                        base_str = ast.unparse(base) if hasattr(ast, "unparse") else self._ast_name(base)
                        if compiled.search(base_str):
                            matched = True
                            detail = base_str
                            extra["class"] = node.name

                if matched:
                    ctx_line = self._get_line(lines, node.lineno)
                    results.append({
                        "file": filepath,
                        "line": node.lineno,
                        "type": pat_type,
                        "detail": detail,
                        "context": ctx_line.strip() if ctx_line else "",
                        **extra,
                    })

        if symbol:
            results = [r for r in results
                       if r.get("context", "").find(symbol) >= 0
                       or r.get("class", "") == symbol]

        return {"total_results": len(results), "results": results}

    # ---- helpers for pattern matching ----

    def _get_or_empty(self, lines: List[str], lineno: int) -> str:
        """获取行文本，不存在返回空字符串。"""
        line = self._get_line(lines, lineno)
        return line.strip() if line else ""

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
