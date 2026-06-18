"""
tests/unit/test_rule_engine.py
单元测试：core/rule_engine.py RuleEngine
覆盖：YAML 加载、多种 handler 解析、matcher 构建、冲突检测
"""

import tempfile
from pathlib import Path

import pytest

from core.rule_engine import RuleEngine
from core.hook_orchestrator import HookOrchestrator, ShellCommand
from core.models.hook import HookPoint, HookMatcher, HookResult


# ====== Fixtures ======

@pytest.fixture
def rules_dir():
    d = Path(tempfile.mkdtemp())
    (d / "security.yaml").write_text("""
rules:
  - id: block-danger
    hook_point: pre_tool_execution
    handler: builtins.security_check
    matcher:
      type: command
      pattern: "rm|sudo"
    priority: 10
    timeout: 3.0

  - id: audit-all
    hook_point: pre_tool_execution
    handler: command:python audit.py
    matcher:
      type: always
    priority: 1
""", encoding="utf-8")
    return d


@pytest.fixture
def engine():
    return RuleEngine()


# ====== 1. YAML 加载 ======

def test_load_dir(rules_dir, engine):
    count = engine.load_dir(rules_dir)
    assert count == 2
    assert engine.loaded_count == 2


def test_load_nonexistent_dir(engine):
    count = engine.load_dir(Path("/tmp/no-such-rules-dir"))
    assert count == 0


def test_load_invalid_yaml(engine):
    d = Path(tempfile.mkdtemp())
    bad = d / "bad.yaml"
    bad.write_text("not: valid: [[[ yaml", encoding="utf-8")
    rules = engine.load_file(bad)
    assert len(rules) == 0


# ====== 2. handler 解析 ======

def test_parse_builtins_handler(engine):
    from core.models.hook import HookResult, HookContext

    async def dummy(ctx):
        return HookResult.continue_()

    engine = RuleEngine(builtins={"security_check": dummy})
    handler = engine._resolve_handler("builtins.security_check", "test")
    assert handler is not None
    assert callable(handler)


def test_parse_command_handler(engine):
    handler = engine._resolve_handler("command:python audit.py", "test")
    assert isinstance(handler, ShellCommand)
    assert "python audit.py" in handler.command


def test_parse_unknown_builtins(engine):
    handler = engine._resolve_handler("builtins.no_such_handler", "test")
    assert handler is None
    assert len(engine.warnings) > 0


def test_parse_python_handler_not_yet(engine):
    handler = engine._resolve_handler("python:my_module.my_func", "test")
    assert handler is None  # 阶段 5 才实现


# ====== 3. matcher 构建 ======

def test_parse_always_matcher(engine):
    m = engine._parse_matcher({})
    assert m.type == "always"


def test_parse_command_matcher(engine):
    m = engine._parse_matcher({"type": "command", "pattern": "rm|sudo"})
    assert m.type == "command"
    assert m.pattern == "rm|sudo"


def test_parse_composite_matcher(engine):
    m = engine._parse_matcher({
        "type": "composite",
        "composite_op": "and",
        "children": [
            {"type": "tool_name", "pattern": "shell"},
            {"type": "command", "pattern": "pip"},
        ],
    })
    assert m.type == "composite"
    assert len(m.children) == 2


# ====== 4. 注册到 HookOrchestrator ======

def test_register_all(rules_dir, engine):
    engine.load_dir(rules_dir)
    orch = HookOrchestrator()
    count = engine.register_all(orch)
    # 只有 audit-all 可以注册（command: handler 可用）
    # block-danger 需要 builtins.security_check 不存在 → 跳过
    assert count >= 1
    hooks = orch.list_hooks()
    assert any(h["name"] == "audit-all" for h in hooks)


# ====== 5. 冲突检测 ======

def test_validate_no_conflicts(rules_dir, engine):
    engine.load_dir(rules_dir)
    warnings = engine.validate()
    # 不同的 priority → 无冲突
    conflict_warnings = [w for w in warnings if w.startswith("冲突")]
    assert len(conflict_warnings) == 0


def test_validate_same_priority_conflict(engine):
    # 手动构建两条相同 priority 的规则
    d = Path(tempfile.mkdtemp())
    (d / "conflict.yaml").write_text("""
rules:
  - id: rule-a
    hook_point: pre_tool_execution
    handler: command:echo a
    priority: 5
  - id: rule-b
    hook_point: pre_tool_execution
    handler: command:echo b
    priority: 5
""", encoding="utf-8")
    engine.load_dir(d)
    warnings = engine.validate()
    conflict_warnings = [w for w in warnings if w.startswith("冲突")]
    assert len(conflict_warnings) == 1
    assert "rule-a" in conflict_warnings[0]
    assert "rule-b" in conflict_warnings[0]


# ====== 6. 无效规则 ======

def test_invalid_hook_point(engine):
    d = Path(tempfile.mkdtemp())
    (d / "bad.yaml").write_text("""
rules:
  - id: bad-rule
    hook_point: invalid_point
    handler: command:echo
""", encoding="utf-8")
    engine.load_dir(d)
    orch = HookOrchestrator()
    count = engine.register_all(orch)
    assert count == 0
