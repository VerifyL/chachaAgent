"""
tests/unit/test_cli_widgets.py
单元测试：interface/cli/widgets.py 组件渲染
"""

import pytest

from interface.cli.widgets import ChatMessage, ToolCallBanner, StatusBar


# ====== ChatMessage ======

def test_chat_message_user():
    msg = ChatMessage.render("user", "Hello world")
    assert "You:" in msg
    assert "Hello world" in msg


def test_chat_message_assistant():
    msg = ChatMessage.render("assistant", "我来帮你")
    assert "Chacha:" in msg
    assert "我来帮你" in msg


def test_chat_message_system():
    msg = ChatMessage.render("system", "消息")
    assert "System:" in msg


# ====== ToolCallBanner ======

def test_tool_banner_start():
    msg = ToolCallBanner.render("read_file", "start")
    assert "read_file" in msg
    assert "Calling" in msg


def test_tool_banner_end():
    msg = ToolCallBanner.render("read_file", "end", preview="content preview")
    assert "done" in msg
    assert "content preview" in msg


def test_tool_banner_end_no_preview():
    msg = ToolCallBanner.render("read_file", "end")
    assert "done" in msg


# ====== StatusBar ======

def test_status_bar_default():
    bar = StatusBar()
    bar.update()
    # Static.render() returns RenderResult
    rendered = str(bar.render())
    assert "就绪" in rendered


def test_status_bar_tokens():
    bar = StatusBar()
    bar.update(tokens=1000)
    rendered = str(bar.render())
    assert "1000" in rendered


def test_status_bar_both():
    bar = StatusBar()
    bar.update(tokens=500, rounds=3, model="deepseek-chat")
    rendered = str(bar.render())
    assert "500" in rendered
    assert "3" in rendered
    assert "deepseek-chat" in rendered


def test_status_bar_thinking():
    bar = StatusBar()
    bar.update(extra="⏳ read_file...")
    rendered = str(bar.render())
    assert "read_file" in rendered
