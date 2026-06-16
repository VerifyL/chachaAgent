"""
tests/unit/test_main.py
单元测试：main.py 参数解析
覆盖：子命令路由、默认值、未知参数报错、--version
"""

import pytest

from main import build_parser, main


# ========== 1. 默认行为 ==========

def test_no_args_prints_help():
    """无参数时返回 0 且打印 help"""
    code = main([])
    assert code == 0


# ========== 2. --version ==========

def test_version(capsys):
    """--version 打印版本号"""
    with pytest.raises(SystemExit) as exc:
        parser = build_parser()
        parser.parse_args(["--version"])
    assert exc.value.code == 0


# ========== 3. run 子命令 ==========

def test_run_default_mode():
    """默认 --mode=cli"""
    parser = build_parser()
    args = parser.parse_args(["run"])
    assert args.command == "run"
    assert args.mode == "cli"
    assert args.port == 8080


def test_run_cli_mode():
    parser = build_parser()
    args = parser.parse_args(["run", "--mode", "cli"])
    assert args.mode == "cli"


def test_run_web_mode():
    parser = build_parser()
    args = parser.parse_args(["run", "--mode", "web"])
    assert args.mode == "web"


def test_run_web_with_port():
    parser = build_parser()
    args = parser.parse_args(["run", "--mode", "web", "--port", "3000"])
    assert args.port == 3000


def test_run_web_with_host():
    parser = build_parser()
    args = parser.parse_args(["run", "--mode", "web", "--host", "0.0.0.0"])
    assert args.host == "0.0.0.0"


def test_run_cmd_returns_zero(capsys):
    """cmd_run 返回 0（stub 实现）"""
    code = main(["run"])
    assert code == 0


def test_run_invalid_mode_rejected():
    """非法 mode 应报错"""
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["run", "--mode", "desktop"])


def test_run_port_type_is_int():
    """--port 接受整数（argparse type=int 不做范围校验，由调用方处理）"""
    parser = build_parser()
    args = parser.parse_args(["run", "--port", "3000"])
    assert args.port == 3000


# ========== 4. init 子命令 ==========

def test_init_default():
    parser = build_parser()
    args = parser.parse_args(["init"])
    assert args.command == "init"
    assert args.project_id is None
    assert args.force is False


def test_init_with_project_id():
    parser = build_parser()
    args = parser.parse_args(["init", "-p", "my-project"])
    assert args.project_id == "my-project"


def test_init_force():
    parser = build_parser()
    args = parser.parse_args(["init", "--force"])
    assert args.force is True


# ========== 5. config 子命令 ==========

def test_config_default():
    parser = build_parser()
    args = parser.parse_args(["config"])
    assert args.command == "config"
    assert args.validate_only is False


def test_config_validate_only():
    parser = build_parser()
    args = parser.parse_args(["config", "--validate-only"])
    assert args.validate_only is True


# ========== 6. 错误处理 ==========

def test_unknown_subcommand():
    """不存在的子命令应报错"""
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["unknown-command"])


def test_unknown_argument():
    """不存在的参数应报错"""
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["--unknown-flag"])


def test_extra_args_on_init():
    """多余的未知参数应报错"""
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["init", "--extra-field"])


# ========== 7. 主入口返回码 ==========

def test_main_returns_zero_on_help(capsys):
    code = main([])
    assert code == 0


def test_version_exits_with_zero():
    """--version 打印版本并 exit(0)"""
    parser = build_parser()
    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["--version"])
    assert exc.value.code == 0
