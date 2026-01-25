"""
Configuration settings for the Licensing API
"""
from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # App
    app_name: str = "Iudex Licensing API"
    app_version: str = "1.0.0"
    environment: Literal["development", "staging", "production"] = "development"
    debug: bool = False

    # Server
    host: str = "0.0.0.0"
    port: int = 8000

    # Database
    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/iudex_licensing"
    database_pool_size: int = 5
    database_max_overflow: int = 10

    @property
    def async_database_url(self) -> str:
        """Convert standard PostgreSQL URL to async format."""
        url = self.database_url
        if url.startswith("postgres://"):
            url = url.replace("postgres://", "postgresql+asyncpg://", 1)
        elif url.startswith("postgresql://"):
            url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
        return url

    # Redis
    redis_url: str = "redis://localhost:6379/0"

    # Stripe
    stripe_secret_key: str = ""
    stripe_webhook_secret: str = ""
    stripe_publishable_key: str = ""

    # JWT
    jwt_secret_key: str = "change-me-in-production"
    jwt_algorithm: str = "HS256"
    jwt_access_token_expire_minutes: int = 30
    jwt_refresh_token_expire_days: int = 7

    # Google OAuth
    google_client_id: str = ""  # Add from Google Cloud Console
    google_client_secret: str = ""  # Add from Google Cloud Console
    google_redirect_uri: str = "http://localhost:8000/api/v1/auth/google/callback"

    # Session secret for OAuth state
    session_secret_key: str = "change-me-in-production-session"

    # CORS
    cors_origins: list[str] = ["http://localhost:3000", "https://iudex.com.br"]

    # URLs
    frontend_url: str = "https://iudex.com.br"
    api_url: str = "https://api.iudex.com.br"

    # Trial
    trial_days: int = 7

    @property
    def is_production(self) -> bool:
        return self.environment == "production"


@lru_cache
def get_settings() -> Settings:
    """Get cached settings instance."""
    return Settings()


settings = get_settings()
