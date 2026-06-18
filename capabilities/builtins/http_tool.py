"""
capabilities/builtins/http_tool.py
HttpTool — HTTP 请求工具（BaseTool）。

用法:
  http_request(method, url, headers?, body?, timeout?)
"""

import logging
from typing import Any, Dict, Optional
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
import json

from capabilities.base import BaseTool

logger = logging.getLogger(__name__)

MAX_RESPONSE_CHARS = 50_000


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

        try:
            req = Request(url, method=method)
            req.add_header("User-Agent", "ChachaAgent/1.0")

            if headers:
                for k, v in (headers or {}).items():
                    req.add_header(k, v)

            if body and method in ("POST", "PUT"):
                req.data = body.encode("utf-8")
                if not headers or "Content-Type" not in headers:
                    req.add_header("Content-Type", "application/json")

            resp = urlopen(req, timeout=timeout)
            content = resp.read().decode("utf-8", errors="replace")

            if len(content) > MAX_RESPONSE_CHARS:
                content = content[:MAX_RESPONSE_CHARS] + "\n... [响应截断]"

            return f"HTTP {resp.status}\n{content}"

        except HTTPError as e:
            body_text = e.read().decode("utf-8", errors="replace")[:2000] if e.fp else ""
            return f"[HTTP {e.code}] {e.reason}\n{body_text}"
        except URLError as e:
            return f"[错误] 连接失败: {e.reason}"
        except Exception as e:
            return f"[错误] {type(e).__name__}: {e}"
