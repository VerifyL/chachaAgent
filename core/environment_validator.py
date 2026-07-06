import subprocess
import sys
from pathlib import Path


def validate_host_environment() -> bool:
    """
    启动时确定性环境校验，返回 True 表示满足要求，False 表示存在致命问题。
    校验项：
    1. 系统默认编码为 UTF-8
    2. Git 命令可用
    3. 运行时目录 .chacha/ 及其子目录可创建/可写
    """
    # 1. 编码检查
    current_encoding = sys.getdefaultencoding().lower()
    if current_encoding != 'utf-8':
        print(f"[ERROR] 系统默认编码为 {current_encoding}，ChaChaAgent 要求 UTF-8。")
        print("        请设置环境变量：LC_ALL=en_US.UTF-8 或 LANG=en_US.UTF-8")
        return False

    # 2. Git 可用性检查
    try:
        subprocess.run(
            ["git", "--version"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
            shell=False
        )
    except (subprocess.SubprocessError, FileNotFoundError):
        print("[ERROR] 未检测到 Git 命令。请安装 Git 并确保其可在 PATH 中访问。")
        print("        ChaChaAgent 依赖 Git 进行版本操作和审计追踪。")
        return False

    # 3. ripgrep (rg) 可用性检查
    rg_available = False
    try:
        subprocess.run(
            ["rg", "--version"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
            shell=False
        )
        rg_available = True
    except (subprocess.SubprocessError, FileNotFoundError):
        pass

    if not rg_available:
        print("[WARN] 未检测到 ripgrep (rg) 命令。大文件搜索将降级为 Python 逐行扫描，可能较慢。")
        print("       安装方法：")
        print("         macOS:      brew install ripgrep")
        print("         Ubuntu:     sudo apt install ripgrep")
        print("         Fedora:     sudo dnf install ripgrep")
        print("         Windows:    scoop install ripgrep  |  choco install ripgrep")
        print("         Cargo:      cargo install ripgrep")
        print("         Python:     pip install rxp (轻量版，功能受限)")

    # 4. 创建运行时根目录及子目录
    runtime_root = Path("./.chacha")
    try:
        runtime_root.mkdir(exist_ok=True)
        (runtime_root / "checkpoints").mkdir(exist_ok=True)
        (runtime_root / "memory" / "projects").mkdir(parents=True, exist_ok=True)
        (runtime_root / "rag_store").mkdir(exist_ok=True)
        (runtime_root / "logs").mkdir(exist_ok=True)
    except OSError as e:
        print(f"[ERROR] 无法创建运行时目录 {runtime_root.absolute()} : {e}")
        return False

    # 可选：显示 Python 版本警告（≥3.11 是推荐版本，但不强制）
    if sys.version_info < (3, 11):
        print(f"[WARN] Python 版本 {sys.version_info.major}.{sys.version_info.minor} 低于推荐版本 3.11")
        print("      部分功能可能受限，建议升级到 Python 3.11 或更高版本。")

    print("[INFO] 环境校验通过。")
    return True
