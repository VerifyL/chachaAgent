"""
core/cli_history.py
SessionHistory — prompt_toolkit History 包装类，支持 session 级切换。
每个 session 的历史存在 sessions/{sid}/cli_history，切换 session 时自动切换到对应文件。
"""

from pathlib import Path

from prompt_toolkit.history import FileHistory, History


class SessionHistory(History):
    """支持 session 切换的 prompt_toolkit History。

    内部持有 FileHistory 引用，仅用于 I/O（读写文件）。
    prompt_toolkit 的 History 基类已正确实现内存管理（_loaded_strings + _loaded），
    子类只需重写 load_history_strings() 和 store_string() 两个 I/O 方法。

    注意：不要重写 get_strings() / append_string() / load()，
    否则会导致 SessionHistory._loaded_strings 与 FileHistory._loaded_strings 分裂，
    使历史导航（↑↓）和去重失效。
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

        重置 _loaded 标志，下次 load() 时将自动从新文件读取。
        """
        self._file_history = self._make_file_history(session_id)
        self._loaded = False
        self._loaded_strings = []

    # ---- 仅重写 I/O 方法（History 基类负责内存管理） ----

    def load_history_strings(self):
        """从当前 session 文件读取历史字符串（最新在前）。"""
        return self._file_history.load_history_strings()

    def store_string(self, string: str) -> None:
        """将一条命令持久化到当前 session 文件。"""
        # FileHistory.store_string 不会自动创建父目录，
        # 如果 session 目录被外部删除（如清理旧 session），需确保目录存在。
        path = Path(self._file_history.filename)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._file_history.store_string(string)
