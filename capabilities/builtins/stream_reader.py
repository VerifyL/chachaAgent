"""
capabilities/builtins/stream_reader.py
StreamIndex — mmap 行首字节索引，按行号偏移读取文件。

核心设计：
- 首次读文件时 mmap 遍历 \\n 建索引，之后 O(1) 查表
- 索引会话级缓存（进程死亡释放），不落盘
- offset=行号（1-based），limit=行数，next_offset=LLM 盲传
- 与 Claude Code read_file 语义完全对齐

用法:
    idx = _ensure_index(path)           # 建/取索引
    result = read_by_offset(路径, offset=1, limit=100)  # 按行号读
"""

import json
import logging
import mmap
import os
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# ====== 配置 ======

MAX_OUTPUT_CHARS = 100_000          # 单次输出字符上限
MAX_FILE_SIZE = 10 * 1024 * 1024   # 10MB，超大文件拒绝

# 二进制扩展名黑名单
_BINARY_EXTS = frozenset({
    ".pyc", ".so", ".dll", ".dylib", ".exe",
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".bmp", ".svg",
    ".zip", ".tar", ".gz", ".bz2", ".xz", ".7z", ".rar",
    ".bin", ".dat", ".db", ".sqlite", ".o", ".a", ".lib",
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".mp3", ".mp4", ".avi", ".mov", ".wav", ".flac",
    ".ttf", ".otf", ".woff", ".woff2", ".eot",
})

# ====== 行号索引缓存（会话级，带 mtime 校验） ======

# path:mtime → [0, 42, 89, ...]
_line_index: Dict[str, List[int]] = {}
# 定时清理计数器
_cleanup_counter = 0
# 写入后待强制重建的路径集合（消除 _invalidate→rename 之间的窗口期）
_pending_rebuild: set = set()


def _ensure_index(path: str) -> List[int]:
    """获取文件的行首字节偏移索引。
    
    缓存 key = ``{abspath}::{mtime}``，文件外部修改后自动重建。
    """
    global _cleanup_counter
    abspath = os.path.abspath(path)
    try:
        mtime = os.path.getmtime(abspath)
    except OSError:
        return [0]

    # 写入后强制重建：跳过缓存
    key = f"{abspath}::{mtime}"
    if abspath in _pending_rebuild:
        _pending_rebuild.discard(abspath)
    elif key in _line_index:
        return _line_index[key]

    offsets = [0]
    try:
        with open(abspath, "rb") as f:
            if f.seek(0, 2) == 0:
                _line_index[key] = offsets
                return offsets
            mm = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
            try:
                for idx in range(len(mm)):
                    if mm[idx] == 0x0A:
                        offsets.append(idx + 1)
            finally:
                mm.close()
    except (OSError, ValueError) as e:
        logger.warning("mmap 索引失败: %s → %s", abspath, e)
        raise

    # 添加新条目前清理过期缓存
    _cleanup_counter += 1
    if _cleanup_counter >= 50:
        _cleanup_counter = 0
        _clean_stale(abspath)

    _line_index[key] = offsets
    return offsets


def _invalidate(path: str) -> None:
    """使指定文件的索引失效（文件被修改后调用，下次读取自动重建）。"""
    abspath = os.path.abspath(path)
    # 删除所有该路径的缓存（不同 mtime 的条目）
    keys = [k for k in _line_index if k.startswith(abspath + "::")]
    for k in keys:
        _line_index.pop(k, None)


def _mark_for_rebuild(path: str) -> None:
    """标记路径待强制重建索引（消除 _invalidate→rename 之间的窗口期）。
    
    AtomicWriter 在写入完成后调用此函数，确保下次 _ensure_index 
    无条件重建，避免 APFS rename 后 mtime 传播延迟导致的空读。
    """
    _pending_rebuild.add(os.path.abspath(path))
def _clean_stale(exclude_path: str) -> None:
    """清除非当前文件的过期缓存条目（保留 200 条）。"""
    if len(_line_index) <= 200:
        return
    # 保留当前文件 + 最近的 199 条
    current_keys = [k for k in _line_index if k.startswith(exclude_path + "::")]
    other_keys = sorted(
        [k for k in _line_index if not k.startswith(exclude_path + "::")],
        reverse=True,
    )[:199]
    keep = set(current_keys + other_keys)
    for k in list(_line_index.keys()):
        if k not in keep:
            _line_index.pop(k, None)


