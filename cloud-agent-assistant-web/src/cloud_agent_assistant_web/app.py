from __future__ import annotations

import json
import os
import re
import secrets
import asyncio
from dataclasses import replace
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Route, Mount
from starlette.staticfiles import StaticFiles

from fde_oci_agent import __version__ as core_version
from fde_oci_agent.config import Settings
from . import __version__
from .jobs import Manager
from .clouds import providers
from .runtime import host_os
from .compartments import lookup_compartment


class TaskInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request: str = Field(min_length=1, max_length=16000)
    cloud: Literal['oci', 'azure', 'gcp', 'aws'] = 'oci'
    profile: str = Field(default='default', min_length=1, max_length=100)
    region: str = Field(min_length=1, max_length=80)
    compartment_id: str = Field(default='', max_length=300)
    scope: str = Field(default='', max_length=300)
    plan_only: bool = False


class InputReply(BaseModel):
    model_config = ConfigDict(extra="forbid")
    prompt_id: str = Field(pattern=r"^[a-f0-9]{24}$")
    answer: str = Field(max_length=16000)


async def payload(request: Request):
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > 65536:
            raise ValueError("Request too large")
    return json.loads(raw)


class LocalSessionMiddleware(BaseHTTPMiddleware):
    """Local browser sessions, same-origin writes, CSRF and DNS-rebinding checks."""
    def __init__(self, app, hosts):
        super().__init__(app)
        self.hosts = set(hosts)
        self.sessions = {}

    async def dispatch(self, request, call_next):
        if request.url.hostname not in self.hosts:
            return JSONResponse({"error": "Unrecognized host"}, status_code=400)
        if request.headers.get("sec-fetch-site") == "cross-site":
            return JSONResponse({"error": "Use the local application address"}, status_code=403)
        origin = request.headers.get("origin")
        if origin and origin != f"{request.url.scheme}://{request.headers.get('host')}":
            return JSONResponse({"error": "Origin mismatch"}, status_code=403)
        if request.url.path == "/health":
            return await call_next(request)
        cookie = request.cookies.get("cloud_agent_session")
        known = cookie in self.sessions if cookie else False
        if request.method not in {"GET", "HEAD"}:
            if (not known or not secrets.compare_digest(request.headers.get("x-csrf-token", ""), self.sessions[cookie])):
                return JSONResponse({"error": "Browser session expired. Reload the page."}, status_code=403)
            if request.headers.get("content-type", "").split(";", 1)[0] != "application/json":
                return JSONResponse({"error": "Expected JSON request"}, status_code=415)
        if not known:
            cookie = secrets.token_urlsafe(32)
            if len(self.sessions) >= 128:
                self.sessions.pop(next(iter(self.sessions)))
            self.sessions[cookie] = secrets.token_urlsafe(32)
        request.state.csrf = self.sessions[cookie]
        response = await call_next(request)
        if not known:
            response.set_cookie("cloud_agent_session", cookie, httponly=True, samesite="strict", max_age=86400)
        response.headers.update({"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
                                 "Referrer-Policy": "no-referrer",
                                 "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"})
        return response


def create_app(settings: Settings, state_dir: Path, *, manager_factory=Manager, hosts=("127.0.0.1", "localhost"), compartment_lookup=lookup_compartment):
    @asynccontextmanager
    async def lifespan(app):
        app.state.manager = manager_factory(settings, state_dir)
        yield
        app.state.manager.close()

    static = Path(__file__).parent / "static"

    async def index(request):
        return FileResponse(static / "index.html")

    async def health(request):
        return JSONResponse(dict(service="cloud-agent-assistant-web", version=__version__,
                                 core_version=core_version, status="ok", pid=os.getpid(), operating_system=host_os()))

    async def bootstrap(request):
        return JSONResponse(dict(csrf=request.state.csrf, version=__version__, core_version=core_version,
                                 providers=providers(settings), profiles=settings.profiles(), defaults=dict(profile=settings.profile, region=settings.region,
                                 compartment_id=settings.compartment_id), model=settings.model_name,
                                 model_region=settings.model_region, poll_interval_seconds=settings.poll_interval,
                                 wait_timeout_seconds=settings.wait_timeout))

    async def jobs(request):
        manager = request.app.state.manager
        if request.method == "GET":
            return JSONResponse({"jobs": manager.store.listing()})
        try:
            value = TaskInput.model_validate(await payload(request))
            if not value.request.strip():
                raise ValueError("Enter a cloud task")
            job = manager.submit(**value.model_dump())
        except (ValueError, ValidationError):
            return JSONResponse({"error": "Task could not be queued. Check the cloud, profile, region, account/subscription/project/compartment, and request."}, status_code=400)
        return JSONResponse(job.public(), status_code=201)

    async def compartment(request):
        try:
            profile = request.query_params.get('profile', settings.profile)
            region = request.query_params.get('region', settings.region)
            if not re.fullmatch(r'[a-z][a-z0-9-]{1,79}', region):
                raise ValueError('Invalid region')
            selected = replace(settings, profile=profile, region=region)
            selected.connection()
            value = await asyncio.to_thread(compartment_lookup, selected, request.query_params.get('ocid', settings.compartment_id))
            return JSONResponse(value)
        except Exception:
            return JSONResponse({'error': 'Compartment name unavailable. Check the OCI profile, OCID, and permission to read this compartment.'}, status_code=400)

    async def job(request):
        manager = request.app.state.manager
        value = manager.get(request.path_params["job_id"])
        if value is None:
            return JSONResponse({"error": "Task not found"}, status_code=404)
        try:
            after = max(0, int(request.query_params.get("after", "0")))
        except ValueError:
            return JSONResponse({"error": "Invalid event cursor"}, status_code=400)
        return JSONResponse(dict(job=value, events=manager.store.events(value["id"], after)))

    async def respond(request):
        try:
            value = InputReply.model_validate(await payload(request))
            request.app.state.manager.respond(request.path_params["job_id"], value.prompt_id, value.answer)
        except (ValueError, ValidationError):
            return JSONResponse({"error": "Input is invalid or no longer active. Refresh the task and retry."}, status_code=409)
        return JSONResponse({"status": "received"})

    async def cancel(request):
        manager = request.app.state.manager
        if manager.get(request.path_params["job_id"]) is None:
            return JSONResponse({"error": "Task not found"}, status_code=404)
        manager.cancel(request.path_params["job_id"])
        return JSONResponse({"status": "cancellation_requested"})

    app = Starlette(lifespan=lifespan, routes=[
        Route("/", index), Route("/health", health), Route("/api/bootstrap", bootstrap),
        Route('/api/oci/compartment', compartment),
        Route("/api/jobs", jobs, methods=["GET", "POST"]),
        Route("/api/jobs/{job_id:str}", job),
        Route("/api/jobs/{job_id:str}/respond", respond, methods=["POST"]),
        Route("/api/jobs/{job_id:str}/cancel", cancel, methods=["POST"]),
        Mount("/static", StaticFiles(directory=static), name="static"),
    ])
    app.add_middleware(LocalSessionMiddleware, hosts=hosts)
    return app
