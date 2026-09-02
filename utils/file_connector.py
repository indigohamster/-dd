"""Read-only enterprise file connectors.

The first implementation supports Windows SMB/NAS shares.  It deliberately
exposes metadata search only: no file content, upload, rename, delete, command
execution, or arbitrary path traversal is available through this module.
"""

from __future__ import annotations

import ctypes
import os
import re
import threading
import time
from contextlib import contextmanager
from ctypes import wintypes
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote, urlsplit


class FileConnectorError(RuntimeError):
    """A safe, user-displayable connector failure."""


_INVALID_SHARE_CHARS = set('<>:"/\\|?*')
_SKIPPED_NAMES = {"$RECYCLE.BIN", "System Volume Information"}
_QUERY_SLOTS = threading.BoundedSemaphore(4)


def normalize_smb_root(value: object) -> str:
    """Return one canonical UNC root and reject traversal or file-like URLs."""
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("请填写 NAS 共享路径")
    if "\x00" in raw:
        raise ValueError("NAS 路径包含无效字符")
    if raw.lower().startswith("smb://"):
        parsed = urlsplit(raw)
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("SMB 地址不能包含账号、密码、查询参数或片段")
        raw = "\\\\" + parsed.netloc + "\\" + unquote(parsed.path).lstrip("/").replace("/", "\\")
    else:
        raw = raw.replace("/", "\\")
    if "\x00" in raw:
        raise ValueError("NAS 路径包含无效字符")
    if not raw.startswith("\\\\"):
        raise ValueError(r"NAS 路径必须是 \\服务器\共享目录 格式或 smb:// 地址")
    parts = [part.strip() for part in raw[2:].split("\\") if part.strip()]
    if len(parts) < 2:
        raise ValueError("NAS 路径必须同时包含服务器和共享目录")
    if any(part in {".", ".."} for part in parts):
        raise ValueError("NAS 路径不能包含 . 或 ..")
    host, share = parts[0], parts[1]
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,253}", host):
        raise ValueError("NAS 服务器地址格式无效")
    if not share or any(char in _INVALID_SHARE_CHARS for char in share):
        raise ValueError("NAS 共享目录名称无效")
    for part in parts[2:]:
        if any(char in _INVALID_SHARE_CHARS for char in part):
            raise ValueError("NAS 子目录包含无效字符")
    return "\\\\" + "\\".join(parts)


def smb_share_root(value: object) -> str:
    root = normalize_smb_root(value)
    parts = root[2:].split("\\")
    return f"\\\\{parts[0]}\\{parts[1]}"