def read_by_offset(
    path: str,
    offset: int = 1,
    limit: int = 100,
    root: Optional[Path] = None,
) -> str:
    """按行号偏移读取文件，返回 JSON 字符串。

    Args:
        path: 文件路径（相对或绝对）
        offset: 起始行号（1-based），默认 1
        limit: 最大读取行数，默认 100
        root: 项目根目录（用于路径解析和 containment 检查）

    Returns:
        JSON:
        {
            "file": str,
            "offset": int,          ← 本次起始行号
            "lines_read": int,      ← 实际读到的行数
            "next_offset": int,     ← LLM 下次盲传
            "total_lines": int,
            "total_bytes": int,
            "has_more": bool,
            "truncated": bool,
            "content": str
        }
    """
    root = root or Path.cwd().resolve()

    # 1. 路径解析
    raw = (Path(path).resolve() if Path(path).is_absolute()
           else (root / path).resolve())
    try:
        raw.relative_to(root)
    except ValueError:
        return json.dumps(
            {"error": "access_denied", "message": "路径超出项目根目录"},
            ensure_ascii=False,
        )

    if not raw.exists():
        return json.dumps(
            {"error": "not_found", "message": f"文件不存在: {path}"},
            ensure_ascii=False,
        )
    if not raw.is_file():
        return json.dumps(
            {"error": "not_a_file", "message": f"不是文件: {path}"},
            ensure_ascii=False,
        )

    # 2. 大小检查
    try:
        fsize = raw.stat().st_size
    except OSError as e:
        return json.dumps(
            {"error": "access_error", "message": str(e)},
            ensure_ascii=False,
        )
    if fsize > MAX_FILE_SIZE:
        return json.dumps(
            {
                "error": "file_too_large",
                "file": str(raw.relative_to(root)),
                "size_mb": round(fsize / 1024 / 1024, 1),
                "message": "文件过大，请用 grep 搜索关键词定位，或用 offset 参数分页读取",
            },
            ensure_ascii=False,
        )

    # 3. 二进制检测
    if raw.suffix.lower() in _BINARY_EXTS:
        return json.dumps(
            {"error": "binary_file", "message": f"二进制文件: {path}"},
            ensure_ascii=False,
        )

    # 4. 建/取索引
    try:
        idx = _ensure_index(str(raw))
    except (OSError, ValueError) as e:
        return json.dumps(
            {"error": "read_error", "message": str(e)},
            ensure_ascii=False,
        )

    # total_lines = 索引项数（空文件 1 项 [0]，0 行）
    total_lines = len(idx)
    # 空文件特殊处理
    if total_lines <= 1 and fsize == 0:
        rel = str(raw.relative_to(root) if raw.is_relative_to(root) else raw)
        return json.dumps({
            "file": rel,
            "offset": 1,
            "lines_read": 0,
            "next_offset": 1,
            "total_lines": 0,
            "total_bytes": 0,
            "has_more": False,
            "truncated": False,
            "content": "",
        }, ensure_ascii=False)

    # 5. 行号 → 字节范围
    start_idx = max(0, offset - 1)
    if start_idx >= len(idx):
        # offset 超出文件末尾 → 显式报错，避免 LLM 盲猜
        rel = str(raw.relative_to(root) if raw.is_relative_to(root) else raw)
        return json.dumps({
            "error": "offset_out_of_range",
            "message": f"offset {offset} 超出文件行数范围（共 {total_lines} 行），请调小 offset",
            "file": rel,
            "total_lines": total_lines,
            "total_bytes": fsize,
        }, ensure_ascii=False)

    byte_start = idx[start_idx]

    end_idx_pos = start_idx + limit
    if end_idx_pos >= len(idx):
        # 读到文件末尾
        byte_end = fsize
        next_offset = offset + (len(idx) - start_idx - 1)
        has_more = False
    else:
        byte_end = idx[end_idx_pos]
        next_offset = offset + limit
        has_more = end_idx_pos < len(idx)

    # 6. seek + read
    try:
        with open(raw, "rb") as f:
            f.seek(byte_start)
            raw_bytes = f.read(byte_end - byte_start)
    except PermissionError:
        return json.dumps(
            {"error": "permission_denied", "message": f"权限不足: {path}"},
            ensure_ascii=False,
        )
    except Exception as e:
        return json.dumps(
            {"error": "read_error", "message": str(e)},
            ensure_ascii=False,
        )

    content = raw_bytes.decode("utf-8", errors="replace")
    lines_read = content.count("\n")

    # 7. 截断
    truncated = False
    if len(content) > MAX_OUTPUT_CHARS:
        content = content[:MAX_OUTPUT_CHARS]
        nl = content.rfind("\n")
        if nl > 0:
            content = content[:nl]
        lines_read = content.count("\n")
        content += f"\n... [截断。续读: offset={next_offset}]"
        truncated = True

    rel = str(raw.relative_to(root) if raw.is_relative_to(root) else raw)

    # 嵌入人类可读元数据到 content 首行，避免 LLM 忽略 JSON 元数据
    end_line = offset + lines_read - 1
    content = f"[第 {offset}-{end_line} 行 / 共 {total_lines} 行]\n\n{content}"

    result = {
        "file": rel,
        "offset": offset,
        "lines_read": lines_read,
        "next_offset": next_offset,
        "total_lines": total_lines,
        "total_bytes": fsize,
        "has_more": has_more,
        "truncated": truncated,
        "content": content,
    }

    return json.dumps(result, ensure_ascii=False)


