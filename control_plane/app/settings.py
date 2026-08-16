from functools import lru_cache

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import URL


class Settings(BaseSettings):
    app_name: str = "AI Orchestra Control Plane"
    environment: str = "production"
    database_url: str | None = None
    db_host: str = "postgres"
    db_port: int = 5432
    db_name: str = "ai_orchestra"
    db_user: str = "ai_orchestra"
    db_password: str = ""
    server_username: str = "manager"
    server_password: str = "CHANGE_ME_MANAGER_PASSWORD"
    opencode_url: str = "http://127.0.0.1:4096"
    default_monthly_budget: float = 25000.0

    model_config = SettingsConfigDict(
        env_prefix="CONTROL_PLANE_",
        case_sensitive=False,
        extra="ignore",
    )

    @model_validator(mode="after")
    def validate_production_secrets(self) -> "Settings":
        if self.environment.lower() != "test":
            if (
                self.server_password == "CHANGE_ME_MANAGER_PASSWORD"
                or len(self.server_password) < 20
            ):
                raise ValueError(
                    "CONTROL_PLANE_SERVER_PASSWORD должен быть заменен и содержать не менее 20 символов"
                )
            if not self.database_url and len(self.db_password) < 20:
                raise ValueError(
                    "CONTROL_PLANE_DB_PASSWORD должен содержать не менее 20 символов"
                )
        return self

    def sqlalchemy_url(self) -> str | URL:
        if self.database_url:
            return self.database_url
        return URL.create(
            drivername="postgresql+psycopg",
            username=self.db_user,
            password=self.db_password,
            host=self.db_host,
            port=self.db_port,
            database=self.db_name,
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
