"""Finds where a slow period comes from. Run it beside a load test: make stall-probe

Prints a line, with the UTC time, whenever one of these is slow:
  laptop   a 50 ms timer on the laptop fires late: the Mac itself is short of CPU
  vm       the same timer inside a container fires late: the Docker VM is short of CPU
  gateway  POST /token through Kong is slow: Kong and the token issuer only, no Redpanda or Cassandra
Every 5 s it also prints the containers using the most CPU.
"""

import argparse
import asyncio
import time
from datetime import UTC, datetime

import httpx

TIMER_SECONDS = 0.05
LATE_MS = 100
SLOW_GATEWAY_MS = 100
# Runs inside the container, printing how late its timer fires
VM_TIMER = f"""
import time
while True:
    t = time.perf_counter()
    time.sleep({TIMER_SECONDS})
    late = (time.perf_counter() - t - {TIMER_SECONDS}) * 1000
    if late >= {LATE_MS}:
        print(round(late), flush=True)
"""


def say(text):
    print(f"{datetime.now(UTC):%H:%M:%S} {text}", flush=True)


async def laptop_timer():
    while True:
        t = time.perf_counter()
        await asyncio.sleep(TIMER_SECONDS)
        late = (time.perf_counter() - t - TIMER_SECONDS) * 1000
        if late >= LATE_MS:
            say(f"laptop  timer {late:.0f} ms late")


async def vm_timer():
    process = await asyncio.create_subprocess_exec(
        "docker", "compose", "exec", "-T", "token-issuer", "python", "-c", VM_TIMER,
        stdout=asyncio.subprocess.PIPE)
    while line := await process.stdout.readline():
        say(f"vm      timer {line.decode().strip()} ms late")


async def gateway(url):
    async with httpx.AsyncClient(base_url=url, timeout=30) as client:
        while True:
            t = time.perf_counter()
            await client.post("/token")
            ms = (time.perf_counter() - t) * 1000
            if ms >= SLOW_GATEWAY_MS:
                say(f"gateway /token took {ms:.0f} ms")
            await asyncio.sleep(0.5)


async def busiest():
    while True:
        process = await asyncio.create_subprocess_exec(
            "docker", "stats", "--no-stream", "--format", "{{.CPUPerc}} {{.Name}}",
            stdout=asyncio.subprocess.PIPE)
        out, _ = await process.communicate()
        rows = sorted(out.decode().splitlines(), key=lambda r: -float(r.split("%")[0]))[:4]
        names = ", ".join(r.replace("fraud-detection-platform-", "") for r in rows)
        say(f"cpu     {names}")
        await asyncio.sleep(5)


async def main(url, seconds):
    tasks = [asyncio.create_task(t) for t in (laptop_timer(), vm_timer(), gateway(url), busiest())]
    await asyncio.sleep(seconds)
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--seconds", type=int, default=240)
    p.add_argument("--url", default="http://localhost:8000")
    args = p.parse_args()
    asyncio.run(main(args.url, args.seconds))
