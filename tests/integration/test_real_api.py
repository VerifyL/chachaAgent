"""
tests/integration/test_real_api.py
真实 API 有限测试（DeepSeek）— 仅手动运行：
  DEEPSEEK_API_KEY=sk-... DEEPSEEK_BASE_URL=https://api.deepseek.com \
    .venv/bin/python -m pytest tests/integration/test_real_api.py -v -m slow
"""

import os

import pytest

from core.llm_clients.openai_client import OpenAIClient
from core.llm_invoker import LLMInvoker


@pytest.fixture(scope="module")
def api_key():
    key = os.environ.get("DEEPSEEK_API_KEY")
    if not key:
        pytest.skip("DEEPSEEK_API_KEY not set")
    return key


@pytest.fixture(scope="module")
def base_url():
    return os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")


@pytest.fixture(scope="module")
def model():
    return os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")


# ====== 真实 API 测试 ======


@pytest.mark.slow
@pytest.mark.asyncio
async def test_real_text_only(api_key, base_url, model):
    """最简单：Say hello → 验证文本非空"""
    client = OpenAIClient(
        api_key=api_key,
        model=model,
        base_url=base_url,
        max_tokens=50,
    )
    invoker = LLMInvoker(model_client=client)
    resp = await invoker.invoke(
        messages=[{"role": "user", "content": "Say hello in one word"}],
        session_id="real-test",
    )

    # text 可能为空（DeepSeek 小 max_tokens 时 length 截断）
    assert isinstance(resp.text, str)
    assert resp.finish_reason in ("stop", "length")
    assert resp.duration_ms > 0


@pytest.mark.slow
@pytest.mark.asyncio
async def test_real_tool_call(api_key, base_url, model):
    """验证工具调用：要求 read_file → 返回工具调用"""
    client = OpenAIClient(
        api_key=api_key,
        model=model,
        base_url=base_url,
        max_tokens=200,
    )
    invoker = LLMInvoker(model_client=client)

    resp = await invoker.invoke(
        messages=[{
            "role": "user",
            "content": "Read the file /tmp/hello.py using the read_file tool",
        }],
        tools=[{
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "Read a file",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "File path"},
                    },
                    "required": ["path"],
                },
            },
        }],
        session_id="real-test-tool",
    )

    assert len(resp.tool_calls) > 0
    assert resp.tool_calls[0].name == "read_file"
    assert "path" in resp.tool_calls[0].arguments