def read_mmap_lines(
    path: str,
    pattern_regex,
    offset: int = 0,
    limit: int = 200,
    context_lines: int = 0,
    root: Optional[Path] = None,
):
    """mmap 流式 grep，返回 (header, output) 两个字符串。
    
    不走全量 read_text()/readlines()，用 mmap.readline() 逐行扫描。
    上下文行复用已有索引（如果文件已被 read_file 索引过）。
    """
    root = root or Path.cwd().resolve()
    raw = (Path(path).resolve() if Path(path).is_absolute()
           else (root / path)).resolve()

    import re
    compiled = re.compile(pattern_regex)

    raw_results: list = []  # [(file_path, lineno, line_text)]
    total_matches = 0

    try:
        fsize = raw.stat().st_size
    except OSError:
        return None

    if fsize == 0:
        return None

    try:
        with open(raw, "rb") as f:
            mm = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
            try:
                lineno = 1
                for line_bytes in iter(mm.readline, b""):
                    if total_matches >= offset + limit:
                        break
                    try:
                        line = line_bytes.decode("utf-8", errors="replace").rstrip("\n").rstrip("\r")
                    except Exception:
                        lineno += 1
                        continue
                    if compiled.search(line):
                        raw_results.append((str(raw), lineno, line))
                        total_matches += 1
                    lineno += 1
            finally:
                mm.close()
    except Exception:
        return None

    if total_matches == 0:
        return None

    # 分页切片
    page = raw_results[offset:offset + limit]

    header = f'[grep] 模式: "{pattern_regex}" | 共 {total_matches} 条匹配 | offset={offset} limit={limit}'
    remaining = total_matches - (offset + limit)
    if remaining > 0:
        header += f"\n... 还有 {remaining} 条，使用 offset={offset + limit} 查看下一页"

    # 上下文行
    if context_lines > 0:
        # 尝试复用索引
        try:
            idx = _ensure_index(str(raw))
        except Exception:
            idx = None

        output_parts: list = []
        for fpath, lineno, text in page:
            output_parts.append(f"{fpath}:{lineno}: {text}")
            try:
                ctx_start = max(1, lineno - context_lines)
                ctx_end = lineno + context_lines
                if idx is not None:
                    # 用索引定位
                    total = len(idx)
                    si = max(0, ctx_start - 1)
                    ei = min(ctx_end, total - 1)
                    byte_s = idx[si]
                    byte_e = idx[ei] if ei < total else fsize
                    with open(raw, "rb") as f:
                        f.seek(byte_s)
                        ctx_bytes = f.read(byte_e - byte_s)
                    ctx_content = ctx_bytes.decode("utf-8", errors="replace")
                    ctx_lines_list = ctx_content.split("\n")
                    for ci, cl in enumerate(ctx_lines_list, ctx_start):
                        if ci > ctx_end:
                            break
                        marker = ">" if ci == lineno else " "
                        output_parts.append(f"  {marker} {ci}:{cl.rstrip()}")
                else:
                    # fallback: mmap 逐行
                    with open(raw, "rb") as f:
                        mm2 = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
                        try:
                            clno = 1
                            for lb in iter(mm2.readline, b""):
                                if clno > ctx_end:
                                    break
                                if clno >= ctx_start:
                                    cl_text = lb.decode("utf-8", errors="replace").rstrip("\n").rstrip("\r")
                                    marker = ">" if clno == lineno else " "
                                    output_parts.append(f"  {marker} {clno}:{cl_text}")
                                clno += 1
                        finally:
                            mm2.close()
            except Exception:
                pass
        output = "\n".join(output_parts)
    else:
        output = "\n".join(f"{f}:{l}: {t}" for f, l, t in page)

    return header, output
