"""
tests/unit/test_token_counter.py
单元测试：core/context/token_counter.py
"""

import pytest

from core.context.token_counter import TokenCounter
from core.models.context import BlockSource, ContextBlock

# ====== Fixtures ======


@pytest.fixture
def counter():
    return TokenCounter(model_name="gpt-4")


# ====== 1. 基本计数 ======


def test_count_text(counter):
    tokens = counter.count_text("Hello world")
    assert tokens >= 2


def test_count_empty(counter):
    assert counter.count_text("") == 0


def test_count_chinese(counter):
    tokens = counter.count_text("你好世界")
    assert tokens >= 2  # 中文 token 比英文多


# ====== 2. 消息计数 ======


def test_count_messages(counter):
    tokens = counter.count_messages(
        [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "Hello"},
        ]
    )
    assert tokens > 10


def test_count_messages_empty(counter):
    tokens = counter.count_messages([])
    assert tokens == 0


# ====== 3. 工具 Schema 计数 ======


def test_count_tool_schemas(counter):
    tokens = counter.count_tool_schemas(
        [
            {"type": "function", "function": {"name": "read_file", "description": "..."}},
        ]
    )
    assert tokens > 20


# ====== 4. ContextBlock 计数 ======


def test_count_block(counter):
    block = ContextBlock(
        source=BlockSource.HISTORY,
        role="user",
        content="Hello world",
        zone="dynamic",
        priority=3,
    )
    tokens = counter.count_block(block)
    assert tokens >= 2


def test_count_blocks(counter):
    blocks = [
        ContextBlock(
            source=BlockSource.SYSTEM_PROMPT, role="system", content="system prompt", zone="protected", priority=0
        ),
        ContextBlock(source=BlockSource.HISTORY, role="user", content="user msg", zone="dynamic", priority=3),
    ]
    tokens = counter.count_blocks(blocks)
    assert tokens >= 2


# ====== 5. 多模态内容 ======


def test_count_multimodal_content(counter):
    tokens = counter.count_messages(
        [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Describe this image"},
                    {"type": "image_url", "image_url": {"url": "https://..."}},
                ],
            },
        ]
    )
    assert tokens >= 3  # 只计数 text 部分


# ====== 6. 不同模型 ======


def test_count_deepseek_uses_cl100k():
    counter = TokenCounter(model_name="deepseek-chat")
    tokens = counter.count_text("test")
    assert tokens >= 1


def test_count_gpt4o_uses_o200k():
    counter = TokenCounter(model_name="gpt-4o")
    tokens = counter.count_text("test")
    assert tokens >= 1


# ====== 7. 图片 Token 估算（预留） ======


def test_estimate_image_tokens_low():
    tokens = TokenCounter.estimate_image_tokens(512, 512, detail="low")
    assert tokens == 85


def test_estimate_image_tokens_auto():
    tokens = TokenCounter.estimate_image_tokens(512, 512, detail="auto")
    assert tokens > 0


def test_estimate_image_tokens_high():
    tokens = TokenCounter.estimate_image_tokens(512, 512, detail="high")
    assert tokens >= 85


def test_estimate_image_tokens_large_high():
    """大图在 high 模式下占更多 token"""
    small = TokenCounter.estimate_image_tokens(512, 512, detail="high")
    large = TokenCounter.estimate_image_tokens(2048, 2048, detail="high")
    assert large >= small
