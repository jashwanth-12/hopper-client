#!/usr/bin/env python3
"""hopper-client demo: one warm connection, many turns.

Simulates a multi-turn voice conversation through a SINGLE shared AsyncHopper
client. Watch the per-turn time-to-first-token (TTFT): turn 1 pays the one-time
TCP+TLS+HTTP/2 handshake; every turn after reuses the warm connection. The
connection counter at the end proves only ONE connection was ever opened.

    pip install hopper-client
    export HOPPER_API_KEY=sk-...
    python examples/demo.py
"""
from __future__ import annotations

import asyncio
import os
import time

from hopper import AsyncHopper

BASE_URL = os.environ.get("HOPPER_BASE_URL", "https://api.withhopper.com/v1")
MODEL = os.environ.get("HOPPER_MODEL", "Qwen/Qwen3.6-35B-A3B")

# A short multi-turn conversation, like a voice agent handles turn by turn.
TURNS = [
    "Hi, who are you?",
    "What's the capital of France?",
    "Roughly how many people live there?",
    "Name one thing worth seeing there.",
    "Thanks, that's all!",
]


def count_connections(client) -> dict:
    """Patch the pool(s) to prove how many connections actually get opened.
    (Patches the default transport and any proxy-mounted transports.)"""
    count = {"n": 0}
    hx = client._client
    for transport in [hx._transport, *hx._mounts.values()]:
        pool = getattr(transport, "_pool", None)
        if pool is None:
            continue
        orig = pool.create_connection

        def counted(origin, _orig=orig):
            count["n"] += 1
            return _orig(origin)

        pool.create_connection = counted
    return count


async def ask(client, messages) -> tuple[float, str]:
    t0 = time.perf_counter()
    stream = await client.chat.completions.create(
        model=MODEL, messages=messages, stream=True, max_tokens=64)
    first, text = None, ""
    async for chunk in stream:
        delta = chunk.choices[0].delta.content if chunk.choices else None
        if delta:
            first = first or time.perf_counter()
            text += delta
    return (first - t0) * 1000, text


async def main():
    if not os.environ.get("HOPPER_API_KEY"):
        raise SystemExit("Set HOPPER_API_KEY first.")

    # ONE client, created once, reused for every turn — this is the whole point.
    client = AsyncHopper(base_url=BASE_URL, api_key=os.environ["HOPPER_API_KEY"])
    conns = count_connections(client)

    print(f"model: {MODEL}\n")
    ttfts = []
    messages = []  # accumulates the conversation so the model keeps context
    for i, prompt in enumerate(TURNS, 1):
        messages.append({"role": "user", "content": prompt})
        ttft, reply = await ask(client, messages)
        messages.append({"role": "assistant", "content": reply})
        ttfts.append(ttft)
        tag = "(cold — includes handshake)" if i == 1 else "(warm — reused connection)"
        print(f"turn {i}: TTFT {ttft:5.0f} ms  {tag}")
        print(f"        you: {prompt}")
        print(f"     hopper: {reply.strip()[:80]}\n")

    await client.close()

    warm = ttfts[1:]
    print("─" * 56)
    print(f"connections opened over {len(TURNS)} turns: {conns['n']}")
    if warm:
        print(f"cold first turn: {ttfts[0]:.0f} ms   |   warm avg: {sum(warm) / len(warm):.0f} ms")
    print("→ one warm connection, reused across the whole conversation.")


if __name__ == "__main__":
    asyncio.run(main())
