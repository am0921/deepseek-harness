"""Authenticated per-lane API relay with raw stream forwarding and independent telemetry."""

from __future__ import annotations

import asyncio
import hmac
import time
from typing import Any

from aiohttp import ClientSession, ClientTimeout, web

from .coordinator import Coordinator, utc_now
from .metrics import SSE, RequestMetrics


class Gateway:
    """Keep real provider keys outside the agent execution worlds."""

    def __init__(self, coordinator: Coordinator, client: ClientSession) -> None:
        self.coordinator = coordinator
        self.client = client

    async def relay(self, request: web.Request) -> web.StreamResponse:
        """Forward only Messages inference; the first pair must pass exact payload comparison."""
        controller = self.coordinator
        lane = controller.lanes.get(request.match_info["lane"])
        if not controller.active or request.match_info["run"] != controller.run_id or lane is None:
            raise web.HTTPNotFound()
        supplied = request.headers.get(
            "x-api-key", request.headers.get("Authorization", "").removeprefix("Bearer ")
        )
        if not hmac.compare_digest(supplied, lane.token):
            raise web.HTTPUnauthorized()
        if request.match_info["tail"] != "v1/messages" or request.method != "POST":
            raise web.HTTPNotFound()
        if lane.state.phase != "running":
            raise web.HTTPConflict(text="Lane is not accepting model requests")
        if lane.metrics.public()["output"] >= controller.limits.max_output_tokens:
            raise web.HTTPTooManyRequests(text="Equal per-lane output-token budget exhausted")
        response: web.StreamResponse | None = None
        try:
            body: dict[str, Any] = await request.json()
            if not isinstance(body, dict) or body.get("stream") is not True:
                raise ValueError("Expected a streaming Messages request")
            first = lane.first_hash is None
            if first:
                controller.emit("request-admission", lane.id, body=body)
                lane.first_hash = await controller.gate.arrive(lane.id, body)
                controller.emit("first-request", lane.id, sha256=lane.first_hash)
            body["model"] = lane.provider.model
            controller.emit(
                "request",
                lane.id,
                number=len(lane.metrics.requests) + 1,
                body=body,
                rates=lane.provider.public()["rates"],
            )
            metrics = RequestMetrics(time.perf_counter(), lane.provider.rates)
            lane.metrics.requests.append(metrics)
            if first:
                lane.first_sent = metrics.started
            headers = {
                "x-api-key": lane.provider.key,
                "Authorization": f"Bearer {lane.provider.key}",
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
                "Accept-Encoding": "identity",
            }
            for header in ("anthropic-version", "anthropic-beta"):
                if header in request.headers:
                    headers[header] = request.headers[header]
            url = lane.provider.base_url.rstrip("/")
            url += "/messages" if url.endswith("/v1") else "/v1/messages"
            async with self.client.post(
                url,
                json=body,
                headers=headers,
                allow_redirects=False,
                timeout=ClientTimeout(total=controller.limits.request_timeout),
            ) as upstream:
                metrics.http_status = upstream.status
                response = web.StreamResponse(
                    status=upstream.status,
                    headers={
                        "Content-Type": upstream.headers.get("Content-Type", "text/event-stream"),
                        "Cache-Control": "no-store",
                        "X-Accel-Buffering": "no",
                    },
                )
                await response.prepare(request)
                parser = SSE()
                telemetry_ok = True
                async for chunk in upstream.content.iter_any():
                    if telemetry_ok and upstream.status == 200:
                        try:
                            for event in parser.feed(chunk):
                                for channel, text in metrics.observe(event, time.perf_counter()):
                                    controller.emit("output", lane.id, channel=channel, text=text)
                        except (ValueError, TypeError, KeyError) as error:
                            telemetry_ok = False
                            metrics.error = f"Invalid usage/stream telemetry: {error}"
                            controller.emit("error", lane.id, message=metrics.error)
                    elif upstream.status != 200:
                        controller.emit(
                            "output",
                            lane.id,
                            channel="diagnostic",
                            text=chunk.decode("utf-8", "replace")[:4000],
                        )
                    await response.write(chunk)
                if telemetry_ok and upstream.status == 200:
                    for event in parser.feed(b"", final=True):
                        for channel, text in metrics.observe(event, time.perf_counter()):
                            controller.emit("output", lane.id, channel=channel, text=text)
                metrics.ended = time.perf_counter()
                if upstream.status != 200:
                    metrics.error = f"HTTP {upstream.status}"
                controller.emit("request-end", lane.id, metrics=metrics.public())
                await response.write_eof()
                return response
        except asyncio.CancelledError:
            raise
        except ConnectionResetError:
            if response is not None and response.prepared:
                return response
            raise
        except Exception as error:
            controller.emit("error", lane.id, message=f"API relay: {error}")
            raise web.HTTPBadGateway(text=str(error)) from error
        finally:
            if lane.metrics.requests and lane.metrics.requests[-1].ended is None:
                lane.metrics.requests[-1].ended = time.perf_counter()

    async def ping(self) -> None:
        """Measure authenticated model-list HTTP round trips without generating model tokens."""

        async def one(name: str) -> None:
            controller = self.coordinator
            provider = controller.providers[name]
            lane = controller.lanes.get(name)
            if not provider.key or lane is None:
                return
            started = time.perf_counter()
            try:
                async with self.client.get(
                    provider.models_url,
                    headers={"Authorization": f"Bearer {provider.key}", "x-api-key": provider.key},
                    timeout=ClientTimeout(total=5),
                    allow_redirects=False,
                ) as result:
                    await result.read()
                    if result.status != 200:
                        raise RuntimeError(f"HTTP {result.status}")
                lane.metrics.ping_ms = (time.perf_counter() - started) * 1000
                lane.metrics.ping_at = utc_now()
                lane.metrics.ping_error = None
            except Exception as error:
                lane.metrics.ping_error = str(error)

        await asyncio.gather(*(one(name) for name in self.coordinator.providers))
