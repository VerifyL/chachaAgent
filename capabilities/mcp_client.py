"""
capabilities/mcp_client.py
MCPClient — 基于 mcp SDK 的 MCP 协议客户端。

职责:
  1. 启停子进程（通过 mcp SDK 的 stdio/SSE 传输）
  2. 工具发现 + include/exclude 过滤 → 返回 MCPToolAdapter 列表
  3. 工具缓存（~/.chacha/mcp_tools_cache.json）实现秒开
  4. 后台连接：缓存命中后启动真实连接，在线替换适配器
  5. 进程生命周期管理（进程组隔离、超时、强制 kill）

参考: MCP 协议规范 (modelcontextprotocol.io) | python-sdk
"""

import asyncio
import hashlib
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from mcp import ClientSession

logger = logging.getLogger(__name__)

# 进程生命周期常量
_SPAWN_TIMEOUT = 60.0  # 子进程/传输层启动超时（秒），比 initialize 长，适应 npx 首次下载（部分 server 首次需 45s+）
_CONNECT_TIMEOUT = 15.0  # initialize 超时（秒）
_CALL_TIMEOUT = 60.0  # tools/call 超时（秒）
_PER_SERVER_TIMEOUT = _SPAWN_TIMEOUT + _CONNECT_TIMEOUT  # 单 server 总超时

# 缓存
CACHE_PATH = Path.home() / ".chacha" / "mcp_tools_cache.json"


