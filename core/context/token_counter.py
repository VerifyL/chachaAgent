"""
core/context/token_counter.py
TokenCounter — 基于 tiktoken 的精确 Token 计数。

替换 ContextManager._estimate_tokens() 的 len(text)//4 粗略估算。
参考 Harness TokenCounter + 原生 SDK 计数。

用法:
    counter = TokenCounter("gpt-4")
    tokens = counter.count_text("Hello world")
    tokens = counter.count_messages([{"role": "user", "content": "hi"}])
"""

import logging
from typing import Any, Dict, List

import tiktoken

logger = logging.getLogger(__name__)

# 模型名 → tiktoken encoding 名称映射
_MODEL_ENCODING: Dict[str, str] = {
    "gpt-4": "cl100k_base",
    "gpt-4-turbo": "cl100k_base",
    "gpt-4o": "o200k_base",
    "gpt-3.5-turbo": "cl100k_base",
    "deepseek-chat": "cl100k_base",
    "deepseek-v4-pro": "cl100k_base",
    "llama3": "cl100k_base",  # 近似
}
_DEFAULT_ENCODING = "cl100k_base"


class TokenCounter:
    """基于 tiktoken 的精确 Token 计数器"""

    def __init__(self, model_name: str = "gpt-4"):
        encoding_name = _MODEL_ENCODING.get(model_name, _DEFAULT_ENCODING)
        try:
            self._enc = tiktoken.get_encoding(encoding_name)
        except Exception:
            self._enc = tiktoken.get_encoding(_DEFAULT_ENCODING)
        self._model = model_name

    # ====== 公有接口 ======

    def count_text(self, text: str) -> int:
        """计算单个文本的 token 数"""
        return len(self._enc.encode(text))

    def count_messages(self, messages: List[Dict[str, Any]]) -> int:
        """计算消息列表的总 token 数（粗略，不含 overhead）"""
        total = 0
        for msg in messages:
            content = msg.get("content", "")
            if isinstance(content, str):
                total += self.count_text(content)
            elif isinstance(content, list):
                # 多模态 content（如 [{"type": "text", "text": "..."}]）
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        total += self.count_text(part.get("text", ""))
        # 每条约 4 tokens overhead（role separator）
        total += len(messages) * 4
        return total

    def count_tool_schemas(self, tools: List[Dict[str, Any]]) -> int:
        """计算工具定义的 token 数"""
        import json
        text = json.dumps(tools, ensure_ascii=False, indent=2)
        return self.count_text(text)

    def count_block(self, block) -> int:
        """计算 ContextBlock 的 token 数"""
        return self.count_text(block.content) if block.content else 0

    def count_blocks(self, blocks: list) -> int:
        """批量计算 ContextBlock 的 token 总数"""
        return sum(self.count_block(b) for b in blocks)

    # ====== 多模态 token 计算（预留） ======

    @staticmethod
    def estimate_image_tokens(width: int, height: int, detail: str = "auto") -> int:
        """估算图片 token 数（参考 OpenAI Vision pricing）。

        TODO(v1.5): 接入实际多模态模型时精确化。
        当前返回默认值 85（low detail 基准），表示一张 512x512 图片约 85 tokens。
        """
        if detail == "low":
            return 85
        # high detail: 按 512x512 tiles 计算
        tiles = max(1, (width // 512) * (height // 512))
        return 85 + tiles * 170
