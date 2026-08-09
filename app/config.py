"""Application settings loaded from environment variables."""

from __future__ import annotations

from functools import lru_cache
from typing import List

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Strongly-typed env loader.

    Each field corresponds to one env var. Optional fields default to
    safe-local-dev values so unit tests can import the app without a
    real Supabase project.
    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Supabase
    SUPABASE_URL: str = "https://example.supabase.co"
    SUPABASE_ANON_KEY: str = "anon-key"
    SUPABASE_SERVICE_ROLE_KEY: str = "service-role-key"
    SUPABASE_JWT_SECRET: str = "jwt-secret"

    # School
    SCHOOL_CODE: str = "DPS-EAST"

    # Email
    RESEND_API_KEY: str = ""
    EMAIL_FROM: str = "Trackr <noreply@trackr.app>"

    # Simulator
    SIMULATE_DRIVERS: bool = True

    # CORS
    CORS_ALLOW_ORIGINS: List[str] = Field(
        default_factory=lambda: [
            "https://trackr.app",
            "exp://localhost:8081",
        ]
    )

    @field_validator("CORS_ALLOW_ORIGINS", mode="before")
    @classmethod
    def _split_csv(cls, v):
        if isinstance(v, str):
            return [o.strip() for o in v.split(",") if o.strip()]
        return v


@lru_cache
def get_settings() -> Settings:
    return Settings()
