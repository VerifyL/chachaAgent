"""
tests/unit/test_rag.py
单元测试：capabilities/rag/ 骨架
"""

from pathlib import Path

import pytest

from capabilities.rag.symbol_parser import SymbolParser
from capabilities.rag.vector_store import VectorStore

# ====== VectorStore ======

@pytest.mark.asyncio
async def test_vector_store_index_returns_zero():
    vs = VectorStore()
    assert await vs.index(Path("/tmp")) == 0


@pytest.mark.asyncio
async def test_vector_store_search_empty():
    vs = VectorStore()
    assert await vs.search("hello") == []


@pytest.mark.asyncio
async def test_vector_store_delete_returns_false():
    vs = VectorStore()
    assert await vs.delete("test.py") is False


# ====== SymbolParser ======

@pytest.mark.asyncio
async def test_symbol_parser_parse_empty():
    sp = SymbolParser()
    assert await sp.parse("test.py") == []


@pytest.mark.asyncio
async def test_symbol_parser_call_graph_empty():
    sp = SymbolParser()
    assert await sp.build_call_graph([]) == {}


@pytest.mark.asyncio
async def test_symbol_parser_find_definition_none():
    sp = SymbolParser()
    assert await sp.find_definition("my_func") is None
