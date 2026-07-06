"""
tests/unit/test_init_project.py
使用 pytest 测试 scripts/init_project.py（Python 初始化脚本）
"""

import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest


def locate_script() -> Path:
    """定位 init_project.py，若找不到则明确报错"""
    test_file = Path(__file__).resolve()
    candidates = [
        test_file.parent.parent / "scripts" / "init_project.py",
        Path.cwd() / "scripts" / "init_project.py",
        Path.cwd() / ".." / "scripts" / "init_project.py",
    ]
    for p in candidates:
        if p.exists():
            return p
    raise FileNotFoundError(f"未找到 init_project.py，尝试过: {candidates}")


@pytest.fixture
def test_env(tmp_path: Path):
    """创建临时测试环境，复制脚本并切换工作目录"""
    script_src = locate_script()
    script_dst = tmp_path / "init_project.py"
    shutil.copy(script_src, script_dst)
    script_dst.chmod(0o755)
    original_cwd = os.getcwd()
    os.chdir(tmp_path)
    yield tmp_path, script_dst
    os.chdir(original_cwd)


def run_script(script_path: Path, *args, env=None):
    """
    运行 Python 脚本，返回 subprocess.CompletedProcess 结果。
    """
    cmd = [sys.executable, str(script_path)] + list(args)
    try:
        result = subprocess.run(
            cmd,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            cwd=os.getcwd(),
        )
    except Exception as e:
        pytest.fail(f"运行脚本失败: {e}")
    return result


def test_runs_successfully(test_env):
    tmp_path, script_path = test_env
    result = run_script(script_path)
    if result.returncode != 0:
        print("STDERR:", result.stderr)
        print("STDOUT:", result.stdout)
    assert result.returncode == 0
    assert "初始化完成" in result.stdout


def test_creates_directory_structure(test_env):
    tmp_path, script_path = test_env
    result = run_script(script_path)
    assert result.returncode == 0
    assert (tmp_path / ".chacha" / "checkpoints").is_dir()
    assert (tmp_path / ".chacha" / "memory" / "projects" / "default" / "memory").is_dir()
    assert (tmp_path / ".chacha" / "memory" / "projects" / "default" / "topics").is_dir()
    assert (tmp_path / ".chacha" / "rag_store").is_dir()
    assert (tmp_path / ".chacha" / "logs").is_dir()


def test_creates_memory_file(test_env):
    """v2.1: MEMORY.md 由 autoDream 管道异步生成，init_project 仅准备目录"""
    tmp_path, script_path = test_env
    result = run_script(script_path)
    assert result.returncode == 0
    memory_dir = tmp_path / ".chacha" / "memory" / "projects" / "default" / "memory"
    assert memory_dir.is_dir()
    assert "记忆目录已准备" in result.stdout


def test_generates_config_if_missing(test_env):
    tmp_path, script_path = test_env
    (tmp_path / "chachaConfig.toml").unlink(missing_ok=True)
    result = run_script(script_path)
    assert result.returncode == 0
    config_file = tmp_path / "chachaConfig.toml"
    assert config_file.is_file()
    content = config_file.read_text()
    assert 'project_id = "default"' in content


def test_sets_correct_permissions(test_env):
    tmp_path, script_path = test_env
    result = run_script(script_path)
    assert result.returncode == 0
    runtime_dir = tmp_path / ".chacha"
    mode = runtime_dir.stat().st_mode
    # 检查 owner 拥有读写执行权限，group 和 other 无权限
    assert (mode & stat.S_IRWXU) == stat.S_IRWXU
    assert (mode & stat.S_IRWXG) == 0
    assert (mode & stat.S_IRWXO) == 0