def _compute_config_hash(servers_config: dict) -> str:
    normalized = {}
    for name in sorted(servers_config.keys()):
        cfg = servers_config[name]
        if hasattr(cfg, "model_dump"):
            d = cfg.model_dump()
        elif isinstance(cfg, dict):
            d = dict(cfg)
        else:
            d = {}
        normalized[name] = {
            "transport": str(d.get("transport", "stdio")),
            "command": str(d.get("command", "")),
            "args": list(d.get("args") or []),
            "env": dict(sorted((d.get("env") or {}).items())),
            "url": str(d.get("url", "")),
        }
    raw = json.dumps(normalized, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _read_cache() -> Optional[dict]:
    try:
        if not CACHE_PATH.exists():
            return None
        return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _write_cache(config_hash: str, servers_data: dict) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    cache = {
        "config_hash": config_hash,
        "servers": servers_data,
        "cached_at": datetime.now(timezone.utc).isoformat(),
    }
    CACHE_PATH.write_text(
        json.dumps(cache, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


class MCPClientError(Exception):
    """MCP 客户端错误"""


class MCPClient:
    """MCP 协议客户端（基于 mcp SDK）。

    管理多个 MCP server 的子进程，对外暴露 get_tools() / call_tool()。
    """

    def __init__(self, server_configs: Optional[Dict[str, Any]] = None):
        self._server_configs: Dict[str, Any] = server_configs or {}
        # SDK session 和传输层
        self._sessions: Dict[str, ClientSession] = {}
        self._server_caps: Dict[str, dict] = {}
        self._http_cms: Dict[str, Any] = {}  # streamable-http 上下文管理器引用
        self._transport_cms: Dict[str, Any] = {}  # stdio/sse 传输层上下文管理器引用
        self._connected = False
        # 缓存相关
        self._from_cache = False
        self._cached_servers: Dict[str, dict] = {}
        self._config_hash: str = ""
        self._bg_ready = asyncio.Event()
        self._bg_client: Optional["MCPClient"] = None
        self._tool_executor: Any = None  # 用于断连时移除失效的 MCP 工具

    # ====== 生命周期 ======

    async def connect(self) -> bool:
        """连接所有 MCP server，先检查缓存。

        缓存命中时跳过连接，由 background_connect() 后台完成真实连接。
        """
        if not self._server_configs:
            logger.info("[mcp] 没有配置 MCP server，跳过")
            self._connected = True
            return True

        self._config_hash = _compute_config_hash(self._server_configs)
        cached = _read_cache()

        if cached and cached.get("config_hash") == self._config_hash:
            # 检查缓存是否过期（24h）
            cached_at = cached.get("cached_at")
            age = timedelta(hours=24)
            if cached_at:
                try:
                    parsed = datetime.fromisoformat(cached_at)
                    if datetime.now(timezone.utc) - parsed >= age:
                        logger.info("[mcp] 缓存已过期（>24h），重新连接")
                        cached = None  # 当作缓存 miss
                    else:
                        age_left = age - (datetime.now(timezone.utc) - parsed)
                        logger.info("[mcp] 缓存未过期（剩余 %ds），使用缓存", int(age_left.total_seconds()))
                except Exception:
                    pass
        if cached and cached.get("config_hash") == self._config_hash:
            self._from_cache = True
            self._cached_servers = cached.get("servers", {})
            self._connected = True
            logger.info(
                "[mcp] 缓存命中，%d server 工具从缓存加载",
                len(self._cached_servers),
            )
            return True

        # 缓存未命中：正常连接
        self._from_cache = False

        async def _connect_safe(name, cfg):
            try:
                await asyncio.wait_for(
                    self._connect_one(name, cfg),
                    timeout=_PER_SERVER_TIMEOUT,
                )
                return True
            except asyncio.TimeoutError:
                logger.error("[mcp] %s 连接超时（%ss）", name, _PER_SERVER_TIMEOUT)
                return False
            except Exception as e:
                logger.error("[mcp] %s 连接失败: %s", name, e)
                return False

        results = await asyncio.gather(*[_connect_safe(name, cfg) for name, cfg in self._server_configs.items()])
        success_count = sum(results)

        if success_count == 0 and self._server_configs:
            logger.warning("[mcp] 所有 server 连接均失败")
            self._connected = False
            return False

        self._connected = True
        logger.info("[mcp] %d/%d server 已连接", success_count, len(self._server_configs))
        return True

    async def disconnect(self) -> None:
        """断开所有连接。"""
        for name in list(self._sessions.keys()):
            try:
                await self._sessions[name].send_notification({"jsonrpc": "2.0", "method": "notifications/shutdown"})
            except Exception:
                pass
        self._sessions.clear()
        # 清理传输层 context managers
        for name, cm in list(self._transport_cms.items()):
            try:
                await cm.__aexit__(None, None, None)
            except Exception:
                pass
        self._transport_cms.clear()
        # 清理 streamable-http 上下文管理器（与 transport_cms 同理）
        for name, cm in list(self._http_cms.items()):
            try:
                await cm.__aexit__(None, None, None)
            except Exception:
                pass
        self._http_cms.clear()
        self._server_caps.clear()
        self._connected = False
        # 断开后台客户端
        if self._bg_client:
            try:
                await self._bg_client.disconnect()
            except Exception:
                pass
            self._bg_client = None

    # ====== 工具发现 ======

    async def get_tools(self) -> List:
        """获取所有 MCP server 的工具列表（MCPToolAdapter）。"""
        from capabilities.mcp.adapter import MCPToolAdapter

        if self._from_cache:
            return self._build_tools_from_cache()

        async def _get_one(name: str):
            if name not in self._sessions:
                return name, [], []
            try:
                result = await self._sessions[name].list_tools()
                raw_schemas = [
                    {"name": t.name, "description": t.description, "inputSchema": t.inputSchema} for t in result.tools
                ]
            except Exception as e:
                logger.warning("[mcp] %s tools/list 失败: %s", name, e)
                return name, [], []

            filtered = self._filter_tools(name, raw_schemas)
            adapters = []
            for schema in filtered:
                try:
                    adapters.append(
                        MCPToolAdapter(
                            mcp_client=self,
                            server_name=name,
                            tool_schema=schema,
                        )
                    )
                except Exception as e:
                    logger.warning("[mcp] %s 工具 %s 包装失败: %s", name, schema.get("name", "?"), e)
            logger.info("[mcp] %s: %d/%d tools injected", name, len(filtered), len(raw_schemas))
            return name, raw_schemas, adapters

        results = await asyncio.gather(*[_get_one(name) for name in self._sessions])
        all_tools = []
        servers_data = {}
        for name, raw_schemas, adapters in results:
            all_tools.extend(adapters)
            servers_data[name] = {"tools": raw_schemas}

        # 写缓存（仅在初始连接后写入）
        if not self._from_cache and servers_data:
            _write_cache(self._config_hash, servers_data)

        self._apply_conflict_resolution(all_tools)
        return all_tools

    def _build_tools_from_cache(self) -> List:
        """从缓存恢复工具适配器。"""
        from capabilities.mcp.adapter import MCPToolAdapter

        all_adapters = []
        for server_name, srv_data in self._cached_servers.items():
            for raw in srv_data.get("tools", []):
                try:
                    adapter = MCPToolAdapter(
                        mcp_client=self,
                        server_name=server_name,
                        tool_schema=raw,
                    )
                    all_adapters.append(adapter)
                except Exception as e:
                    logger.warning("[mcp] %s 工具 %s 缓存恢复失败: %s", server_name, raw.get("name", "?"), e)
        self._apply_conflict_resolution(all_adapters)
        return all_adapters

    @staticmethod
    def _apply_conflict_resolution(adapters: List) -> None:
        from collections import Counter

        names = [a.name for a in adapters]
        conflicts = {n for n, c in Counter(names).items() if c > 1}
        if not conflicts:
            return
        for a in adapters:
            if a.name in conflicts:
                a._resolve_conflict()

    async def refresh_tools(self) -> int:
        """强制刷新工具列表（忽略缓存，直接从 MCP server 重新获取）。"""
        if self._bg_client is not None:
            tools = await self._bg_client.get_tools()
        else:
            was_cached = self._from_cache
            self._from_cache = False
            try:
                tools = await self.get_tools()
            finally:
                self._from_cache = was_cached
        return len(tools)

    # ====== 工具调用 ======

    async def call_tool(
        self,
        server_name: str,
        tool_name: str,
        arguments: Dict[str, Any],
    ) -> "ToolResult":  # noqa: F821
        """调用指定 MCP server 的工具。"""
        from capabilities.result import ToolResult

        # 缓存模式：委托给后台客户端
        if self._from_cache:
            if not self._bg_ready.is_set():
                logger.info("[mcp] 等待后台连接完成（%s/%s）...", server_name, tool_name)
                try:
                    timeout = _PER_SERVER_TIMEOUT * len(self._server_configs)
                    await asyncio.wait_for(self._bg_ready.wait(), timeout=timeout)
                except asyncio.TimeoutError:
                    return ToolResult(
                        status="error",
                        content="",
                        error=f"MCP server '{server_name}' 后台连接超时，请稍后重试",
                        error_type="timeout",
                    )
            if self._bg_client:
                return await self._bg_client.call_tool(server_name, tool_name, arguments)
            return ToolResult(
                status="error",
                content="",
                error=f"MCP server '{server_name}' 后台连接失败",
                error_type="unknown",
            )

        session = self._sessions.get(server_name)
        if session is None:
            return ToolResult(
                status="error",
                content="",
                error=f"MCP server '{server_name}' 未连接",
                error_type="unknown",
            )

        cfg = self._server_configs.get(server_name)
        timeout = _CALL_TIMEOUT
        if cfg is not None:
            timeout = float(getattr(cfg, "timeout", _CALL_TIMEOUT) or _CALL_TIMEOUT)

        try:
            result = await asyncio.wait_for(
                session.call_tool(tool_name, arguments),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            return ToolResult(
                status="error",
                content="",
                error=f"MCP 调用超时 ({timeout}s): {server_name}/{tool_name}",
                error_type="timeout",
            )
        except (ConnectionResetError, BrokenPipeError, EOFError, ConnectionRefusedError, ConnectionError) as e:
            # 连接已彻底断开 → 移除该 server 的所有工具，避免 LLM 反复尝试
            self._remove_server_tools(server_name, e)
            return ToolResult(
                status="error",
                content="",
                error=f"MCP server '{server_name}' 连接断开，已移除其工具: {e}",
                error_type="connection_lost",
            )
        except Exception as e:
            logger.error("[mcp] call_tool %s/%s 异常: %s", server_name, tool_name, e)
            return ToolResult(
                status="error",
                content="",
                error=f"MCP 调用失败 ({server_name}/{tool_name}): {type(e).__name__}: {e}",
                error_type="mcp_error",
            )

        # SDK 返回 CallToolResult，其 content 是 list[TextContent|ImageContent|...]
        parts = []
        for item in result.content or []:
            if hasattr(item, "text"):
                parts.append(item.text)
            elif item.type == "image":
                parts.append(f"[image: {item.mimeType or '?'}]")
            elif item.type == "resource":
                parts.append(f"[resource: {item.uri or '?'}]")
            else:
                parts.append(str(item))
        content = "\n".join(parts)

        return ToolResult(
            status="success",
            content=content,
            data={"server": server_name, "tool": tool_name},
        )

    def set_tool_executor(self, tool_executor: Any) -> None:
        """注入 ToolExecutor 引用，用于断连时自动移除失效工具。"""
        self._tool_executor = tool_executor

    def _remove_server_tools(self, server_name: str, error: Exception) -> None:
        """移除指定 MCP server 的所有工具适配器（从 ToolExecutor 中剔除）。"""
        if self._tool_executor is None:
            return
        prefix = f"mcp__{server_name}__"
        removed = []
        for name in list(self._tool_executor._tools.keys()):
            if name.startswith(prefix):
                self._tool_executor._tools.pop(name, None)
                removed.append(name)
        if removed:
            logger.warning(
                "[mcp] %s 连接断开 (%s: %s)，已从 ToolExecutor 移除 %d 个工具: %s",
                server_name,
                type(error).__name__,
                error,
                len(removed),
                ", ".join(removed),
            )
        else:
            logger.warning(
                "[mcp] %s 连接断开 (%s: %s)，但没有找到对应的注册工具",
                server_name,
                type(error).__name__,
                error,
            )

    # ====== 重连 ======

    async def reconnect(self, server_name: str) -> Dict[str, Any]:
        """重新连接指定 MCP server 并恢复 ToolExecutor 中的工具。

        Returns:
            {"server": str, "reconnected": bool, "tools_restored": int, "error": str|None}
        """
        from capabilities.mcp.adapter import MCPToolAdapter

        result: Dict[str, Any] = {
            "server": server_name,
            "reconnected": False,
            "tools_restored": 0,
            "error": None,
        }

        if server_name not in self._server_configs:
            result["error"] = f"未知 server: {server_name}（可用: {', '.join(self._server_configs.keys())}）"
            return result

        cfg = self._server_configs[server_name]

        # 1. 断开旧连接（session + 传输层 context manager）
        old_session = self._sessions.pop(server_name, None)
        if old_session:
            try:
                await old_session.send_notification({"jsonrpc": "2.0", "method": "notifications/shutdown"})
            except Exception:
                pass

        old_cm = self._transport_cms.pop(server_name, None)
        if old_cm:
            try:
                await old_cm.__aexit__(None, None, None)
            except Exception:
                pass

        old_http_cm = self._http_cms.pop(server_name, None)
        if old_http_cm:
            try:
                await old_http_cm.__aexit__(None, None, None)
            except Exception:
                pass

        self._server_caps.pop(server_name, None)

        # 2. 重新连接
        try:
            await self._connect_one(server_name, cfg)
            result["reconnected"] = True
        except Exception as e:
            result["error"] = f"重连失败: {e}"
            logger.error("[mcp] %s 重连失败: %s", server_name, e)
            return result

        # 3. 重新发现工具
        try:
            session = self._sessions[server_name]
            list_result = await session.list_tools()
            raw_schemas = [
                {"name": t.name, "description": t.description, "inputSchema": t.inputSchema} for t in list_result.tools
            ]
            filtered = self._filter_tools(server_name, raw_schemas)

            adapters = []
            for schema in filtered:
                try:
                    adapter = MCPToolAdapter(
                        mcp_client=self,
                        server_name=server_name,
                        tool_schema=schema,
                    )
                    adapters.append(adapter)
                except Exception as e:
                    logger.warning(
                        "[mcp] %s 工具 %s 包装失败: %s",
                        server_name,
                        schema.get("name", "?"),
                        e,
                    )
        except Exception as e:
            logger.error("[mcp] %s 工具发现失败: %s", server_name, e)
            # 连接成功但工具发现失败，至少 session 恢复了
            result["error"] = f"已连接但工具发现失败: {e}"
            return result

        # 4. 更新 ToolExecutor：先移除旧工具，再注册新工具
        if self._tool_executor:
            prefix = f"mcp__{server_name}__"
            removed = []
            for name in list(self._tool_executor._tools.keys()):
                if name.startswith(prefix):
                    self._tool_executor._tools.pop(name, None)
                    removed.append(name)
            if removed:
                logger.info(
                    "[mcp] 重连: 移除 %d 个旧工具: %s",
                    len(removed),
                    ", ".join(removed),
                )
            for adapter in adapters:
                self._tool_executor.register(adapter)

        result["tools_restored"] = len(adapters)
        logger.info(
            "[mcp] %s 重连成功，恢复 %d 个工具",
            server_name,
            len(adapters),
        )

        return result

    # ====== 后台连接 ======

    @property
    def from_cache(self) -> bool:
        return self._from_cache

    async def background_connect(self, tool_executor: Any) -> None:
        """后台连接所有 MCP server 并更新 ToolExecutor 中的工具。"""
        logger.info("[mcp] 后台连接开始...")
        try:
            bg = MCPClient(self._server_configs)
            # 绕过缓存，强制走真实连接
            bg._from_cache = False
            bg.set_tool_executor(tool_executor)
            for name, cfg in self._server_configs.items():
                try:
                    await asyncio.wait_for(
                        bg._connect_one(name, cfg),
                        timeout=_PER_SERVER_TIMEOUT,
                    )
                except asyncio.TimeoutError:
                    logger.error("[mcp] 后台连接 %s 超时（%ss）", name, _PER_SERVER_TIMEOUT)
                except Exception as e:
                    logger.error("[mcp] 后台连接 %s 失败: %s", name, e)
            fresh_tools = await bg.get_tools()
        except Exception as e:
            logger.error("[mcp] 后台连接失败: %s", e)
            self._bg_ready.set()
            return

        new_map = {t.name: t for t in fresh_tools}
        old_names = set(tool_executor._tools.keys())
        new_names = set(new_map.keys())

        for name in new_names:
            tool_executor._tools[name] = new_map[name]
        removed = {n for n in old_names - new_names if n.startswith("mcp__")}
        for name in removed:
            tool_executor._tools.pop(name, None)
            logger.info("[mcp] 后台刷新: 移除工具 %s", name)

        added = new_names - old_names
        if added:
            logger.info("[mcp] 后台刷新: 新增工具 %s", ", ".join(sorted(added)))

        self._bg_client = bg
        self._bg_ready.set()
        logger.info("[mcp] 后台连接完成: %d tools (新增 %d, 移除 %d)", len(fresh_tools), len(added), len(removed))

    # ====== 过滤逻辑 ======

    def _filter_tools(self, server_name: str, tools: List[Dict]) -> List[Dict]:
        cfg = self._server_configs.get(server_name)
        if cfg is None:
            return tools
        include = getattr(cfg, "include", None)
        exclude = getattr(cfg, "exclude", None)

        if include is not None and exclude is not None:
            raise MCPClientError(f"[mcp] {server_name}: include 和 exclude 不能同时配置")
        all_names = {t["name"] for t in tools}
        if include is not None:
            missing = set(include) - all_names
            if missing:
                logger.warning("[mcp] %s: include 指定的工具不存在: %s", server_name, missing)
            return [t for t in tools if t["name"] in include]
        if exclude is not None:
            unknown = set(exclude) - all_names
            if unknown:
                logger.warning("[mcp] %s: exclude 指定的工具不存在: %s", server_name, unknown)
            return [t for t in tools if t["name"] not in exclude]
        return tools

    # ====== 内部：SDK 连接管理 ======

    async def _connect_one(self, name: str, cfg: Any) -> None:
        from mcp import ClientSession

        transport = getattr(cfg, "transport", "stdio")

        if transport == "sse":
            from mcp.client.sse import sse_client

            url = getattr(cfg, "url", None)
            if not url:
                raise MCPClientError(f"[mcp] {name}: SSE 模式需要配置 url")

            headers = dict(getattr(cfg, "env", {}))
            logger.info("[mcp] %s: 连接 SSE %s", name, url)
            cm = sse_client(url=url, headers=headers)
            self._transport_cms[name] = cm
            read, write = await asyncio.wait_for(cm.__aenter__(), timeout=_SPAWN_TIMEOUT)
            session = await ClientSession(read, write).__aenter__()
            await asyncio.wait_for(session.initialize(), timeout=_CONNECT_TIMEOUT)
            self._sessions[name] = session
            self._server_caps[name] = session.get_server_capabilities() or {}
            logger.info("[mcp] %s: 已连接（SSE）", name)
            return

        if transport == "streamable-http":
            import httpx
            from mcp.client.streamable_http import streamable_http_client

            url = getattr(cfg, "url", None)
            if not url:
                raise MCPClientError(f"[mcp] {name}: streamable-http 模式需要配置 url")

            headers = dict(getattr(cfg, "env", {}))
            http_client = httpx.AsyncClient(headers=headers) if headers else None

            logger.info("[mcp] %s: 连接 streamable-http %s", name, url)

            # 用 async with 避免 anyio 任务组竞态
            cm = streamable_http_client(url=url, http_client=http_client)
            self._http_cms[name] = cm  # 保存引用，断开时清理
            read, write, _get_sid = await asyncio.wait_for(cm.__aenter__(), timeout=_SPAWN_TIMEOUT)
            session = await ClientSession(read, write).__aenter__()
            await asyncio.wait_for(session.initialize(), timeout=_CONNECT_TIMEOUT)
            self._sessions[name] = session
            self._server_caps[name] = session.get_server_capabilities() or {}
            logger.info("[mcp] %s: 已连接（streamable-http）", name)
            return

        # stdio: 使用 SDK 的 stdio_client 管理子进程
        from mcp.client.stdio import StdioServerParameters, stdio_client

        command = getattr(cfg, "command", "")
        args = getattr(cfg, "args", [])
        env_vars = dict(getattr(cfg, "env", {}))

        logger.info("[mcp] %s: 启动子进程 %s %s", name, command, " ".join(args))

        server_params = StdioServerParameters(
            command=command,
            args=args,
            env=env_vars if env_vars else None,
        )
        cm = stdio_client(server_params, errlog=open(os.devnull, "w"))
        self._transport_cms[name] = cm
        read, write = await asyncio.wait_for(cm.__aenter__(), timeout=_SPAWN_TIMEOUT)
        session = await ClientSession(read, write).__aenter__()
        await asyncio.wait_for(session.initialize(), timeout=_CONNECT_TIMEOUT)
        self._sessions[name] = session
        self._server_caps[name] = session.get_server_capabilities() or {}
        logger.info("[mcp] %s: 已连接（stdio）", name)

    def _force_kill_all_sync(self) -> None:
        """同步强制终止所有连接（信号处理器路径，无 async/await）。

        stdio/SSE transport 由 SDK 的 anyio 管理子进程生命周期，
        此方法无法在同步上下文中执行异步清理。它只清空引用，
        实际子进程终止依赖 SDK 内部的进程组管理。
        """
        self._sessions.clear()
        self._transport_cms.clear()
        self._http_cms.clear()
        self._server_caps.clear()

    # ====== 属性 ======

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def server_names(self) -> List[str]:
        return list(self._server_configs.keys())

    def get_server_tools_raw(self, name: str) -> Optional[List[Dict]]:
        return None
