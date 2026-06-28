"""
core/cli_history.py
SessionHistory — prompt_toolkit History 包装类，支持 session 级切换。
每个 session 的历史存在 sessions/{sid}/cli_history，切换 session 时自动切换到对应文件。
"""
from pathlib import Path

from prompt_toolkit.history import FileHistory, History


class SessionHistory(History):
    """支持 session 切换的 prompt_toolkit History 包装。

    内部持有 FileHistory 引用，切换 session 时替换为指向不同文件的 FileHistory。
    prompt_toolkit 的 PromptSession 无需感知变化，所有调用透明委托给内部 FileHistory。
    """

    def __init__(self, sessions_base_dir: Path, session_id: str):
        """初始化 SessionHistory。

        Args:
            sessions_base_dir: sessions 目录（如 ~/.chacha/projects/{pid}/memory/sessions）
            session_id: 当前 session ID
        """
        super().__init__()
        self._sessions_base = Path(sessions_base_dir)
        self._file_history = self._make_file_history(session_id)

    def _make_file_history(self, session_id: str) -> FileHistory:
        """为指定 session 创建 FileHistory，自动创建目录。"""
        path = self._sessions_base / session_id / "cli_history"
        path.parent.mkdir(parents=True, exist_ok=True)
        return FileHistory(str(path))

    def switch_session(self, session_id: str) -> None:
        """切换到另一个 session 的历史文件。

        旧历史自动保持，新历史延迟加载（首次按 ↑ 时自动读取）。
        """
        self._file_history = self._make_file_history(session_id)
        self._loaded = False
        self._loaded_strings = []

    # ---- delegate all History methods to _file_history ----

    def append_string(self, string: str) -> None:
        """追加一条命令到历史。"""
        self._file_history.append_string(string)

    def get_strings(self):
        """获取所有历史字符串。"""
        return self._file_history.get_strings()

    def load_history_strings(self):
        """延迟加载历史字符串。"""
        return self._file_history.load_history_strings()

    def store_string(self, string: str) -> None:
        """存储并追加一条命令。"""
        self._file_history.store_string(string)
