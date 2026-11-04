"""Request metrics for every service, and the /metrics page Prometheus reads. Design: docs/metrics.md

    instrument(app, "scoring-svc")

Per route (the route's pattern, e.g. /cases/{case_id}, so case ids don't create one series each):
  http_requests_total{service, route, method, status}
  http_request_duration_seconds{service, route, method}   histogram -> p50, p95, p99
/metrics is for Prometheus inside the network only; Kong never routes it.
"""

import time

from fastapi import Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

# Milliseconds to seconds: fine steps where our payments usually are, coarse ones for slow periods
BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.075, 0.1, 0.25, 0.5, 1, 2.5, 5, 10)

REQUESTS = Counter("http_requests_total", "Requests answered", ["service", "route", "method", "status"])
LATENCY = Histogram("http_request_duration_seconds", "Time to answer a request",
                    ["service", "route", "method"], buckets=BUCKETS)


def instrument(app, service, before_scrape=None):
    """before_scrape: optional function run before each /metrics read, to refresh values that
    are only known at that moment (for example which model is live)."""

    @app.middleware("http")
    async def measure(request: Request, call_next):
        if request.url.path == "/metrics":
            return await call_next(request)
        start = time.perf_counter()
        response = await call_next(request)
        route = request.scope.get("route")
        name = route.path if route is not None else "unmatched"
        LATENCY.labels(service, name, request.method).observe(time.perf_counter() - start)
        REQUESTS.labels(service, name, request.method, str(response.status_code)).inc()
        return response

    @app.get("/metrics", include_in_schema=False)
    def metrics():
        if before_scrape:
            before_scrape()
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
