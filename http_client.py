"""Минимальный HTTP-клиент на urllib (без внешних зависимостей)."""

from __future__ import annotations

import http.cookiejar
import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


class HttpSession:
    """GET/POST с сохранением cookies между запросами."""

    __slots__ = ("_opener", "_base_headers")

    def __init__(self) -> None:
        jar = http.cookiejar.CookieJar()
        self._opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        self._base_headers = {
            "User-Agent": "Mozilla/5.0 (compatible; FAW-Monitor/1.0)",
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json, text/javascript, */*; q=0.01",
        }

    def get(self, url: str, timeout: float = 45) -> str:
        req = urllib.request.Request(url, headers=dict(self._base_headers))
        with self._opener.open(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")

    def post_form(
        self,
        url: str,
        data: dict[str, str],
        extra_headers: dict[str, str] | None = None,
        timeout: float = 45,
    ) -> str:
        body = urllib.parse.urlencode(data).encode("utf-8")
        h = dict(self._base_headers)
        h["Content-Type"] = "application/x-www-form-urlencoded"
        if extra_headers:
            h.update(extra_headers)
        req = urllib.request.Request(url, data=body, headers=h, method="POST")
        with self._opener.open(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")

    def post_json(self, url: str, payload: dict[str, Any], timeout: float = 30) -> tuple[int, str]:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        h = dict(self._base_headers)
        h["Content-Type"] = "application/json; charset=utf-8"
        req = urllib.request.Request(url, data=body, headers=h, method="POST")
        with self._opener.open(req, timeout=timeout) as resp:
            return resp.getcode(), resp.read().decode("utf-8", errors="replace")
