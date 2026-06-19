# hopper-client

A drop-in, voice-tuned replacement for the OpenAI async client. It speaks the
exact same API as `openai`'s `AsyncOpenAI`, but ships an **HTTP/2 connection pool
that stays warm across streaming turns** — so a voice agent stops paying a fresh
TCP + TLS handshake on every turn.

**Why this exists.** Voice agents call an LLM once per conversational turn, and
the thing that gates response latency is **time-to-first-token (TTFT)**. With the
stock OpenAI client (HTTP/1.1, 5-second keep-alive) the connection is constantly
torn down and rebuilt between turns — conversational pauses exceed the keep-alive
window, and cancelling a stream early (barge-in) leaves an HTTP/1.1 connection
un-reusable, so it's closed. Each rebuild costs a TCP + TLS handshake, which on a
long-RTT path is **hundreds of milliseconds added to every turn**. `hopper-client`
fixes this with one change: an HTTP/2 pool with a long keep-alive, so a single
warm connection is reused across turns, interruptions, and many concurrent
sessions.

```python
# before
from openai import AsyncOpenAI
client = AsyncOpenAI(base_url="https://api.withhopper.com/v1", api_key="sk-...")

# after — same constructor, same methods
from hopper import AsyncHopper as AsyncOpenAI
client = AsyncOpenAI(base_url="https://api.withhopper.com/v1", api_key="sk-...")
```

Initialize **once, globally**, and reuse it everywhere — constructing a client
per request gives each one its own pool and defeats the warm-connection win:

```python
from hopper import AsyncHopper

client = AsyncHopper(base_url="https://api.withhopper.com/v1", api_key="sk-...")

async def handle_turn(messages):
    return await client.chat.completions.create(
        model="Qwen/Qwen3.6-35B-A3B", messages=messages, stream=True)
```

## Install

```bash
pip install hopper-client      # once published
# or, from source:
pip install -e .
```

Pulls `openai` and `httpx[http2]` (the `h2` package is a hard dependency, since
HTTP/2 is on by default).

## Parameters & reasoning

`AsyncHopper` has the **identical constructor** to `AsyncOpenAI` — every OpenAI
parameter is accepted and passed straight through. The only difference is the
**default transport**: when you don't supply your own `http_client`, Hopper builds
one tuned for voice. To override anything, pass your own `http_client=` (the
standard OpenAI escape hatch).

| Setting | OpenAI default | Hopper default | Why |
|---|---|---|---|
| `http2` | `False` | **`True`** | One connection multiplexes many streams. Cancelling a stream (barge-in) sends `RST_STREAM` and keeps the connection warm; under HTTP/1.1 a half-read connection can't be reused and is closed. This is the core fix. |
| `keepalive_expiry` | `5.0s` | **`300s`** | httpx reaps idle connections after this long. Voice turns are seconds apart (TTS playing, the user speaking); a 5s window means the next turn re-handshakes. 300s spans normal conversational gaps. **Coordinate with your server/LB idle timeout** — if the server closes the socket first, the warm pool fills with dead connections. |
| `max_keepalive_connections` | `100` | **`20`** | With HTTP/2 a single connection carries many streams, so the pool stays small. A small pool also bounds TCP head-of-line-blocking blast radius on lossy links. Raise it if one process drives many concurrent sessions. |
| `max_connections` | `1000` | **`100`** | The client shouldn't be the bottleneck; server-side admission control (429) is the real concurrency limit. With HTTP/2 you rarely approach this. |
| `connect` timeout | `5.0s` | **`3.0s`** | Fail a bad connection fast so the caller can retry/hedge instead of stalling a live turn. |
| `read` / `write` / `pool` timeout | `600s` | **`60s`** | `read` is the per-chunk gap (it bounds both TTFT and inter-token stalls), not a total cap. 60s is generous but far tighter than 600s. Override per-request for a strict turn budget. |
| `max_retries` | `2` | `2` (unchanged) | Left at the default for now. Note retries add latency silently; voice deployments may prefer `0`–`1` plus explicit hedging. |

The timeout is set on the **SDK client**, not the httpx client, on purpose: the
OpenAI SDK builds every request with `timeout=self.timeout`, overriding whatever
the httpx client carries — so the SDK-level value is the authoritative one.

## Results

Measured with `scripts/voice_sim.py` against a live `Qwen/Qwen3.6-35B-A3B`
endpoint, comparing vanilla `AsyncOpenAI` vs `AsyncHopper` on the same model and
target. The decisive metric is **connections created** — vanilla opens one per
call, Hopper reuses a single warm connection throughout.

| Scenario | Calls | Vanilla conns | Hopper conns | Vanilla TTFT (median) | Hopper TTFT (median) |
|---|---|---|---|---|---|
| **gaps** — conversational pauses between turns | 7 | **7** | **1** | 510 ms | 197 ms |
| **bargein** — stream cancelled after first token | 9 | **9** | **1** | 512 ms | 193 ms |
| **concurrent** — 20 sessions × 3 turns | 61 | **61** | **1** | 600 ms | 319 ms |
| **steady** — back-to-back turns, no gaps (control) | 9 | **9** | **1** | 514 ms | 193 ms |

The standout is **concurrent**: 60 concurrent LLM calls rode a **single** HTTP/2
connection with Hopper, versus 61 separate connections with vanilla. In a real
high-concurrency voice server that's the difference between one warm connection
and hundreds of handshakes.

> **Caveat on the latency numbers.** This run went through a corporate proxy, so
> the absolute TTFT figures are *indicative, not production-accurate*. The
> connection counts are valid regardless (they count client-side connection
> creation). For quotable latency, run the benchmark from a clean, unproxied host
> near the endpoint.

### Running the benchmarks

```bash
export HOPPER_API_KEY=sk-...
python scripts/voice_sim.py                  # all scenarios
python scripts/voice_sim.py --scenario concurrent --sessions 40
python scripts/simulate_pool.py --concurrency 20 --wait 8   # memory / FD footprint
```

## Roadmap

This is the minimal first cut — connection reuse only. Planned, behind the same
drop-in interface: connection keep-warm heartbeat, request hedging for tail
latency, client-side caching for canned turns, and an HTTP/3 (QUIC) transport for
lossy mobile networks.
