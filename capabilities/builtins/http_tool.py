"""
capabilities/builtins/http_tool.py
HttpTool — HTTP 请求工具（BaseTool）。

用法:
  http_request(method, url, headers?, body?, timeout?)
"""

import asyncio
import ipaddress
import json
import logging
import socket
from typing import Any, Dict, Optional
from urllib.parse import urlparse

import httpx

from capabilities.base import BaseTool

logger = logging.getLogger(__name__)

MAX_RESPONSE_CHARS = 50_000

# Rate limiter + shared httpx client
_RATE_LIMITER = asyncio.Semaphore(5)
_HTTP_CLIENT: Optional[httpx.AsyncClient] = None

async def _get_client() -> httpx.AsyncClient:
    global _HTTP_CLIENT
    if _HTTP_CLIENT is None:
        _HTTP_CLIENT = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0),
            limits=httpx.Limits(max_keepalive_connections=10),
            headers={"User-Agent": "ChachaAgent/2.0"},
        )
    return _HTTP_CLIENT

# ====== SSRF 防护 ======

_SSRF_BLOCKED_NETWORKS = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
]
_SSRF_ALLOWED_HOSTS: set = set()


def _is_private_ip(host: str) -> bool:
    if host in _SSRF_ALLOWED_HOSTS:
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return any(ip in net for net in _SSRF_BLOCKED_NETWORKS)


class HttpTool(BaseTool):
    """HTTP 请求：http_request(method, url, ...)"""

    name = "http_request"
    description = "发送 HTTP 请求。支持 GET/POST/PUT/DELETE，可用于调用外部 API。"
    parameters = {
        "type": "object",
        "properties": {
            "method": {"type": "string", "description": "HTTP 方法: GET/POST/PUT/DELETE"},
            "url": {"type": "string", "description": "请求 URL"},
            "headers": {"type": "object", "description": "自定义请求头（JSON 对象）"},
            "body": {"type": "string", "description": "请求体（POST/PUT 使用）"},
            "timeout": {"type": "number", "description": "超时秒数，默认 30"},
        },
        "required": ["method", "url"],
    }
    risk = "medium"
    requires_approval = True

    async def execute(
        self,
        method: str,
        url: str,
        headers: Optional[Dict[str, str]] = None,
        body: str = "",
        timeout: float = 30,
    ) -> str:
        method = method.upper()
        if method not in ("GET", "POST", "PUT", "DELETE"):
            return f"[错误] 不支持的 HTTP 方法: {method}"

        # 只允许 http/https
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return f"[错误] 不支持的协议: {parsed.scheme}"

        # SSRF: DNS resolve + check target IP
        hostname = parsed.hostname or ""
        if hostname:
            try:
                resolved_ip = socket.getaddrinfo(hostname, None, socket.AF_UNSPEC)[0][4][0]
            except (socket.gaierror, IndexError):
                resolved_ip = hostname
            if _is_private_ip(resolved_ip):
                return f"[错误] SSRF 防护：禁止访问内网地址 ({resolved_ip})"

        try:
            async with _RATE_LIMITER:
                client = await _get_client()
                resp = await client.request(
                    method=method,
                    url=url,
                    headers=headers,
                    content=body.encode("utf-8") if body else None,
                    timeout=timeout,
                )

            content = resp.text
            if len(content) > MAX_RESPONSE_CHARS:
                content = content[:MAX_RESPONSE_CHARS] + "\n... [response truncated]"

            return f"HTTP {resp.status_code}\n{content}"

        except httpx.HTTPStatusError as e:
            body_text = e.response.text[:2000] if e.response else ""
            return f"[HTTP {e.response.status_code}] {e.response.reason_phrase}\n{body_text}"
        except httpx.TimeoutException:
            return f"[Error] Request timeout ({timeout}s)"
        except httpx.ConnectError as e:
            return f"[Error] Connection failed: {e}"
        except Exception as e:
            return f"[Error] {type(e).__name__}: {e}"
