"""可配置 HTTP 连接器执行器。"""

import json
import re

import requests


def _render(value, variables):
    if not isinstance(value, str):
        return value
    return re.sub(r"\{\{\s*([\w.-]+)\s*\}\}", lambda m: str(variables.get(m.group(1), "")), value)


def _extract(data, path):
    current = data
    for part in str(path).split("."):
        if part == "":
            continue
        if isinstance(current, list) and part.isdigit():
            current = current[int(part)]
        elif isinstance(current, dict):
            current = current.get(part)
        else:
            return None
    return current


class HTTPConnectorExecutor:
    @staticmethod
    def execute(connector, variables=None, timeout=15):
        variables = variables or {}
        method = str(connector.get("method") or "GET").upper()
        url = _render(connector.get("url", ""), variables)
        if not url.startswith(("http://", "https://")):
            raise ValueError("连接器 URL 必须以 http:// 或 https:// 开头")
        headers = connector.get("headers") or {}
        if isinstance(headers, str):
            headers = json.loads(headers or "{}")
        headers = {str(k): _render(v, variables) for k, v in headers.items()}
        auth_type = connector.get("auth_type", "none")
        secret = connector.get("auth_value", "")
        if auth_type == "bearer" and secret:
            headers["Authorization"] = f"Bearer {secret}"
        elif auth_type == "api_key" and secret:
            headers[connector.get("auth_header") or "X-API-Key"] = secret
        params = connector.get("query") or {}
        if isinstance(params, str):
            params = json.loads(params or "{}")
        params = {str(k): _render(v, variables) for k, v in params.items()}
        body = connector.get("body_template")
        if isinstance(body, str) and body.strip():
            body = json.loads(_render(body, variables))
        response = requests.request(method, url, headers=headers, params=params, json=body, timeout=timeout)
        response.raise_for_status()
        try:
            raw = response.json()
        except ValueError:
            raw = response.text
        mapping = connector.get("response_mapping") or {}
        if isinstance(mapping, str):
            mapping = dict(
                line.split("=", 1) for line in mapping.splitlines() if "=" in line
            )
        mapped = {label.strip(): _extract(raw, path.strip()) for label, path in mapping.items()} if mapping else raw
        return {"status_code": response.status_code, "data": mapped}
