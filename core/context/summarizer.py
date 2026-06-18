"""
core/context/summarizer.py
Summarizer — LLM 摘要辅助（ContextCompressor SUMMARIZED + DreamPipeline CONSOLIDATED 共用）。

避免硬编码 prompt 分散在两处。

用法:
    s = Summarizer(llm_invoker)
    summary = await s.summarize(old_messages, style="brief")
"""

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

_PROMPTS = {
    "brief": "将以下对话历史总结为 2-3 句话的摘要，只提取关键决策和结果。",
    "detailed": (
        "从以下对话记忆中提取最关键的持久信息（最多 200 条）。"
        "去重、按主题分类、按重要性排序。"
        "分类: 用户偏好、项目决策、经验教训、修复的错误、项目进度。"
        "每条一行简洁摘要。输出 Markdown，用 ## 分类标题。"
    ),
}


class Summarizer:
    """LLM 摘要辅助"""

    def __init__(self, llm_invoker: Optional[Any] = None):
        self._llm = llm_invoker

    async def summarize(self, text: str, style: str = "brief") -> str:
        """调用 LLM 生成摘要。style: brief|detailed"""
        if not self._llm:
            return text

        system = _PROMPTS.get(style, _PROMPTS["brief"])
        resp = await self._llm.invoke(
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": text},
            ],
            session_id="summarizer",
        )
        return resp.text.strip()

    async def summarize_blocks(self, blocks: list, style: str = "brief") -> str:
        """拼接多个 block 的内容 → 摘要"""
        text = "\n".join(b.content for b in blocks if b.content)
        return await self.summarize(text, style=style)
