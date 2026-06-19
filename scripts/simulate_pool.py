#!/usr/bin/env python3
"""Measure the real memory + file-descriptor cost of the warm connection pool.

Run against any reachable base_url. A valid API key is NOT required — even a 401
still completes the TCP+TLS+HTTP/2 handshake, so the connection is established and
pooled, which is exactly what we're measuring.

    python scripts/simulate_pool.py --concurrency 20
    python scripts/simulate_pool.py --concurrency 20 --http2 off --wait 8

Reports RSS + open FDs at baseline, after opening N concurrent connections, and
(optionally) after an idle wait so you can watch idle connections get reaped.
Best run on Linux (uses /proc); falls back to resource.ru_maxrss elsewhere.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time


def rss_mb() -> float:
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024  # KB -> MB
    except FileNotFoundError:
        import resource

        m = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss  # mac=bytes, linux=KB
        return (m / 1024 / 1024) if sys.platform == "darwin" else (m / 1024)
    return -1.0


def open_fds() -> int:
    try:
        return len(os.listdir("/proc/self/fd"))
    except FileNotFoundError:
        return -1  # not available on macOS without lsof


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--concurrency", type=int, default=20)
    ap.add_argument("--base-url", default=os.environ.get("HOPPER_BASE_URL", "https://api.withhopper.com/v1"))
    ap.add_argument("--http2", default="on", choices=["on", "off"])
    ap.add_argument("--wait", type=float, default=0.0, help="idle seconds after, to watch reaping")
    args = ap.parse_args()

    import httpx
    from openai import AsyncOpenAI

    client = AsyncOpenAI(
        base_url=args.base_url,
        api_key=os.environ.get("HOPPER_API_KEY", "sk-noauth"),
        http_client=httpx.AsyncClient(
            http2=(args.http2 == "on"),
            limits=httpx.Limits(max_connections=100, max_keepalive_connections=20, keepalive_expiry=300),
            timeout=httpx.Timeout(60.0, connect=3.0),
        ),
    )
    pool = client._client._transport._pool

    def conns() -> int:
        return len(getattr(pool, "connections", []))

    def snap(label: str) -> None:
        print(f"{label:<26} RSS={rss_mb():7.1f} MB   fds={open_fds():4d}   pooled_conns={conns():3d}")

    snap("baseline")

    async def hit() -> None:
        try:
            await client.chat.completions.create(
                model="x", messages=[{"role": "user", "content": "hi"}], max_tokens=1)
        except Exception:
            pass  # 401/404/etc still established + pooled the connection

    t0 = time.perf_counter()
    await asyncio.gather(*[hit() for _ in range(args.concurrency)])
    snap(f"after {args.concurrency} concurrent")
    print(f"  ({args.concurrency} reqs in {(time.perf_counter() - t0) * 1000:.0f} ms; with HTTP/2, "
          f"pooled_conns can be << concurrency due to multiplexing)")

    if args.wait:
        await asyncio.sleep(args.wait)
        snap(f"after {args.wait:.0f}s idle")

    await client.close()


if __name__ == "__main__":
    asyncio.run(main())
