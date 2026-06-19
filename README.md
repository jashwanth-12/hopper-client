# hopper-client

A drop-in, voice-tuned replacement for the OpenAI client. It has the same interface as OpenAI client with optimized config so that a voice agent stops paying a fresh TCP + TLS handshake on every turn.

Voice agents call an LLM once per conversational turn. With the
stock OpenAI client (HTTP/1.1, 5-second keep-alive) the connection is constantly
torn down and rebuilt between turns, conversational pauses exceed the keep-alive
window and hence you can't reuse a HTTP/1.1 connection. Each rebuild costs a TCP + TLS handshake, which is **hundreds of milliseconds added to every turn**. `hopper-client`
fixes this with a single warm connection that is reused across turns and interruptions.

```python
# before
from openai import AsyncOpenAI
client = AsyncOpenAI(base_url="https://api.withhopper.com/v1", api_key="sk-...")

# after — same constructor, same methods
from hopper import AsyncHopper as AsyncOpenAI
client = AsyncOpenAI(base_url="https://api.withhopper.com/v1", api_key="sk-...")
```

## Install

```bash
pip install hopper-client  
```

Pulls `openai` and `httpx[http2]`

## Parameters & reasoning

`AsyncHopper` has the **identical constructor** to `AsyncOpenAI` — every OpenAI
parameter is accepted and passed straight through. The only difference is the
**default transport**: when you don't supply your own `http_client`, Hopper builds
one tuned for voice. To override anything, pass your own `http_client=`.

| Setting | OpenAI default | Hopper default | Why |
|---|---|---|---|
| `http2` | `False` | **`True`** | One connection multiplexes many streams. Cancelling a stream (barge-in) sends `RST_STREAM` and keeps the connection warm; under HTTP/1.1 a half-read connection can't be reused and is closed. This is the core fix. |
| `keepalive_expiry` | `5.0s` | **`300s`** | httpx reaps idle connections after this long. Voice turns are seconds apart (TTS playing, the user speaking); a 5s window means the next turn re-handshakes. 300s spans normal conversational gaps. **Coordinate with your server/LB idle timeout** — if the server closes the socket first, the warm pool fills with dead connections. |
| `max_keepalive_connections` | `100` | **`20`** | With HTTP/2 a single connection carries many streams, so the pool stays small. A small pool also bounds TCP head-of-line-blocking blast radius on lossy links. Raise it if one process drives many concurrent sessions. |
| `max_connections` | `1000` | **`100`** | The client shouldn't be the bottleneck; server-side admission control (429) is the real concurrency limit. With HTTP/2 you rarely approach this. |
| `connect` timeout | `5.0s` | **`3.0s`** | Fail a bad connection fast so the caller can retry/hedge instead of stalling a live turn. |
| `read` / `write` / `pool` timeout | `600s` | **`60s`** | `read` is the per-chunk gap (it bounds both TTFT and inter-token stalls), not a total cap. 60s is generous but far tighter than 600s. Override per-request for a strict turn budget. |
| `max_retries` | `2` | `2` (unchanged) | Left at the default for now. Note retries add latency silently; voice deployments may prefer `0`–`1` plus explicit hedging. |


## Results

Measured with `scripts/voice_sim.py` against a live `Qwen/Qwen3.6-35B-A3B`
endpoint, comparing vanilla `AsyncOpenAI` vs `AsyncHopper` on the same model and
target. Vanilla opens one per
call, Hopper reuses a single warm connection throughout.

| Scenario | Calls | OpenAI client conns | Hopper client conns | OpenAI client TTFT (median) | Hopper client TTFT (median) |
|---|---|---|---|---|---|
| **gaps** — conversational pauses between turns | 7 | **7** | **1** | 510 ms | 197 ms |
| **bargein** — stream cancelled after first token | 9 | **9** | **1** | 512 ms | 193 ms |
| **concurrent** — 20 sessions × 3 turns | 61 | **61** | **1** | 600 ms | 319 ms |
| **steady** — back-to-back turns, no gaps (control) | 9 | **9** | **1** | 514 ms | 193 ms |


