from enum import Enum
from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

SRC_DIR = Path(__file__).resolve().parents[2]
ENV_FILE = SRC_DIR / ".env"


class AppSettings(BaseSettings):
    APP_NAME: str
    APP_DESCRIPTION: str
    APP_VERSION: str
    LICENSE_NAME: str
    CONTACT_NAME: str
    CONTACT_EMAIL: str


class CryptSettings(BaseSettings):
    SECRET_KEY: SecretStr
    ALGORITHM: str
    ACCESS_TOKEN_EXPIRE_MINUTES: int
    REFRESH_TOKEN_EXPIRE_DAYS: int


class FileLoggerSettings(BaseSettings):
    FILE_LOG_MAX_BYTES: int
    FILE_LOG_BACKUP_COUNT: int
    FILE_LOG_FORMAT_JSON: bool
    FILE_LOG_LEVEL: str

    FILE_LOG_INCLUDE_REQUEST_ID: bool
    FILE_LOG_INCLUDE_PATH: bool
    FILE_LOG_INCLUDE_METHOD: bool
    FILE_LOG_INCLUDE_CLIENT_HOST: bool
    FILE_LOG_INCLUDE_STATUS_CODE: bool


class ConsoleLoggerSettings(BaseSettings):
    CONSOLE_LOG_LEVEL: str
    CONSOLE_LOG_FORMAT_JSON: bool

    CONSOLE_LOG_INCLUDE_REQUEST_ID: bool
    CONSOLE_LOG_INCLUDE_PATH: bool
    CONSOLE_LOG_INCLUDE_METHOD: bool
    CONSOLE_LOG_INCLUDE_CLIENT_HOST: bool
    CONSOLE_LOG_INCLUDE_STATUS_CODE: bool


class DatabaseSettings(BaseSettings):
    pass


class PostgresSettings(DatabaseSettings):
    POSTGRES_USER: str
    POSTGRES_PASSWORD: str
    POSTGRES_SERVER: str
    POSTGRES_PORT: int
    POSTGRES_DB: str
    POSTGRES_SYNC_URL: str
    POSTGRES_ASYNC_URL: str


class FirstUserSettings(BaseSettings):
    ADMIN_NAME: str
    ADMIN_EMAIL: str
    ADMIN_USERNAME: str
    ADMIN_PASSWORD: str


class TestSettings(BaseSettings):
    ...


class RedisCacheSettings(BaseSettings):
    REDIS_CACHE_HOST: str
    REDIS_CACHE_PORT: int
    REDIS_CACHE_URL: str


class ClientSideCacheSettings(BaseSettings):
    CLIENT_CACHE_MAX_AGE: int


class RedisQueueSettings(BaseSettings):
    REDIS_QUEUE_HOST: str
    REDIS_QUEUE_PORT: int


class RedisRateLimiterSettings(BaseSettings):
    REDIS_RATE_LIMIT_HOST: str
    REDIS_RATE_LIMIT_PORT: int
    REDIS_RATE_LIMIT_URL: str


class DefaultRateLimitSettings(BaseSettings):
    DEFAULT_RATE_LIMIT_LIMIT: int
    DEFAULT_RATE_LIMIT_PERIOD: int


class CRUDAdminSettings(BaseSettings):
    CRUD_ADMIN_ENABLED: bool
    CRUD_ADMIN_MOUNT_PATH: str

    CRUD_ADMIN_ALLOWED_IPS_LIST: list[str]
    CRUD_ADMIN_ALLOWED_NETWORKS_LIST: list[str]
    CRUD_ADMIN_MAX_SESSIONS: int
    CRUD_ADMIN_SESSION_TIMEOUT: int
    SESSION_SECURE_COOKIES: bool

    CRUD_ADMIN_TRACK_EVENTS: bool
    CRUD_ADMIN_TRACK_SESSIONS: bool

    CRUD_ADMIN_REDIS_ENABLED: bool
    CRUD_ADMIN_REDIS_HOST: str
    CRUD_ADMIN_REDIS_PORT: int
    CRUD_ADMIN_REDIS_DB: int
    CRUD_ADMIN_REDIS_PASSWORD: str | None
    CRUD_ADMIN_REDIS_SSL: bool


class HealthOSArchitectureSettings(BaseSettings):
    HEALTHOS_DATABASE_NAME: str
    HEALTHOS_ARCHITECTURE_VERSION: str
    HEALTHOS_ARCHITECTURE_STAGE: str
    HEALTHOS_CORE_SCHEMAS: list[str]
    HEALTHOS_FUTURE_SCHEMAS: list[str]
    HEALTHOS_AUTH_PROVIDER: str
    HEALTHOS_FEATURE_RESOLUTION_ORDER: list[str]
    HEALTHOS_WORKFLOW_RESOLUTION_ORDER: list[str]


class LogtoSettings(BaseSettings):
    """Configuration for the backend-for-frontend Logto integration."""

    LOGTO_ENABLED: bool = False
    LOGTO_ENDPOINT: str | None = None
    LOGTO_APP_ID: str | None = None
    LOGTO_APP_SECRET: SecretStr | None = None
    LOGTO_REDIRECT_URI: str | None = None
    LOGTO_POST_LOGOUT_REDIRECT_URI: str | None = None
    AUTH_POST_LOGIN_REDIRECT_URI: str | None = None
    LOGTO_SCOPES: list[str] = ["openid", "profile", "email", "offline_access"]

    AUTH_SESSION_COOKIE_NAME: str = "healthos_session"
    AUTH_SESSION_TTL_SECONDS: int = 28_800
    AUTH_COOKIE_SECURE: bool = True
    AUTH_COOKIE_SAMESITE: str = "lax"
    AUTH_CSRF_HEADER_NAME: str = "X-CSRF-Token"


class EnvironmentOption(str, Enum):
    LOCAL = "local"
    STAGING = "staging"
    PRODUCTION = "production"


class EnvironmentSettings(BaseSettings):
    ENVIRONMENT: EnvironmentOption


class CORSSettings(BaseSettings):
    CORS_ORIGINS: list[str]
    CORS_METHODS: list[str]
    CORS_HEADERS: list[str]


class Settings(
    AppSettings,
    PostgresSettings,
    HealthOSArchitectureSettings,
    LogtoSettings,
    CryptSettings,
    FirstUserSettings,
    TestSettings,
    RedisCacheSettings,
    ClientSideCacheSettings,
    RedisQueueSettings,
    RedisRateLimiterSettings,
    DefaultRateLimitSettings,
    CRUDAdminSettings,
    EnvironmentSettings,
    CORSSettings,
    FileLoggerSettings,
    ConsoleLoggerSettings,
):
    model_config = SettingsConfigDict(
        env_file=ENV_FILE,
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )


settings = Settings()
