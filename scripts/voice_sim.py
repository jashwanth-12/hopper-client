#!/usr/bin/env python3
"""Voice-agent latency simulations: vanilla OpenAI vs Hopper.

Compares time-to-first-token (TTFT) and connections-created across realistic
voice situations — the ones where a cold vs warm connection actually shows.

    export HOPPER_API_KEY=sk-...
    python scripts/voice_sim.py                  # all scenarios
    python scripts/voice_sim.py --scenario gaps  # one scenario
    python scripts/voice_sim.py --scenario concurrent --sessions 40

Scenarios
  gaps        sequential turns with conversational pauses (TTS playing / user
              speaking). Vanilla's 5s keepalive reaps the connection between
              turns -> handshake every turn; Hopper (300s) stays warm.
  bargein     each turn cancels the stream after the first token (user
              interrupts). HTTP/1.1 can't reuse a half-read connection -> it
              dies and re-handshakes; Hopper's HTTP/2 sends RST_STREAM and keeps
              the connection warm.
  concurrent  many sessions in one process. HTTP/1.1 opens ~one connection per
              concurrent call; Hopper's HTTP/2 multiplexes onto ~1-2.
  steady      control: back-to-back fully-consumed turns, no gaps. Both should be
              warm and roughly tied — shows where it does NOT matter (honest).
"""
from __future__ import annotations

import argparse
import asyncio
import os
import statistics
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from openai import AsyncOpenAI

from hopper import AsyncHopper

MODEL = os.environ.get("HOPPER_MODEL", "Qwen/Qwen3.6-35B-A3B")
BASE_URL = os.environ.get("HOPPER_BASE_URL", "https://api.withhopper.com/v1")
API_KEY = os.environ.get("HOPPER_API_KEY", "")

FILLER = ("The quick brown fox jumps over the lazy dog near the riverbank "
          "while the sun sets slowly behind the distant mountains. ")


