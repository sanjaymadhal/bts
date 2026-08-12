"""FastAPI entry point + lifespan wiring for the simulator."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config import get_settings
from .geocode import router as geocode_router
from .auth import router as auth_router
from .buses import router as buses_router
from .students import router as students_router
from .invitations import router as invitations_router
from .settings import router as settings_router
from .positions import router as positions_router
from .profiles import router as profiles_router
from .notifications import router as notifications_router
from .simulator import start_simulator, stop_simulator


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start the in-process driver simulator on app startup."""
    settings = get_settings()
    task: asyncio.Task | None = None
    if settings.SIMULATE_DRIVERS:
        task = await start_simulator()
    yield
    if task is not None:
        await stop_simulator(task)


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="Trackr API",
        version="1.0.0",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.CORS_ALLOW_ORIGINS,
        allow_credentials=True,
        # OPTIONS is handled automatically by Starlette's CORS middleware
        # when preflight requests arrive; explicit listing here is fine
        # but not required.
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
    )

    # Routers
    app.include_router(auth_router, prefix="/auth", tags=["auth"])
    app.include_router(profiles_router, prefix="/profiles", tags=["profiles"])
    app.include_router(buses_router, prefix="/buses", tags=["buses"])
    app.include_router(students_router, prefix="/students", tags=["students"])
    app.include_router(invitations_router, prefix="/invitations", tags=["invitations"])
    app.include_router(settings_router, prefix="/settings", tags=["settings"])
    app.include_router(positions_router, prefix="/positions", tags=["positions"])
    app.include_router(notifications_router, prefix="/notifications", tags=["notifications"])
    app.include_router(geocode_router, tags=["geocode"])

    @app.get("/health", tags=["health"])
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
