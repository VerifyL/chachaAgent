"""
core/rule_engine.py
RuleEngine — 声明式规则引擎：YAML 规则 → HookOrchestrator.register()。

设计理念：
1. 非开发人员通过 YAML 文件扩展钩子，无需写 Python 代码
2. 三种 handler：builtins.*（内置）/ python:*（用户模块）/ command:*（外部进程）
3. 扫描目录加载所有 .yaml / .yml 规则文件
4. 冲突检测：同一 hook_point + 不兼容 matcher 在同一优先级警告

用法:
    engine = RuleEngine()
    count = engine.load_dir(Path(".chacha/rules"))
    engine.register_all(hook_orchestrator)
"""

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml  # PyYAML

from core.hook_orchestrator import HookOrchestrator, ShellCommand
from core.models.hook import HookMatcher, HookPoint

logger = logging.getLogger(__name__)

# ========================= 内置处理器映射 =========================
# 名称 → (handler_callable, timeout)
# TODO(阶段5): 实现 builtins.security_check / cost_check 等实际函数
_BUILTINS: Dict[str, Any] = {}


class RuleEngine:
    """YAML 声明式规则引擎"""

    def __init__(self, builtins: Optional[Dict[str, Any]] = None):
        self._rules: List[Dict[str, Any]] = []
        self._builtins = builtins or _BUILTINS
        self._warnings: List[str] = []

    # ====== 加载 ======

    def load_dir(self, rules_dir: Path) -> int:
        """扫描目录，加载所有 .yaml / .yml 文件。返回加载的规则总数。"""
        if not rules_dir.exists():
            logger.warning("规则目录不存在: %s", rules_dir)
            return 0

        count = 0
        for ext in ("*.yaml", "*.yml"):
            for f in sorted(rules_dir.glob(ext)):
                count += len(self.load_file(f))
        return count

    def load_file(self, path: Path) -> List[Dict[str, Any]]:
        """加载单个 YAML 规则文件。返回解析后的规则列表。"""
        if not path.exists():
            logger.warning("规则文件不存在: %s", path)
            return []

        try:
            with open(path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
        except yaml.YAMLError as e:
            logger.error("YAML 解析失败: %s - %s", path, e)
            return []
        except Exception as e:
            logger.error("读取规则文件失败: %s - %s", path, e)
            return []

        if not isinstance(data, dict):
            logger.warning("规则文件格式错误（期望顶层为 dict）: %s", path)
            return []

        rules = data.get("rules", [])
        if not isinstance(rules, list):
            logger.warning("rules 字段应为列表: %s", path)
            return []

        parsed = []
        for rule_data in rules:
            rule = self._parse_rule(rule_data, path)
            if rule:
                self._rules.append(rule_data)
                parsed.append(rule_data)
                logger.debug("已加载规则: %s", rule_data.get("id"))

        return parsed

    # ====== 注册 ======

    def register_all(self, orchestrator: HookOrchestrator) -> int:
        """将所有已加载规则注册到 HookOrchestrator。返回注册数量。"""
        count = 0
        for rule_data in self._rules:
            if self._register_one(rule_data, orchestrator):
                count += 1
        return count

    def _register_one(self, rule_data: Dict[str, Any], orchestrator: HookOrchestrator) -> bool:
        """注册单条规则"""
        rule_id = rule_data.get("id", "unknown")
        hook_point = rule_data.get("hook_point", "")
        priority = rule_data.get("priority", 0)
        timeout = rule_data.get("timeout", 10.0)

        # 解析 hook_point
        try:
            hp = HookPoint(hook_point)
        except ValueError:
            self._warnings.append(f"规则 {rule_id}: 无效 hook_point='{hook_point}'")
            return False

        # 解析 handler
        handler = self._resolve_handler(rule_data.get("handler", ""), rule_id)
        if handler is None:
            return False

        # 解析 matcher
        matcher = self._parse_matcher(rule_data.get("matcher", {}))

        # 注册
        try:
            orchestrator.register(
                name=rule_id,
                hook_point=hp,
                handler=handler,
                matcher=matcher,
                priority=priority,
                timeout=timeout,
            )
            return True
        except Exception as e:
            self._warnings.append(f"规则 {rule_id}: 注册失败 - {e}")
            return False

    # ====== 解析 ======

    def _resolve_handler(self, handler_spec: str, rule_id: str):
        """解析 handler 字符串为可调用对象或 ShellCommand。

        builtins.xxx → 内置处理器映射
        command:xxx → ShellCommand（外部进程）
        python:xxx  → TODO(阶段5) 动态导入
        """
        if not handler_spec:
            self._warnings.append(f"规则 {rule_id}: 缺少 handler")
            return None

        if handler_spec.startswith("builtins."):
            name = handler_spec[len("builtins."):]
            handler = self._builtins.get(name)
            if handler is None:
                self._warnings.append(f"规则 {rule_id}: 未知内置处理器 '{name}'")
                return None
            return handler

        if handler_spec.startswith("command:"):
            cmd = handler_spec[len("command:"):]
            return ShellCommand(command=cmd.strip())

        if handler_spec.startswith("python:"):
            # TODO(阶段5): importlib 动态导入
            self._warnings.append(f"规则 {rule_id}: python: handler 尚未实现（阶段5）")
            return None

        self._warnings.append(f"规则 {rule_id}: 无法识别的 handler 格式 '{handler_spec}'")
        return None

    def _parse_matcher(self, matcher_data: Dict[str, Any]) -> HookMatcher:
        """从 YAML dict 构建 HookMatcher"""
        if not matcher_data or not isinstance(matcher_data, dict):
            return HookMatcher(type="always")

        mtype = matcher_data.get("type", "always")
        pattern = matcher_data.get("pattern")
        invert = matcher_data.get("invert", False)

        if mtype == "composite":
            children_data = matcher_data.get("children", [])
            children = [self._parse_matcher(c) for c in children_data]
            return HookMatcher(
                type=mtype,
                composite_op=matcher_data.get("composite_op", "or"),
                children=children,
            )

        return HookMatcher(type=mtype, pattern=pattern, invert=invert)

    def _parse_rule(self, data: Dict[str, Any], source: Path) -> Optional[Dict[str, Any]]:
        """校验单条规则的必要字段"""
        if not isinstance(data, dict):
            return None
        if not data.get("id"):
            logger.warning("规则缺少 id 字段: %s", source)
            return None
        if not data.get("hook_point"):
            logger.warning("规则 %s 缺少 hook_point: %s", data.get("id"), source)
            return None
        return data

    # ====== 校验 ======

    def validate(self) -> List[str]:
        """校验已加载规则，返回警告列表（冲突检测）。"""
        warnings = list(self._warnings)

        # 检测同 hook_point + 同 priority 的冲突
        groups: Dict[str, Dict[int, List[str]]] = {}
        for r in self._rules:
            hp = r.get("hook_point", "")
            pri = r.get("priority", 0)
            rid = r.get("id", "")
            if hp not in groups:
                groups[hp] = {}
            if pri not in groups[hp]:
                groups[hp][pri] = []
            groups[hp][pri].append(rid)

        for hp, pri_map in groups.items():
            for pri, ids in pri_map.items():
                if len(ids) > 1:
                    warnings.append(
                        f"冲突: hook_point='{hp}' priority={pri} 有 {len(ids)} 个规则: {ids}"
                    )

        return warnings

    @property
    def warnings(self) -> List[str]:
        return self._warnings

    @property
    def loaded_count(self) -> int:
        return len(self._rules)
