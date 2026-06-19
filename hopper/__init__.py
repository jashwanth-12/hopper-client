"""hopper — a drop-in, voice-tuned OpenAI client.

Same interface as the `openai` SDK's AsyncOpenAI, but with an HTTP/2 connection
pool that stays warm across streaming turns. That removes the per-turn TCP+TLS
handshake which, on a long-RTT path, adds hundreds of ms to time-to-first-token.

    from hopper import AsyncHopper

    # Initialize ONCE, globally, not per request, or each call gets its own
    # pool and cold-connects, defeating the point.
    client = AsyncHopper(base_url="https://api.withhopper.com/v1", api_key="sk-...")

    async def handle_turn(messages):
        return await client.chat.completions.create(
            model="...", messages=messages, stream=True)

Everything else is exactly the OpenAI SDK.
"""
from __future__ import annotations

import httpx
from openai import AsyncOpenAI

__all__ = ["AsyncHopper"]
__version__ = "0.1.0"

# ── voice-tuned transport defaults
_HTTP2 = True
_KEEPALIVE_EXPIRY = 300.0
_MAX_KEEPALIVE_CONNECTIONS = 20
_MAX_CONNECTIONS = 100
_TIMEOUT = httpx.Timeout(60.0, connect=3.0)


def _default_http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        http2=_HTTP2,
        limits=httpx.Limits(
            max_connections=_MAX_CONNECTIONS,
            max_keepalive_connections=_MAX_KEEPALIVE_CONNECTIONS,
            keepalive_expiry=_KEEPALIVE_EXPIRY,
        ),
    )


class AsyncHopper(AsyncOpenAI):
    def __init__(self, *args, http_client: httpx.AsyncClient | None = None, **kwargs) -> None:
        if http_client is None:
            http_client = _default_http_client()
            kwargs.setdefault("timeout", _TIMEOUT)
        super().__init__(*args, http_client=http_client, **kwargs)
