# 静态规则加载器 (`core/context/static_rule_loader.py`)

`StaticRuleLoader` 按分层加载 `CHACHA.md` 文件，参考 Claude Code 的 `.claude/CLAUDE.md` 机制。

## 加载顺序

```
~/.chacha/CHACHA.md              → 用户级（最先）
project/CHACHA.md                → 项目级
project/src/CHACHA.md            → 子目录级（最后）
```

上层规则追加到下层之后，无覆盖行为。

## 使用

```python
from core.context.static_rule_loader import StaticRuleLoader

loader = StaticRuleLoader(project_root=Path("/path/to/project"))
rules = loader.load(sub_dir="src")  # 含子目录规则
# → "用户规则\n项目规则\nsrc 规则"

# 注入 ContextManager
mgr = ContextManager()
ctx = mgr.assemble(state, static_rules=rules)
```

## @import 语法

```markdown
# CHACHA.md
@import ./rules/coding-style.md     # 相对路径
@import ~/.chacha/shared/rules.md   # 绝对路径
```

循环引用会被检测并跳过（`_LOADED` 集合去重）。
