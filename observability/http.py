"""Request metrics for every service, and the /metrics page Prometheus reads. Design: docs/metrics.md

    instrument(app, "scoring-svc")

Per route (the route's pattern, e.g. /cases/{case_id}, so case ids don't create one series each):
  http_requests_total{service, route, method, status}
  http_request_duration_seconds{service, route, method}   histogram -> p50, p95, p99
/metrics is for Prometheus inside the network only; Kong never routes it.

Health routes for Kubernetes' probes (week 4, step 2; docs/k8s.md), also never routed by Kong:
  /healthz   the process answers: liveness. Async, so it answers even while every worker thread
             is busy scoring; a busy pod is slow, not dead, and must not be restarted
  /readyz    the service can do its job (`ready()`): readiness. 503 takes the pod out of the
             Service until it is ready again
Neither is counted in the request metrics: probes every few seconds would drown real traffic.
"""

import time

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

# Milliseconds to seconds: fine steps where our payments usually are, coarse ones for slow periods
BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.075, 0.1, 0.25, 0.5, 1, 2.5, 5, 10)

REQUESTS = Counter("http_requests_total", "Requests answered", ["service", "route", "method", "status"])
LATENCY = Histogram("http_request_duration_seconds", "Time to answer a request",
                    ["service", "route", "method"], buckets=BUCKETS)


NOT_MEASURED = {"/metrics", "/healthz", "/readyz"}


def instrument(app, service, before_scrape=None, ready=None):
    """before_scrape: optional function run before each /metrics read, to refresh values that
    are only known at that moment (for example which model is live).
    ready: optional function returning (ok, reason) for /readyz; default always ready. Keep it
    cheap and local (no calls to other services): it runs every few seconds."""

    @app.middleware("http")
    async def measure(request: Request, call_next):
        if request.url.path in NOT_MEASURED:
            return await call_next(request)
        start = time.perf_counter()
        status = "500"   # an exception escaping the route still reaches the client as a 500
        try:
            response = await call_next(request)
            status = str(response.status_code)
            return response
        finally:
            # In `finally`, so a request that crashed is counted too. Before, it wasn't, and a
            # 500 the client saw could be missing from the service's own error rate.
            route = request.scope.get("route")
            name = route.path if route is not None else "unmatched"
            LATENCY.labels(service, name, request.method).observe(time.perf_counter() - start)
            REQUESTS.labels(service, name, request.method, status).inc()

    @app.get("/metrics", include_in_schema=False)
    def metrics():
        if before_scrape:
            before_scrape()
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    @app.get("/healthz", include_in_schema=False)
    async def healthz():
        return {"alive": True}

    @app.get("/readyz", include_in_schema=False)
    async def readyz():
        ok, reason = ready() if ready else (True, "")
        if ok:
            return {"ready": True}
        return JSONResponse({"ready": False, "reason": reason}, status_code=503)
