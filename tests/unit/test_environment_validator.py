import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from core.environment_validator import validate_host_environment


@pytest.fixture
def temp_workdir():
    """临时切换工作目录，避免污染真实 .chacha"""
    old_cwd = Path.cwd()
    with tempfile.TemporaryDirectory() as tmpdir:
        os.chdir(tmpdir)
        yield Path(tmpdir)
    os.chdir(old_cwd)


def test_valid_environment(temp_workdir):
    """所有条件满足时应返回 True"""
    with patch("sys.getdefaultencoding", return_value="utf-8"):
        with patch("subprocess.run") as mock_run:
            # 模拟 Git 命令成功
            mock_run.return_value = MagicMock()
            result = validate_host_environment()
            assert result is True
            # 验证目录已创建
            assert (temp_workdir / ".chacha").exists()
            assert (temp_workdir / ".chacha/checkpoints").exists()


def test_invalid_encoding(temp_workdir):
    """编码不是 UTF-8 时应返回 False"""
    with patch("sys.getdefaultencoding", return_value="ascii"):
        result = validate_host_environment()
        assert result is False


def test_git_not_found(temp_workdir):
    """Git 不可用时应返回 False"""
    with patch("sys.getdefaultencoding", return_value="utf-8"):
        with patch("subprocess.run", side_effect=FileNotFoundError):
            result = validate_host_environment()
            assert result is False


def test_directory_creation_fails(temp_workdir):
    """目录创建失败时应返回 False"""
    with patch("sys.getdefaultencoding", return_value="utf-8"):
        with patch("subprocess.run"):
            with patch("pathlib.Path.mkdir", side_effect=PermissionError):
                result = validate_host_environment()
                assert result is False
