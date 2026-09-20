"""Application settings loaded from environment variables."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import List

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _load_dotenv() -> dict[str, str]:
    """Minimal .env loader.

    Parses KEY=VALUE lines from the local .env (without JSON-decoding
    list values — pydantic-settings v2 does that and breaks CSV strings).
    """
    env_path = Path(".env")
    if not env_path.exists():
        return {}
    out: dict[str, str] = {}
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip().strip('"').strip("'")
    return out


class Settings(BaseSettings):
    """Strongly-typed env loader.

    Each field corresponds to one env var. Optional fields default to
    safe-local-dev values so unit tests can import the app without a
    real Supabase project.
    """

    # No env_file here — we load it ourselves below so list-typed fields
    # can be CSV without pydantic-settings trying to JSON-decode them.
    model_config = SettingsConfigDict(extra="ignore")

    # Supabase
    SUPABASE_URL: str = "https://example.supabase.co"
    SUPABASE_ANON_KEY: str = "anon-key"
    SUPABASE_SERVICE_ROLE_KEY: str = "service-role-key"
    SUPABASE_JWT_SECRET: str = "jwt-secret"

    # School
    SCHOOL_CODE: str = "DPS-EAST"

    # Email — sent via Resend HTTP API.
    RESEND_API_KEY: str = ""
    EMAIL_FROM: str = "Trackr <onboarding@resend.dev>"

    # Notifications — Expo push transport. Defaults to True so the
    # backend ships ready-to-send; tests set this to False so the
    # fan-out doesn't actually hit Expo's HTTP endpoint.
    EXPO_PUSH_ENABLED: bool = True

    # Routing — OSRM base URL. Public demo server is often rate-limited.
    OSRM_BASE_URL: str = "https://router.project-osrm.org"

    # CORS — comma-separated list in the env. Stored as a parsed list.
    # Includes the production EAS bundle origin so the deployed mobile
    # app can hit the API. Add your own deployment to the env var in
    # production — the defaults are local-dev safe.
    CORS_ALLOW_ORIGINS: List[str] = [
        "https://trackr.app",
        "exp://localhost:8081",
    ]

    # Position-ingest shared secret. Distinct from
    # SUPABASE_SERVICE_ROLE_KEY so a leaked bearer token can't write
    # arbitrary positions. Generate per-environment with
    # `python -c "import secrets; print(secrets.token_urlsafe(32))"`.
    POSITION_SECRET: str = ""

    @field_validator("CORS_ALLOW_ORIGINS", mode="before")
    @classmethod
    def _split_csv(cls, v):
        if isinstance(v, str):
            return [o.strip() for o in v.split(",") if o.strip()]
        return v


@lru_cache
def get_settings() -> Settings:
    """Build a Settings from the current environment + the .env file.

    Env vars (already in os.environ) take precedence; anything missing
    is filled in from .env. CORS_ALLOW_ORIGINS is parsed as CSV so the
    comma-separated env value becomes a list. Booleans are coerced from
    the common "true"/"false" strings.

    Empty values in the .env file are treated as "unset" so a placeholder
    like `SUPABASE_JWT_SECRET=` doesn't blank out the field's default.
    """
    dotenv = _load_dotenv()
    merged: dict[str, str] = {k: v for k, v in dotenv.items() if v != ""}
    for key, value in os.environ.items():
        if value != "":
            merged[key] = value

    overrides: dict[str, object] = {}
    for key in Settings.model_fields:
        if key in merged:
            overrides[key] = merged[key]

    if "CORS_ALLOW_ORIGINS" in overrides and isinstance(overrides["CORS_ALLOW_ORIGINS"], str):
        overrides["CORS_ALLOW_ORIGINS"] = [
            o.strip() for o in overrides["CORS_ALLOW_ORIGINS"].split(",") if o.strip()
        ]

    if "SIMULATE_DRIVERS" in overrides and isinstance(overrides["SIMULATE_DRIVERS"], str):
        overrides["SIMULATE_DRIVERS"] = overrides["SIMULATE_DRIVERS"].lower() in {
            "true", "1", "yes", "on"
        }

    if "EXPO_PUSH_ENABLED" in overrides and isinstance(overrides["EXPO_PUSH_ENABLED"], str):
        overrides["EXPO_PUSH_ENABLED"] = overrides["EXPO_PUSH_ENABLED"].lower() in {
            "true", "1", "yes", "on"
        }

    return Settings(**overrides)