def test_force_overwrites(test_env):
    """v2.1: --force 重建目录并覆盖配置文件"""
    tmp_path, script_path = test_env
    # 第一次运行
    result = run_script(script_path)
    assert result.returncode == 0
    # 修改配置文件为旧内容
    config_file = tmp_path / "chachaConfig.toml"
    config_file.write_text('project_id = "old_stuff"\n')
    # 在目录中创建额外文件，验证 --force 会清空重建
    extra_file = tmp_path / ".chacha" / "checkpoints" / "extra.txt"
    extra_file.parent.mkdir(parents=True, exist_ok=True)
    extra_file.write_text("should be removed")
    # --force 运行
    result = run_script(script_path, "--force")
    assert result.returncode == 0
    # 配置文件被覆盖
    content = config_file.read_text()
    assert 'project_id = "default"' in content
    # 额外文件被清除
    assert not extra_file.exists()
    # 目录被重建
    assert "强制重建目录" in result.stdout


def test_custom_project_id(test_env):
    """v2.1: 自定义项目 ID 创建对应目录结构和配置"""
    tmp_path, script_path = test_env
    result = run_script(script_path, "-p", "custom_project")
    assert result.returncode == 0
    custom_memory = tmp_path / ".chacha" / "memory" / "projects" / "custom_project" / "memory"
    assert custom_memory.is_dir()
    custom_topics = tmp_path / ".chacha" / "memory" / "projects" / "custom_project" / "topics"
    assert custom_topics.is_dir()
    # MEMORY.md 由 autoDream 管道异步生成，此处仅验证目录就绪
    assert "记忆目录已准备: memory/projects/custom_project/memory" in result.stdout
    config_file = tmp_path / "chachaConfig.toml"
    assert 'project_id = "custom_project"' in config_file.read_text()


def test_help_message(test_env):
    tmp_path, script_path = test_env
    result = run_script(script_path, "--help")
    assert result.returncode == 0
    assert "usage:" in result.stdout.lower()


def test_skip_existing_dirs_without_force(test_env):
    tmp_path, script_path = test_env
    (tmp_path / ".chacha" / "checkpoints").mkdir(parents=True)
    result = run_script(script_path)
    assert result.returncode == 0
    assert "目录已存在，跳过" in result.stdout


def test_env_var_project_id(test_env):
    tmp_path, script_path = test_env
    env = os.environ.copy()
    env["PROJECT_ID"] = "env_project"
    result = subprocess.run(
        [sys.executable, str(script_path)],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=os.getcwd(),
    )
    assert result.returncode == 0
    env_memory = tmp_path / ".chacha" / "memory" / "projects" / "env_project" / "memory"
    assert env_memory.is_dir()
    config_file = tmp_path / "chachaConfig.toml"
    assert 'project_id = "env_project"' in config_file.read_text()


def test_cli_overrides_env_var(test_env):
    tmp_path, script_path = test_env
    env = os.environ.copy()
    env["PROJECT_ID"] = "env_project"
    result = subprocess.run(
        [sys.executable, str(script_path), "-p", "cli_project"],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=os.getcwd(),
    )
    assert result.returncode == 0
    assert (tmp_path / ".chacha" / "memory" / "projects" / "cli_project" / "memory").is_dir()
    assert not (tmp_path / ".chacha" / "memory" / "projects" / "env_project").exists()
    config_file = tmp_path / "chachaConfig.toml"
    assert 'project_id = "cli_project"' in config_file.read_text()


def test_copy_config_from_examples(test_env):
    tmp_path, script_path = test_env
    examples_dir = tmp_path / "examples"
    examples_dir.mkdir()
    example_config = examples_dir / "chachaConfig.toml"
    example_config.write_text(
        (
            'project_id = "from_example"\n'
            'environment = "prod"\n'
            "[model]\n"
            "[model.providers.default]\n"
            'provider = "openai"\n'
            'default_model = "gpt-4"\n'
        )
    )
    (tmp_path / "chachaConfig.toml").unlink(missing_ok=True)
    result = run_script(script_path)
    assert result.returncode == 0
    assert "从 examples/chachaConfig.toml 复制配置文件" in result.stdout
    config_file = tmp_path / "chachaConfig.toml"
    content = config_file.read_text()
    assert 'project_id = "from_example"' in content
