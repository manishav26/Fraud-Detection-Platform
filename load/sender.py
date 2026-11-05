"""Sends payments through the gateway on a schedule. Used by load/replay.py and load/synthetic.py.

    stats, wall_s = asyncio.run(run(schedule, url="http://localhost:8000", concurrency=32))

`schedule` yields (seconds_from_start, request, extra) in time order. Each request is sent at its
time ("open loop": a slow platform does not slow the senders down, so slow answers show up as
latency instead of being hidden). `extra` (for example the true fraud label) goes to the CSV only.
"""

import asyncio
import csv
import time
from collections import Counter, defaultdict

import httpx
import numpy as np

SCORE_PATH = "/v1/transactions/score"
RETRIES = 3     # for 503 "could not record", with the same txn_id
BACKOFF = 0.5   # seconds before the first retry, doubled each time


class Token:
    """Gets a JWT from the dev token issuer and gets a new one when the old one expires."""

    def __init__(self, client):
        self.client, self.value, self.lock = client, None, asyncio.Lock()

    async def refresh(self, old=None):
        async with self.lock:
            if self.value == old:   # nobody refreshed it while we waited
                r = await self.client.post("/token")
                r.raise_for_status()
                self.value = r.json()["access_token"]
        return self.value


class Stats:
    def __init__(self):
        self.status, self.decisions, self.latency_ms = Counter(), Counter(), []
        self.retries, self.max_lag_s, self.rows, self.cold_starts = 0, 0.0, [], 0
        self.degraded = 0

    def summary(self, wall_s):
        sent = sum(self.status.values())
        lines = [f"Sent {sent:,} payments in {wall_s:.1f} s ({sent / wall_s:,.1f} per second)",
                 f"Status codes: {dict(self.status)}",
                 f"Decisions: {dict(self.decisions)}",
                 f"Cold starts: {self.cold_starts:,}",
                 f"Degraded (scored without history): {self.degraded:,}",
                 f"Retries after 503: {self.retries}",
                 f"Furthest behind schedule: {self.max_lag_s:.2f} s"]
        if self.latency_ms:
            p50, p95, p99 = np.percentile(self.latency_ms, [50, 95, 99])
            lines.append(f"Latency ms: p50 {p50:.1f}  p95 {p95:.1f}  p99 {p99:.1f}")
        return "\n".join(lines)


async def post(client, req, token):
    return await client.post(SCORE_PATH, json=req, headers={"Authorization": f"Bearer {token}"})


async def send_one(client, token, req):
    """POST one payment. Returns (status, body, latency_ms, retries); status "error" = no answer."""
    retries = 0
    t0 = time.perf_counter()
    try:
        while True:
            current = token.value
            r = await post(client, req, current)
            if r.status_code == 401:            # token expired: get a new one, try once more
                r = await post(client, req, await token.refresh(current))
            if r.status_code == 503 and retries < RETRIES:
                retries += 1
                await asyncio.sleep(BACKOFF * 2 ** (retries - 1))
                continue
            try:
                body = r.json()
            except ValueError:
                body = {}
            return r.status_code, body, (time.perf_counter() - t0) * 1000, retries
    except httpx.HTTPError:
        return "error", {}, (time.perf_counter() - t0) * 1000, retries


CSV_HEADER = ["txn_id", "user_id", "status", "decision", "score", "latency_ms", "extra"]


def write_csv(rows, path):
    with open(path, "w", newline="") as f:
        csv.writer(f).writerows([CSV_HEADER, *rows])


async def run(schedule, url, concurrency=32, keep_rows=False, transport=None):
    """Returns (stats, wall seconds). keep_rows: one row per payment in stats.rows, for a CSV.

    transport: only for tests (a fake gateway).
    """
    stats = Stats()
    in_flight = asyncio.Semaphore(concurrency)
    # One request per user at a time, so a user's payments arrive in the order they happened.
    user_locks = defaultdict(asyncio.Lock)

    async def task(req, extra):
        try:
            async with user_locks[req["user_id"]]:
                status, body, ms, retries = await send_one(client, token, req)
        finally:
            in_flight.release()
        stats.status[status] += 1
        stats.retries += retries
        if status == 200:
            stats.latency_ms.append(ms)
            stats.decisions[body["decision"]] += 1
            stats.cold_starts += bool(body.get("cold_start"))
            stats.degraded += bool(body.get("degraded"))
        if keep_rows:
            stats.rows.append([req["txn_id"], req["user_id"], status, body.get("decision"),
                               body.get("score"), round(ms, 2), extra])

    limits = httpx.Limits(max_connections=concurrency)
    async with httpx.AsyncClient(base_url=url, timeout=30, limits=limits,
                                 transport=transport) as client:
        token = Token(client)
        await token.refresh()
        tasks = set()
        start = time.perf_counter()
        for offset, req, extra in schedule:
            wait = start + offset - time.perf_counter()
            if wait > 0:
                await asyncio.sleep(wait)
            await in_flight.acquire()
            stats.max_lag_s = max(stats.max_lag_s, time.perf_counter() - start - offset)
            t = asyncio.create_task(task(req, extra))
            tasks.add(t)
            t.add_done_callback(tasks.discard)
        await asyncio.gather(*tasks)
        wall = time.perf_counter() - start
    return stats, wall
