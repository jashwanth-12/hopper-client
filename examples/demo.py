"""Minimal hopper-client usage — a drop-in for the OpenAI async client."""
import asyncio

from hopper import AsyncHopper as AsyncOpenAI

MODEL = "Qwen/Qwen3.6-35B-A3B"

# Create the client once, as a global, and reuse it everywhere.
client = AsyncOpenAI(
    base_url="https://api.withhopper.com/v1",
    api_key="",  # <-- put your Hopper API key here
)


async def main():
    resp = await client.chat.completions.create(
        model=MODEL,
        messages=[{"role": "user", "content": "Hi, who are you?"}],
    )
    print(resp.choices[0].message.content)


asyncio.run(main())