def make_prompt(approx_tokens: int) -> str:
    if approx_tokens <= 5:
        return "Hi"
    words = int(approx_tokens * 0.75)
    text = " ".join((FILLER * (words // len(FILLER.split()) + 1)).split()[:words])
    return f"Summarize the following text in one word:\n\n{text}"


def _pool(client):
    return client._client._transport._pool


def _install_conn_counter(client) -> dict:
    """Count every new connection (= TCP+TLS handshake) over this client's life.
    httpcore calls self.create_connection() (connection_pool.py:326). We patch it
    on the default transport AND every mounted transport — with a proxy in the env
    (trust_env), requests route through a mounted proxy transport, not the default
    one, so patching only the default pool counts nothing."""
    count = {"n": 0}
    hx = client._client
    for t in [hx._transport, *hx._mounts.values()]:
        pool = getattr(t, "_pool", None)
        if pool is None:
            continue
        orig = pool.create_connection

        def counted(origin, _orig=orig):
            count["n"] += 1
            return _orig(origin)

        pool.create_connection = counted
    return count


async def ttft_ms(client, prompt: str, *, max_tokens: int = 16, early_break: bool = False) -> float:
    """Time-to-first-token in ms. If early_break, cancel right after token 1."""
    t0 = time.perf_counter()
    stream = await client.chat.completions.create(
        model=MODEL, messages=[{"role": "user", "content": prompt}],
        stream=True, max_tokens=max_tokens)
    first = None
    try:
        async for chunk in stream:
            if chunk.choices and chunk.choices[0].delta.content:
                first = time.perf_counter()
                if early_break:
                    break
    finally:
        if early_break:
            await stream.close()  # partial read -> the connection-reuse test
    return (first - t0) * 1000 if first else float("nan")


async def _warm(client) -> None:
    try:
        await ttft_ms(client, "Hi", max_tokens=4)
    except Exception:
        pass


# ── scenarios (each returns a list of TTFTs; connection count is tracked outside)
async def scn_gaps(client, *, turns=6, gap=6.0, size=500):
    await _warm(client)
    ttfts = []
    for i in range(turns):
        ttfts.append(await ttft_ms(client, make_prompt(size)))
        if i < turns - 1:
            await asyncio.sleep(gap)
    return ttfts


async def scn_bargein(client, *, turns=8, size=500):
    await _warm(client)
    ttfts = []
    for _ in range(turns):
        ttfts.append(await ttft_ms(client, make_prompt(size), max_tokens=64, early_break=True))
        await asyncio.sleep(0.3)  # like TTS playing the first word back
    return ttfts


async def scn_concurrent(client, *, sessions=20, turns=3, size=500):
    await _warm(client)

    async def session():
        out = []
        for _ in range(turns):
            out.append(await ttft_ms(client, make_prompt(size)))
            await asyncio.sleep(0.5)
        return out

    results = await asyncio.gather(*[session() for _ in range(sessions)])
    return [t for r in results for t in r]


async def scn_steady(client, *, turns=8, size=500):
    await _warm(client)
    ttfts = []
    for _ in range(turns):
        ttfts.append(await ttft_ms(client, make_prompt(size)))
    return ttfts


SCENARIOS = {"gaps": scn_gaps, "bargein": scn_bargein,
             "concurrent": scn_concurrent, "steady": scn_steady}


# ── runner ────────────────────────────────────────────────────────────────────
def _summary(ttfts) -> str:
    xs = sorted(t for t in ttfts if t == t)  # drop NaN
    if not xs:
        return "no data (all calls failed — check key/model/base_url)"
    p95 = xs[min(len(xs) - 1, round(0.95 * (len(xs) - 1)))]
    return (f"median {statistics.median(xs):6.0f}ms   p95 {p95:6.0f}ms   "
            f"max {max(xs):6.0f}ms   n={len(xs)}")


def _make(kind):
    if kind == "vanilla":
        return AsyncOpenAI(base_url=BASE_URL, api_key=API_KEY)
    return AsyncHopper(base_url=BASE_URL, api_key=API_KEY)


async def run_one(name, fn, **kw):
    print(f"\n=== {name} ===")
    for kind, label in (("vanilla", "vanilla OpenAI"), ("hopper", "Hopper        ")):
        client = _make(kind)
        count = _install_conn_counter(client)  # before warmup, so it's counted too
        try:
            ttfts = await fn(client, **kw)
            print(f"  {label}:  {_summary(ttfts)}   | connections created: {count['n']}")
        except Exception as e:
            print(f"  {label}:  ERROR {type(e).__name__}: {e}")
        finally:
            await client.close()


async def preflight() -> bool:
    print(f"target: {BASE_URL}   model: {MODEL}")
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    if proxy:
        print(f"WARNING: traffic routes through a proxy ({proxy}). Latency numbers are "
              f"NOT production-accurate — run from a clean, unproxied host near the server "
              f"for real figures. Connection COUNTS remain valid.")
    if not API_KEY:
        print("ERROR: set HOPPER_API_KEY first."); return False
    c = AsyncHopper(base_url=BASE_URL, api_key=API_KEY)
    try:
        t = await ttft_ms(c, "Hi", max_tokens=4)
        print(f"preflight OK (first call TTFT {t:.0f}ms — includes the cold handshake)\n")
        return True
    except Exception as e:
        print(f"preflight FAILED: {type(e).__name__}: {e}"); return False
    finally:
        await c.close()


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", choices=[*SCENARIOS, "all"], default="all")
    ap.add_argument("--sessions", type=int, default=20)
    ap.add_argument("--gap", type=float, default=6.0)
    ap.add_argument("--size", type=int, default=500)
    args = ap.parse_args()

    if not await preflight():
        sys.exit(1)

    chosen = SCENARIOS if args.scenario == "all" else {args.scenario: SCENARIOS[args.scenario]}
    for name, fn in chosen.items():
        kw = {"size": args.size}
        if name == "gaps":
            kw["gap"] = args.gap
        if name == "concurrent":
            kw["sessions"] = args.sessions
        await run_one(name, fn, **kw)

    print("\nReading: same target/model for both, so TTFT gaps = connection cost. "
          "Watch 'connections created' — vanilla churns, Hopper stays ~flat.")


if __name__ == "__main__":
    asyncio.run(main())
