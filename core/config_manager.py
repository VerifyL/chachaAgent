"""
core/config_manager.py
配置管理器：加载、校验、热重载 chachaConfig.toml / harness.toml。
仅依赖 core.models.config 和标准库。
"""

import os
import tomllib
import logging
from pathlib import Path
from typing import Optional, Dict, Any, List
import threading

from core.models.config import ChaChaConfig

logger = logging.getLogger(__name__)


class ConfigManager:
    _instance: Optional["ConfigManager"] = None
    _lock = threading.Lock()

    def __new__(cls, config_path: Optional[Path] = None) -> "ConfigManager":
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self, config_path: Optional[Path] = None):
        if getattr(self, "_initialized", False):
            if config_path is not None and config_path != self._config_path:
                self._config_path = config_path
                self._config = None
                self._loaded_path = None
            return
        self._config_path: Optional[Path] = config_path
        self._config: Optional[ChaChaConfig] = None
        self._loaded_path: Optional[Path] = None
        self._config_lock = threading.RLock()
        self._watcher = None
        self._watch_callbacks: List[callable] = []
        self._initialized = True

    # ---------- 公共接口 ----------
    def load(self, force: bool = False) -> ChaChaConfig:
        with self._config_lock:
            current_path = self._find_config_file()
            if current_path is None:
                raise FileNotFoundError(
                    "未找到配置文件 chachaConfig.toml 或 harness.toml，请确保文件存在。"
                )

            if self._config is not None and not force and self._loaded_path == current_path:
                return self._config

            raw_data = self._read_toml(current_path)
            self._apply_env_overrides(raw_data)
            try:
                self._config = ChaChaConfig.model_validate(raw_data)
            except Exception as e:
                logger.error("配置校验失败: %s", e)
                raise ValueError(f"配置文件格式错误: {e}") from e

            self._loaded_path = current_path
            logger.info("配置文件已加载: %s", current_path)
            return self._config

    def get_config(self) -> ChaChaConfig:
        if self._config is None:
            self.load()
        return self._config  # type: ignore

    def reload(self) -> ChaChaConfig:
        return self.load(force=True)

    def start_watch(self, callback: Optional[callable] = None) -> None:
        try:
            from watchdog.observers import Observer
            from watchdog.events import FileSystemEventHandler
        except ImportError:
            logger.warning("watchdog 未安装，无法启用热加载。")
            return

        if self._watcher is not None:
            return

        path = self._find_config_file()
        if path is None:
            raise FileNotFoundError("配置文件不存在，无法启动监听。")

        class ConfigChangeHandler(FileSystemEventHandler):
            def __init__(self, mgr: "ConfigManager"):
                self.mgr = mgr

            def on_modified(self, event):
                if event.src_path == str(path.resolve()):
                    logger.info("配置文件变更，执行热重载...")
                    try:
                        new_config = self.mgr.reload()
                        if callback:
                            callback(new_config)
                        for cb in self.mgr._watch_callbacks:
                            cb(new_config)
                    except Exception as e:
                        logger.error("热重载失败: %s", e)

        self._watcher = Observer()
        self._watcher.schedule(ConfigChangeHandler(self), path.parent, recursive=False)
        self._watcher.start()
        logger.info("开始监听配置文件变更: %s", path)

    def stop_watch(self) -> None:
        if self._watcher:
            self._watcher.stop()
            self._watcher.join()
            self._watcher = None
            logger.info("已停止配置文件监听。")

    def register_callback(self, callback: callable) -> None:
        self._watch_callbacks.append(callback)

    # ---------- 内部方法 ----------
    def _find_config_file(self) -> Optional[Path]:
        if self._config_path is not None and self._config_path.exists():
            return self._config_path

        cwd = Path.cwd()
        candidates = [
            cwd / "chachaConfig.toml",
            cwd / "harness.toml",
            Path.home() / ".chacha" / "config.toml",
        ]
        for p in candidates:
            if p.exists():
                return p

        # 无配置文件 → 生成默认模板
        default = Path.home() / ".chacha" / "config.toml"
        write_default_config(default)
        return default if default.exists() else None

    def _read_toml(self, path: Path) -> Dict[str, Any]:
        try:
            with open(path, "rb") as f:
                return tomllib.load(f)
        except tomllib.TOMLDecodeError as e:
            raise ValueError(f"TOML 解析失败: {e}") from e
        except OSError as e:
            raise ValueError(f"读取配置文件失败: {e}") from e

    # TODO(阶段3): 当 ModelFactory 实现后，注册配置变更回调 → 模型热切换
    def _apply_env_overrides(self, data: Dict[str, Any], prefix: str = "CHA_CHA") -> None:
        """只处理顶层字段在模型中的环境变量，忽略其它。"""
        # 获取模型的所有顶层字段名
        valid_top_fields = set(ChaChaConfig.model_fields.keys())

        env_vars = {
            k[len(prefix) + 1:]: v
            for k, v in os.environ.items()
            if k.startswith(prefix + "_") and len(k) > len(prefix) + 1
        }
        if not env_vars:
            return

        for key_path, value in env_vars.items():
            parts = [p.lower() for p in key_path.split("__")]
            if not parts:
                continue
            # 只处理顶层字段在模型中的变量
            if parts[0] not in valid_top_fields:
                continue
            self._set_nested_value(data, parts, value)

    def _set_nested_value(self, data: Dict[str, Any], parts: List[str], value: str) -> None:
        """递归设置嵌套字典的值，若路径不存在则创建。"""
        if not parts:
            return
        key = parts[0]
        if len(parts) == 1:
            data[key] = self._convert_env_value(value)
        else:
            if key not in data:
                data[key] = {}
            if not isinstance(data[key], dict):
                # 若中间节点不是字典，则无法继续，直接设置叶子值（覆盖）
                data[key] = {}
            self._set_nested_value(data[key], parts[1:], value)

    def _convert_env_value(self, value: str) -> Any:
        if value.lower() in ("true", "false"):
            return value.lower() == "true"
        try:
            if "." in value:
                return float(value)
            else:
                return int(value)
        except ValueError:
            pass
        return value


# ---------- 全局便利函数 ----------
_config_manager: Optional[ConfigManager] = None

def get_config_manager() -> ConfigManager:
    global _config_manager
    if _config_manager is None:
        _config_manager = ConfigManager()
    return _config_manager

def load_config() -> ChaChaConfig:
    return get_config_manager().load()


def write_default_config(path: Optional[Path] = None) -> None:
    """首次运行时生成默认 ~/.chacha/config.toml 模板。"""
    target = path or (Path.home() / ".chacha" / "config.toml")
    if target.exists():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("""# ChachaAgent 全局配置
# 优先级: 项目 chachaConfig.toml > 此文件 > 环境变量

[model.providers.default]
provider = "openai"
# API 密钥（亦可设置环境变量 DEEPSEEK_API_KEY）
api_key = ""
# 自定义 API 端点
base_url = "https://api.deepseek.com"
default_model = "deepseek-v4-pro"
# 最大输出 token（默认 16384，亦可设置环境变量 MAX_TOKENS）
# max_tokens = 131072

# [dream]
# Session Dream 触发阈值
# dream_rounds = 10
# dream_hours = 24
# Global Dream 触发阈值
# global_dream_rounds = 50
# global_dream_hours = 72
""", encoding="utf-8")

def get_config() -> ChaChaConfig:
    return get_config_manager().get_config()