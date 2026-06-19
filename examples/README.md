# Examples

## `demo.py` — one warm connection, many turns

Runs a short multi-turn conversation through a **single shared `AsyncHopper`
client** and prints the time-to-first-token (TTFT) for each turn. The first turn
pays the one-time TCP+TLS+HTTP/2 handshake; every turn after reuses the warm
connection — and the connection counter at the end proves only **one** connection
was opened.

```bash
pip install hopper-client
export HOPPER_API_KEY=sk-...
python examples/demo.py
```

Example output:

```
turn 1: TTFT   746 ms  (cold — includes handshake)
        you: Hi, who are you?
     hopper: Hello! I am Qwen, a large language model ...
turn 2: TTFT   187 ms  (warm — reused connection)
        you: What's the capital of France?
     hopper: The capital of France is Paris.
...
connections opened over 5 turns: 1
cold first turn: 746 ms   |   warm avg: 191 ms
→ one warm connection, reused across the whole conversation.
```

The takeaway for a voice agent: pay the handshake once, then every conversational
turn rides the same warm connection — no per-turn handshake tax.

> Run it from your own machine (not behind a corporate proxy) for representative
> latency numbers.
