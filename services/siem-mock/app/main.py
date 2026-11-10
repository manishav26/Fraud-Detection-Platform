"""siem-mock: a stand-in for the security team's SIEM (week 4, step 7). Design: docs/siem.md

Speaks the same protocol as Splunk's HTTP Event Collector, so a real Splunk could replace it by
changing only alert-svc's SIEM_URL and token:
  POST /services/collector/event   header "Authorization: Splunk <token>"; body: one or more
                                   events, each {"time", "source", "sourcetype", "event": {...}}
                                   (as JSON objects one after another, like HEC's batch mode)
  GET  /events?limit=50            the latest events, newest first (JSON)
  GET  /                           the "SIEM view": a page of the latest events, refreshes itself

A mock: it keeps the last MAX_EVENTS in memory only. Each event carries its audit row id, and an
id it has already seen is counted but not stored twice (the forwarder delivers at least once).
Not routed through Kong; the laptop reaches it with `make k8s-siem` or, in compose, port 8088.
"""

import hmac
import html
import json
import os
import time
from collections import OrderedDict

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from prometheus_client import Counter, Gauge

from observability.http import instrument

TOKEN = os.getenv("SIEM_TOKEN", "")
MAX_EVENTS = int(os.getenv("SIEM_MAX_EVENTS", "10000"))
MAX_BODY = 2_000_000   # bytes per request; the forwarder sends at most 500 small events

EVENTS = Counter("siem_events_total", "Events received, by outcome", ["outcome"])
STORED = Gauge("siem_events_stored", "Events held in memory")
# Seconds from the audit row being written to the SIEM storing it: the end-to-end delay
DELAY = Gauge("siem_last_event_delay_seconds", "Delay of the most recent event")

app = FastAPI(title="siem-mock")
instrument(app, "siem-mock")
_events = OrderedDict()   # audit row id -> stored event, oldest first


def authorized(header):
    """HEC's scheme: 'Splunk <token>'. Compared in constant time; no token set means refuse all."""
    scheme, _, given = (header or "").partition(" ")
    return bool(TOKEN) and scheme == "Splunk" and hmac.compare_digest(given.strip(), TOKEN)


def split_events(body):
    """HEC batch mode: JSON objects back to back, optionally separated by whitespace."""
    decoder, events, i = json.JSONDecoder(), [], 0
    text = body.decode()
    while i < len(text):
        while i < len(text) and text[i].isspace():
            i += 1
        if i == len(text):
            break
        obj, i = decoder.raw_decode(text, i)
        events.append(obj)
    return events


def check(item):
    if not isinstance(item, dict) or not isinstance(item.get("event"), dict) \
            or "id" not in item["event"]:
        raise ValueError("each item needs an 'event' object with an 'id'")


def store(item, received_at):
    """Keep one checked event; False if its id was already stored."""
    event = item["event"]
    if event["id"] in _events:
        return False
    _events[event["id"]] = {**item, "received_at": received_at}
    while len(_events) > MAX_EVENTS:
        _events.popitem(last=False)
    if isinstance(item.get("time"), int | float):
        DELAY.set(max(0.0, received_at - item["time"]))
    return True


@app.post("/services/collector/event")
async def collect(request: Request):
    if not authorized(request.headers.get("authorization")):
        EVENTS.labels("refused").inc()
        raise HTTPException(401, {"text": "Invalid token", "code": 4})
    body = await request.body()
    if len(body) > MAX_BODY:
        raise HTTPException(413, {"text": "Too large", "code": 6})
    try:
        items = split_events(body)
        received_at = time.time()
        # All checked before any is kept, so a bad batch is refused whole and resent whole
        for item in items:
            check(item)
    except (ValueError, UnicodeDecodeError) as e:
        EVENTS.labels("invalid").inc()
        raise HTTPException(400, {"text": f"Invalid data format: {e}", "code": 6}) from None
    new = sum(store(item, received_at) for item in items)
    EVENTS.labels("stored").inc(new)
    EVENTS.labels("duplicate").inc(len(items) - new)
    STORED.set(len(_events))
    return {"text": "Success", "code": 0, "stored": new, "duplicates": len(items) - new}


def latest(limit):
    return list(reversed(list(_events.values())[-limit:]))


@app.get("/events")
def events(limit: int = Query(default=50, ge=1, le=1000)):
    return latest(limit)


@app.get("/", response_class=HTMLResponse)
def view(limit: int = Query(default=100, ge=1, le=1000)):
    rows = []
    for item in latest(limit):
        e = item["event"]
        delay = item["received_at"] - item["time"] if isinstance(item.get("time"), int | float) else None
        rows.append("<tr>" + "".join(f"<td>{html.escape(str(v))}</td>" for v in (
            e["id"], e.get("at", ""), e.get("actor", ""), e.get("action", ""), e.get("target", ""),
            json.dumps(e.get("details", {}), sort_keys=True)[:300],
            "" if delay is None else f"{delay:.1f} s")) + "</tr>")
    return f"""<!doctype html><meta charset="utf-8"><meta http-equiv="refresh" content="2">
<title>SIEM (mock)</title>
<style>body{{font:14px system-ui;margin:16px}}table{{border-collapse:collapse;width:100%}}
td,th{{border-bottom:1px solid #ddd;padding:4px 6px;text-align:left;vertical-align:top}}
td:nth-child(6){{font-family:monospace;font-size:12px}}</style>
<h2>SIEM (mock): latest security events</h2>
<p>{len(_events)} stored. Refreshes every 2 s. Source: the platform's audit log, via alert-svc.</p>
<table><tr><th>audit id</th><th>at</th><th>actor</th><th>action</th><th>target</th><th>details</th>
<th>delay</th></tr>{"".join(rows)}</table>"""
