"""
tests/unit/test_plugin_installer.py
单元测试：capabilities/plugin_installer.py PluginInstaller 骨架
"""

import tempfile
from pathlib import Path

import pytest

from capabilities.plugin_installer import PluginInstaller


@pytest.fixture
def installer():
    d = Path(tempfile.mkdtemp())
    return PluginInstaller(plugins_dir=d)


def test_init_creates_dir(installer):
    assert installer._dir.exists()


@pytest.mark.asyncio
async def test_install_returns_false(installer):
    assert await installer.install("test-plugin") is False


def test_uninstall_returns_false(installer):
    assert installer.uninstall("test-plugin") is False


def test_list_empty(installer):
    assert installer.list_installed() == []


def test_list_detects_manifest(installer):
    # 创建有 manifest.json 的目录
    d = installer._dir / "my-plugin"
    d.mkdir()
    (d / "manifest.json").write_text("{}")
    assert "my-plugin" in installer.list_installed()


def test_validate_manifest_exists(installer):
    d = installer._dir / "valid-plugin"
    d.mkdir()
    (d / "manifest.json").write_text("{}")
    assert PluginInstaller.validate(d) is True


def test_validate_no_manifest(installer):
    d = installer._dir / "no-manifest"
    d.mkdir()
    assert PluginInstaller.validate(d) is False
