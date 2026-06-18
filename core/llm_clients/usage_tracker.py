"""
core/model/usage_tracker.py
UsageTracker — 用量累加器：输入/输出 token + 成本统计（阶段 4 接入 TokenCounter 后精确化）。

用法:
    tracker = UsageTracker()
    tracker.record("gpt-4", input_tokens=1000, output_tokens=500,
                   cost_per_1k_input=0.003, cost_per_1k_output=0.015)
    print(tracker.total_cost)
"""

import logging

logger = logging.getLogger(__name__)


class UsageTracker:
    """Token 和成本累加器"""

    def __init__(self):
        self._total_input: int = 0
        self._total_output: int = 0
        self._total_cost: float = 0.0
        self._call_count: int = 0
        self._per_model: dict[str, dict] = {}  # {model: {input, output, cost, calls}}

    # ====== 记录 ======

    def record(
        self,
        model: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cost_per_1k_input: float = 0.0,
        cost_per_1k_output: float = 0.0,
    ) -> None:
        """记录一次 LLM 调用。成本由传入的单价计算。"""
        cost = (input_tokens * cost_per_1k_input + output_tokens * cost_per_1k_output) / 1000

        self._total_input += input_tokens
        self._total_output += output_tokens
        self._total_cost += cost
        self._call_count += 1

        if model not in self._per_model:
            self._per_model[model] = {"input": 0, "output": 0, "cost": 0.0, "calls": 0}
        m = self._per_model[model]
        m["input"] += input_tokens
        m["output"] += output_tokens
        m["cost"] += cost
        m["calls"] += 1

    # ====== 查询 ======

    @property
    def total_input(self) -> int:
        return self._total_input

    @property
    def total_output(self) -> int:
        return self._total_output

    @property
    def total_tokens(self) -> int:
        return self._total_input + self._total_output

    @property
    def total_cost(self) -> float:
        return self._total_cost

    @property
    def call_count(self) -> int:
        return self._call_count

    def per_model(self, model: str) -> dict:
        return self._per_model.get(model, {})

    def summary(self) -> dict:
        return {
            "total_input": self._total_input,
            "total_output": self._total_output,
            "total_cost": round(self._total_cost, 6),
            "call_count": self._call_count,
            "per_model": self._per_model,
        }

    # ====== 重置 ======

    def reset(self) -> None:
        self._total_input = 0
        self._total_output = 0
        self._total_cost = 0.0
        self._call_count = 0
        self._per_model.clear()