def _positive_int(value: object, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return min(maximum, max(minimum, parsed))


def _extensions(value: object) -> set[str]:
    if isinstance(value, str):
        values = re.split(r"[,，;；\s]+", value)
    elif isinstance(value, (list, tuple, set)):
        values = value
    else:
        values = []
    result = set()
    for item in values:
        ext = str(item or "").strip().lower()
        if not ext:
            continue
        if not re.fullmatch(r"\.?[A-Za-z0-9]{1,12}", ext):
            raise ValueError(f"文件扩展名无效：{ext}")
        result.add(ext if ext.startswith(".") else f".{ext}")
    return result


class _NETRESOURCEW(ctypes.Structure):
    _fields_ = [
        ("dwScope", wintypes.DWORD), ("dwType", wintypes.DWORD),
        ("dwDisplayType", wintypes.DWORD), ("dwUsage", wintypes.DWORD),
        ("lpLocalName", wintypes.LPWSTR), ("lpRemoteName", wintypes.LPWSTR),
        ("lpComment", wintypes.LPWSTR), ("lpProvider", wintypes.LPWSTR),
    ]


@contextmanager
def _windows_smb_session(root: str, username: str = "", password: str = "", domain: str = ""):
    """Temporarily authenticate to one share without putting secrets in argv."""
    if os.name != "nt" or not username:
        yield
        return
    remote = smb_share_root(root)
    qualified_user = f"{domain}\\{username}" if domain and "\\" not in username else username
    resource = _NETRESOURCEW(0, 1, 0, 0, None, remote, None, None)
    mpr = ctypes.WinDLL("mpr", use_last_error=True)
    add = mpr.WNetAddConnection2W
    add.argtypes = [ctypes.POINTER(_NETRESOURCEW), wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
    add.restype = wintypes.DWORD
    cancel = mpr.WNetCancelConnection2W
    cancel.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.BOOL]
    cancel.restype = wintypes.DWORD
    code = int(add(ctypes.byref(resource), password or None, qualified_user, 0))
    created = code == 0
    if code not in {0, 85}:
        if code == 1219:
            raise FileConnectorError("Windows 已使用其他账号连接该 NAS，请先断开冲突连接后重试")
        raise FileConnectorError(f"NAS 身份验证失败（Windows 错误 {code}）")
    try:
        yield
    finally:
        if created:
            cancel(remote, 0, False)


class SMBReadOnlyConnector:
    """Bounded metadata search for one configured SMB/NAS root."""

    @staticmethod
    def validate_config(connector: dict) -> dict:
        if not isinstance(connector, dict):
            raise ValueError("NAS 连接器配置必须是对象")
        root = normalize_smb_root(connector.get("url"))
        username = str(connector.get("username") or "").strip()
        password = str(connector.get("auth_value") or "")
        if password and not username:
            raise ValueError("填写 NAS 密码时必须同时填写用户名")
        access_scope = str(connector.get("access_scope") or "public").strip().lower()
        if access_scope not in {"public", "restricted", "admin"}:
            raise ValueError("NAS 访问范围必须是 public、restricted 或 admin")
        access_lists = {}
        for key in ("allowed_staff_ids", "allowed_departments", "allowed_roles"):
            values = connector.get(key, []) or []
            if not isinstance(values, list):
                raise ValueError(f"{key} 必须是数组")
            access_lists[key] = list(dict.fromkeys(
                str(value).strip() for value in values if str(value).strip()
            ))[:100]
        return {
            "root": root,
            "max_depth": _positive_int(connector.get("max_depth"), 4, 1, 12),
            "max_results": _positive_int(connector.get("max_results"), 20, 1, 100),
            "extensions": _extensions(connector.get("allowed_extensions")),
            "include_directories": connector.get("include_directories", True) is not False,
            "username": username,
            "password": password,
            "domain": str(connector.get("domain") or "").strip(),
            "access_scope": access_scope,
            **access_lists,
        }

    @staticmethod
    def _validate_query(value: object) -> str:
        query = str(value or "").strip()
        if not query:
            raise ValueError("请输入要查询的文件名或关键词")
        if len(query) > 120:
            raise ValueError("查询关键词不能超过 120 个字符")
        if "\x00" in query or ".." in query or "/" in query or "\\" in query:
            raise ValueError("查询关键词不能包含路径或 ..")
        return query.casefold()

    @staticmethod
    def _item(entry, root: str, relative: str, is_directory: bool) -> dict:
        try:
            stat = entry.stat(follow_symlinks=False)
            modified = datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
            size = 0 if is_directory else int(stat.st_size)
        except OSError:
            modified, size = "", 0
        return {
            "name": entry.name,
            "relative_path": relative.replace("/", "\\"),
            "path": root.rstrip("\\") + "\\" + relative.replace("/", "\\"),
            "type": "directory" if is_directory else "file",
            "size": size,
            "modified_at": modified,
        }

    @classmethod
    def _search_tree(cls, root: str, query: str, *, max_depth: int, max_results: int,
                     extensions: set[str], include_directories: bool, timeout: float) -> list[dict]:
        deadline = time.monotonic() + max(1.0, float(timeout))
        queue = [(root, "", 0)]
        results = []
        while queue and len(results) < max_results:
            if time.monotonic() >= deadline:
                break
            current, relative_root, depth = queue.pop(0)
            try:
                with os.scandir(current) as entries:
                    for entry in entries:
                        if time.monotonic() >= deadline or len(results) >= max_results:
                            break
                        if entry.name in _SKIPPED_NAMES or entry.name.startswith("."):
                            continue
                        try:
                            stat = entry.stat(follow_symlinks=False)
                            is_reparse_point = bool(getattr(stat, "st_file_attributes", 0) & 0x400)
                            if entry.is_symlink() or is_reparse_point:
                                continue
                            is_directory = entry.is_dir(follow_symlinks=False)
                        except OSError:
                            continue
                        relative = f"{relative_root}/{entry.name}" if relative_root else entry.name
                        if query in entry.name.casefold() or query in relative.casefold():
                            if is_directory and include_directories:
                                results.append(cls._item(entry, root, relative, True))
                            elif not is_directory and (not extensions or Path(entry.name).suffix.lower() in extensions):
                                results.append(cls._item(entry, root, relative, False))
                        if is_directory and depth < max_depth:
                            queue.append((entry.path, relative, depth + 1))
            except (OSError, PermissionError) as exc:
                if depth == 0:
                    raise FileConnectorError(f"无法读取 NAS 根目录：{getattr(exc, 'strerror', None) or exc}") from None
                continue
        return results

    @classmethod
    def test(cls, connector: dict, timeout: float = 8) -> dict:
        config = cls.validate_config(connector)
        started = time.perf_counter()
        with _windows_smb_session(config["root"], config["username"], config["password"], config["domain"]):
            try:
                with os.scandir(config["root"]) as entries:
                    visible = sum(1 for _, __ in zip(range(20), entries))
            except (OSError, PermissionError) as exc:
                raise FileConnectorError(f"NAS 连接失败：{getattr(exc, 'strerror', None) or exc}") from None
        return {"root": config["root"], "visible_items_sampled": visible,
                "duration_ms": round((time.perf_counter() - started) * 1000, 2), "read_only": True}

    @classmethod
    def search(cls, connector: dict, query: object, timeout: float = 10) -> dict:
        config = cls.validate_config(connector)
        normalized_query = cls._validate_query(query)
        started = time.perf_counter()
        acquired = _QUERY_SLOTS.acquire(timeout=1)
        if not acquired:
            raise FileConnectorError("NAS 查询任务较多，请稍后重试")
        try:
            with _windows_smb_session(config["root"], config["username"], config["password"], config["domain"]):
                results = cls._search_tree(
                    config["root"], normalized_query,
                    max_depth=config["max_depth"], max_results=config["max_results"],
                    extensions=config["extensions"], include_directories=config["include_directories"],
                    timeout=timeout,
                )
        finally:
            _QUERY_SLOTS.release()
        return {"root": config["root"], "results": results, "count": len(results),
                "duration_ms": round((time.perf_counter() - started) * 1000, 2), "read_only": True}
