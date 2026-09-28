"""默认 HTTP 出口（httpx 实现）。

**所有**供应商请求都必须走这里，不允供应商适配器自己 new 一个 client。
理由：超时/重试/连接池/埋点/测试替身集中在一处，出问题只改这里。
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from ..core.ports import HttpRequest, HttpResponse


class HttpxTransport:
    """同步语义、异步实现。``retries`` 只重试幂等失败（连接错误 / 5xx / 429）。"""

    def __init__(
        self,
        *,
        timeout: float = 60.0,
        retries: int = 2,
        backoff: float = 0.8,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._timeout = timeout
        self._retries = retries
        self._backoff = backoff
        self._client = client
        self._owned = client is None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout, follow_redirects=True)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and self._owned:
            await self._client.aclose()
            self._client = None

    async def request(self, req: HttpRequest) -> HttpResponse:
        client = await self._get_client()
        last_exc: Exception | None = None
        for attempt in range(self._retries + 1):
            started = time.perf_counter()
            try:
                resp = await client.request(
                    req.method,
                    req.url,
                    headers=req.headers or None,
                    params=req.params or None,
                    json=req.json_body,
                    data=req.data,
                    files=req.files,
                    timeout=req.timeout or self._timeout,
                )
            except httpx.HTTPError as exc:
                last_exc = exc
                if attempt < self._retries:
                    await _sleep(self._backoff * (2**attempt))
                    continue
                raise
            elapsed = int((time.perf_counter() - started) * 1000)
            if (resp.status_code >= 500 or resp.status_code == 429) and attempt < self._retries:
                await _sleep(self._backoff * (2**attempt))
                continue
            return _to_response(resp, elapsed)
        raise last_exc  # pragma: no cover - 循环内已 raise

    async def __aenter__(self) -> HttpxTransport:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()


def _to_response(resp: httpx.Response, elapsed_ms: int) -> HttpResponse:
    body: Any = None
    content: bytes | None = None
    try:
        body = resp.json()
    except Exception:
        content = resp.content
    request_id = (
        resp.headers.get("x-request-id")
        or resp.headers.get("x-dashscope-request-id")
        or resp.headers.get("x-acs-request-id")
    )
    return HttpResponse(
        status_code=resp.status_code,
        headers={k.lower(): v for k, v in resp.headers.items()},
        body=body,
        text=resp.text if body is None else "",
        content=content,
        request_id=request_id,
        elapsed_ms=elapsed_ms,
        url=str(resp.url),
    )


async def _sleep(seconds: float) -> None:
    import anyio

    await anyio.sleep(seconds)
