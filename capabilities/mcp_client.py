"""
capabilities/mcp_client.py
MCPClient — MCP 协议客户端，通过 stdio 与外部 MCP server 通信。

职责:
  1. 启停子进程（stdin/stdout NDJSON）
  2. JSON-RPC 2.0 通信（initialize / tools/list / tools/call / shutdown）
  3. 工具发现 + include/exclude 过滤 → 返回 MCPToolAdapter 列表
  4. 进程生命周期管理（超时、优雅关闭、强制 kill）

参考: MCP 协议规范 (modelcontextprotocol.io)
"""

import asyncio
import json
import logging
from typing import Any, Dict, List, Optional, Tuple

from capabilities.result import ToolResult

logger = logging.getLogger(__name__)

# 进程生命周期常量
_CONNECT_TIMEOUT = 15.0       # initialize 超时（秒）
_CALL_TIMEOUT = 60.0          # tools/call 超时（秒）
_FORCE_KILL_TIMEOUT = 2.0     # 强制 kill 等待（秒）


class MCPClientError(Exception):
    """MCP 客户端错误"""


class MCPClient:
    """MCP 协议客户端。

    管理多个 MCP server 的子进程，对外暴露 get_tools() / call_tool()。
    """

    def __init__(self, server_configs: Optional[Dict[str, Any]] = None):
        self._server_configs: Dict[str, Any] = server_configs or {}
        self._processes: Dict[str, asyncio.subprocess.Process] = {}
        self._readers: Dict[str, asyncio.StreamReader] = {}
        self._writers: Dict[str, asyncio.StreamWriter] = {}
        self._server_caps: Dict[str, Dict[str, Any]] = {}  # initialize 返回的 capabilities
        self._connected = False

    # ====== 生命周期 ======

    async def connect(self) -> bool:
        """连接所有 MCP server（启动子进程 + initialize handshake）。

        Returns:
            True 如果至少一个 server 连接成功。
        """
        if not self._server_configs:
            logger.info("[mcp] 没有配置 MCP server，跳过")
            self._connected = True
            return True

        success_count = 0
        for name, cfg in self._server_configs.items():
            try:
                await self._connect_one(name, cfg)
                success_count += 1
            except Exception as e:
                logger.error("[mcp] %s 连接失败: %s", name, e)

        if success_count == 0 and self._server_configs:
            logger.warning("[mcp] 所有 server 连接均失败")
            self._connected = False
            return False

        self._connected = True
        logger.info("[mcp] %d/%d server 已连接", success_count, len(self._server_configs))
        return True

    async def disconnect(self) -> None:
        """断开所有连接：发送 shutdown 通知 → 等待退出 → 超时 force kill。"""
        for name in list(self._processes.keys()):
            try:
                await self._disconnect_one(name)
            except Exception:
                pass
        self._processes.clear()
        self._readers.clear()
        self._writers.clear()
        self._server_caps.clear()
        self._connected = False

    # ====== 工具发现 ======

    async def get_tools(self) -> List:
        """获取所有 MCP server 的工具列表（MCPToolAdapter）。

        对每个已连接 server 发送 tools/list，应用 include/exclude 过滤后
        包装为 MCPToolAdapter 返回。
        """
        from capabilities.mcp.adapter import MCPToolAdapter

        all_tools: List[MCPToolAdapter] = []

        for name in self._processes:
            if name not in self._writers:
                continue
            try:
                raw_tools = await self._list_tools(name)
            except Exception as e:
                logger.warning("[mcp] %s tools/list 失败: %s", name, e)
                continue

            # 应用 include/exclude 过滤
            filtered = self._filter_tools(name, raw_tools)

            # 包装
            for tool_schema in filtered:
                try:
                    adapter = MCPToolAdapter(
                        mcp_client=self,
                        server_name=name,
                        tool_schema=tool_schema,
                    )
                    all_tools.append(adapter)
                except Exception as e:
                    logger.warning("[mcp] %s 工具 %s 包装失败: %s",
                                   name, tool_schema.get("name", "?"), e)

            logger.info(
                "[mcp] %s: %d/%d tools injected",
                name, len(filtered), len(raw_tools),
            )

        return all_tools

    async def refresh_tools(self) -> int:
        """强制刷新工具列表（重新调用 tools/list）。

        Returns:
            刷新的工具总数。
        """
        tools = await self.get_tools()
        return len(tools)

    # ====== 工具调用 ======

    async def call_tool(
        self,
        server_name: str,
        tool_name: str,
        arguments: Dict[str, Any],
    ) -> ToolResult:
        """调用指定 MCP server 的工具。

        Args:
            server_name: MCP server 标识名
            tool_name: 工具原名（不含 mcp__ 前缀）
            arguments: 工具参数

        Returns:
            ToolResult 统一结果。
        """
        if server_name not in self._writers:
            # 尝试重连
            if not await self._ensure_alive(server_name):
                return ToolResult(
                    status="error",
                    content="",
                    error=f"MCP server '{server_name}' 未连接",
                    error_type="unknown",
                )

        request = {
            "jsonrpc": "2.0",
            "id": f"call_{tool_name}_{id(arguments)}",
            "method": "tools/call",
            "params": {
                "name": tool_name,
                "arguments": arguments,
            },
        }

        try:
            response = await asyncio.wait_for(
                self._send_and_receive(server_name, request),
                timeout=_CALL_TIMEOUT,
            )
        except asyncio.TimeoutError:
            return ToolResult(
                status="error",
                content="",
                error=f"MCP 调用超时 ({_CALL_TIMEOUT}s): {server_name}/{tool_name}",
                error_type="timeout",
            )

        if "error" in response:
            err = response["error"]
            return ToolResult(
                status="error",
                content="",
                error=f"MCP error [{err.get('code', '?')}]: {err.get('message', '')}",
                error_type="unknown",
            )

        result = response.get("result", {})
        content = self._extract_content(result)

        return ToolResult(
            status="success",
            content=content,
            data={"server": server_name, "tool": tool_name, "raw_result": result},
        )

    # ====== 过滤逻辑 ======

    def _filter_tools(self, server_name: str, tools: List[Dict]) -> List[Dict]:
        """应用 include/exclude 过滤。

        规则:
          - include 和 exclude 互斥，同时配置 → 报错
          - include: 只保留白名单中的工具名
          - exclude: 排除黑名单中的工具名
          - 都不配 → 全量
        """
        cfg = self._server_configs.get(server_name)
        if cfg is None:
            return tools

        include = getattr(cfg, "include", None)
        exclude = getattr(cfg, "exclude", None)

        if include is not None and exclude is not None:
            raise MCPClientError(
                f"[mcp] {server_name}: include 和 exclude 不能同时配置"
            )

        all_names = {t["name"] for t in tools}

        if include is not None:
            missing = set(include) - all_names
            if missing:
                logger.warning(
                    "[mcp] %s: include 指定的工具不存在: %s", server_name, missing
                )
            return [t for t in tools if t["name"] in include]

        if exclude is not None:
            unknown = set(exclude) - all_names
            if unknown:
                logger.warning(
                    "[mcp] %s: exclude 指定的工具不存在: %s", server_name, unknown
                )
            return [t for t in tools if t["name"] not in exclude]

        return tools

    # ====== 内部：单个 server 连接管理 ======

    async def _connect_one(self, name: str, cfg: Any) -> None:
        """启动单个 MCP server 子进程并完成 initialize handshake。"""
        command = getattr(cfg, "command", "")
        args = getattr(cfg, "args", [])
        env_vars = getattr(cfg, "env", {})

        # 合并环境变量
        import os
        process_env = os.environ.copy()
        process_env.update(env_vars)

        logger.info("[mcp] %s: 启动子进程 %s %s", name, command, " ".join(args))

        process = await asyncio.create_subprocess_exec(
            command, *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=process_env,
        )

        self._processes[name] = process
        self._readers[name] = process.stdout
        self._writers[name] = process.stdin

        # MCP initialize handshake
        init_request = {
            "jsonrpc": "2.0",
            "id": f"init_{name}",
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {
                    "tools": {},
                },
                "clientInfo": {
                    "name": "ChachaAgent",
                    "version": "3.1.6",
                },
            },
        }

        try:
            response = await asyncio.wait_for(
                self._send_and_receive(name, init_request, _retry=False),
                timeout=_CONNECT_TIMEOUT,
            )
        except asyncio.TimeoutError:
            await self._kill_process(name)
            raise MCPClientError(f"[mcp] {name}: initialize 超时 ({_CONNECT_TIMEOUT}s)")

        if "error" in response:
            err = response["error"]
            await self._kill_process(name)
            raise MCPClientError(
                f"[mcp] {name}: initialize 失败 [{err.get('code', '?')}]: {err.get('message', '')}"
            )

        self._server_caps[name] = response.get("result", {}).get("capabilities", {})

        # 发送 initialized 通知（MCP 协议要求）
        initialized_notification = {
            "jsonrpc": "2.0",
            "method": "notifications/initialized",
            "params": {},
        }
        await self._send_notification(name, initialized_notification)

        logger.info("[mcp] %s: 已连接", name)

    async def _disconnect_one(self, name: str) -> None:
        """断开单个 server：直接 kill。"""
        process = self._processes.get(name)
        if process is None or process.returncode is not None:
            return

        await self._kill_process(name)

    async def _kill_process(self, name: str) -> None:
        """强制终止子进程。"""
        process = self._processes.get(name)
        if process is None or process.returncode is not None:
            return

        try:
            process.kill()
            await asyncio.wait_for(process.wait(), timeout=_FORCE_KILL_TIMEOUT)
            logger.warning("[mcp] %s: 已强制 kill", name)
        except asyncio.TimeoutError:
            logger.error("[mcp] %s: 强制 kill 超时", name)
        except ProcessLookupError:
            pass
        except Exception as e:
            logger.error("[mcp] %s: kill 异常: %s", name, e)

    # ====== 进程存活检查与自动重连 ======

    def _is_process_alive(self, name: str) -> bool:
        """检查子进程是否存活。"""
        process = self._processes.get(name)
        if process is None:
            return False
        return process.returncode is None

    def _cleanup_dead(self, name: str) -> None:
        """清理已死进程的残留资源。"""
        self._processes.pop(name, None)
        self._readers.pop(name, None)
        self._writers.pop(name, None)
        self._server_caps.pop(name, None)

    async def _ensure_alive(self, name: str) -> bool:
        """确保 server 子进程存活，已退出则自动重连。

        Returns:
            True 如果进程存活或重连成功。
        """
        if self._is_process_alive(name):
            return True

        cfg = self._server_configs.get(name)
        if cfg is None:
            logger.warning("[mcp] %s: 无法重连，无配置信息", name)
            return False

        rc = "N/A"
        proc = self._processes.get(name)
        if proc is not None:
            rc = str(proc.returncode)

        logger.warning(
            "[mcp] %s: 子进程已退出 (returncode=%s)，尝试重连...", name, rc
        )

        self._cleanup_dead(name)

        try:
            await self._connect_one(name, cfg)
            logger.info("[mcp] %s: 重连成功", name)
            return True
        except Exception as e:
            logger.error("[mcp] %s: 重连失败: %s", name, e)
            return False

    # ====== 内部：JSON-RPC 通信 ======

    async def _list_tools(self, name: str) -> List[Dict]:
        """发送 tools/list 请求，返回工具列表。"""
        request = {
            "jsonrpc": "2.0",
            "id": f"list_{name}",
            "method": "tools/list",
            "params": {},
        }
        response = await asyncio.wait_for(
            self._send_and_receive(name, request),
            timeout=_CALL_TIMEOUT,
        )

        if "error" in response:
            err = response["error"]
            raise MCPClientError(
                f"tools/list 失败 [{err.get('code', '?')}]: {err.get('message', '')}"
            )

        return response.get("result", {}).get("tools", [])

    async def _send_and_receive(
        self, name: str, request: Dict[str, Any], _retry: bool = True,
    ) -> Dict[str, Any]:
        """发送 JSON-RPC 请求并读取响应。断线时自动重连并重试一次。"""
        reader = self._readers.get(name)
        writer = self._writers.get(name)
        if writer is None or reader is None:
            raise MCPClientError(f"server '{name}' 未连接")

        # 发送（NDJSON: 一行 JSON）
        line = json.dumps(request, ensure_ascii=False) + "\n"
        writer.write(line.encode("utf-8"))
        await writer.drain()

        # 读取响应（一行 JSON）
        raw = await reader.readline()
        if not raw:
            if not _retry:
                raise MCPClientError(f"server '{name}' 连接已关闭")
            # 尝试重连后重试一次
            if await self._ensure_alive(name):
                reader = self._readers[name]
                writer = self._writers[name]
                logger.info("[mcp] %s: 已重连，重试请求", name)
                line = json.dumps(request, ensure_ascii=False) + "\n"
                writer.write(line.encode("utf-8"))
                await writer.drain()
                raw = await reader.readline()
                if not raw:
                    raise MCPClientError(
                        f"server '{name}' 连接已关闭（重连后仍失败）"
                    )
            else:
                raise MCPClientError(
                    f"server '{name}' 连接已关闭（重连失败）"
                )

        return json.loads(raw.decode("utf-8"))

    async def _send_notification(
        self, name: str, notification: Dict[str, Any]
    ) -> None:
        """发送 JSON-RPC 通知（无需响应）。"""
        writer = self._writers.get(name)
        if writer is None:
            return
        line = json.dumps(notification, ensure_ascii=False) + "\n"
        writer.write(line.encode("utf-8"))
        await writer.drain()

    # ====== 辅助 ======

    @staticmethod
    def _extract_content(result: Dict[str, Any]) -> str:
        """从 MCP tools/call 结果中提取文本内容。

        MCP 返回的 content 是一个 list，每个元素有 type 字段。
        """
        content_items = result.get("content", [])
        if not content_items:
            return json.dumps(result, ensure_ascii=False)

        parts = []
        for item in content_items:
            item_type = item.get("type", "text")
            if item_type == "text":
                parts.append(item.get("text", ""))
            elif item_type == "image":
                parts.append(f"[image: {item.get('mimeType', '?')}]")
            elif item_type == "resource":
                parts.append(f"[resource: {item.get('uri', '?')}]")
            else:
                parts.append(json.dumps(item, ensure_ascii=False))

        return "\n".join(parts)

    # ====== 属性 ======

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def server_names(self) -> List[str]:
        return list(self._server_configs.keys())

    def get_server_tools_raw(self, name: str) -> Optional[List[Dict]]:
        """获取某个 server 的原始工具列表（供 CLI list-tools 使用）。

        注意：这是同步方法，需在 connect + get_tools 之后调用。
        """
        # get_tools 会缓存吗？当前不缓存，每次都重新获取。
        # 这里返回 None 表示需要异步获取。
        return None
