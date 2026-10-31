"""官网机读面 HTTP 客户端：manifest 指纹 + 三区 feed 全量拉取。

刻意 ``trust_env=False``：集群内系统代理劫持回环请求有先例（502 假故障），
而 feed 是公开件、无需代理。凭据零需求——不落任何密钥。
"""

from __future__ import annotations

import httpx

ZONES: tuple[str, ...] = ("briefings", "academy", "research")


class FeedUnavailable(Exception):
    """上游不可达/非 200/非 JSON——摄取整体中止，保留上一版。"""


class FeedSchemaMismatch(Exception):
    """feed 顶层契约版本不在声明兼容范围内——拒摄，不猜字段。"""

    def __init__(self, zone: str, actual: object):
        super().__init__(f"{zone} feed schema_version={actual!r} 不在兼容范围")
        self.zone = zone
        self.actual = actual


class FeedClient:
    """三区 feed 与 manifest 的只读客户端（同步 HTTP，跑在线程里）。"""

    def __init__(self, base_url: str, timeout_seconds: float = 30.0, transport=None):
        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(
            base_url=self.base_url,
            timeout=timeout_seconds,
            transport=transport,
            trust_env=False,
            follow_redirects=True,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "FeedClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _get_json(self, path: str) -> dict:
        try:
            res = self._client.get(path)
        except httpx.HTTPError as exc:
            raise FeedUnavailable(f"{path} 不可达：{type(exc).__name__}: {exc}") from exc
        if res.status_code != 200:
            raise FeedUnavailable(f"{path} → HTTP {res.status_code}")
        try:
            data = res.json()
        except ValueError as exc:
            raise FeedUnavailable(f"{path} 不是合法 JSON") from exc
        if not isinstance(data, dict):
            raise FeedUnavailable(f"{path} 顶层不是对象")
        return data

    def fetch_manifest(self) -> dict:
        return self._get_json("/content-manifest.json")

    def fetch_zone(self, zone: str) -> dict:
        data = self._get_json(f"/{zone}.json")
        items = data.get(zone)
        if not isinstance(items, list):
            raise FeedUnavailable(f"/{zone}.json 缺少 {zone} 数组")
        return data
