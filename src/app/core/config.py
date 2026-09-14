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
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7


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
    POSTGRES_SYNC_URL: str
    POSTGRES_ASYNC_URL: str


class FirstUserSettings(BaseSettings):
    ADMIN_NAME: str = "HealthOS Admin"
    ADMIN_EMAIL: str = "admin@healthos.local"
    ADMIN_USERNAME: str = "admin"
    ADMIN_PASSWORD: str = ""


class TestSettings(BaseSettings):
    ...


class ClientSideCacheSettings(BaseSettings):
    CLIENT_CACHE_MAX_AGE: int


class CRUDAdminSettings(BaseSettings):
    CRUD_ADMIN_ENABLED: bool = False
    CRUD_ADMIN_MOUNT_PATH: str = "/admin"

    CRUD_ADMIN_ALLOWED_IPS_LIST: list[str] = []
    CRUD_ADMIN_ALLOWED_NETWORKS_LIST: list[str] = []
    CRUD_ADMIN_MAX_SESSIONS: int = 10
    CRUD_ADMIN_SESSION_TIMEOUT: int = 1440
    SESSION_SECURE_COOKIES: bool = True

    CRUD_ADMIN_TRACK_EVENTS: bool = False
    CRUD_ADMIN_TRACK_SESSIONS: bool = False



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
    AUTH_POST_REGISTRATION_REDIRECT_URI: str | None = None
    # Request the minimum required claim set. Add profile/email only after enabling
    # those user-data permissions for the Logto application.
    # Keep this limited to the scope enabled by the current Logto application.
    # Add profile/email only after enabling those permissions in Logto.
    LOGTO_SCOPES: list[str] = ["openid"]
    LOGTO_ORGANIZATIONS_ENABLED: bool = False
    LOGTO_MANAGEMENT_API_BASE_URL: str | None = None
    LOGTO_MANAGEMENT_TOKEN_ENDPOINT: str | None = None
    LOGTO_MANAGEMENT_APP_ID: str | None = None
    LOGTO_MANAGEMENT_APP_SECRET: SecretStr | None = None
    LOGTO_MANAGEMENT_SCOPE: str = "all"
    LOGTO_ORGANIZATION_ADMIN_ROLE_ID: str | None = None
    LOGTO_ORGANIZATION_ROLE_IDS: dict[str, str] = {}

    AUTH_SESSION_COOKIE_NAME: str = "healthos_session"
    AUTH_SESSION_TTL_SECONDS: int = 28_800
    AUTH_COOKIE_SECURE: bool = True
    AUTH_COOKIE_SAMESITE: str = "lax"
    AUTH_CSRF_HEADER_NAME: str = "X-CSRF-Token"
    AUTH_LOCAL_DEV_BYPASS: bool = False


class EnvironmentOption(str, Enum):
    LOCAL = "local"
    STAGING = "staging"
    PRODUCTION = "production"


class EnvironmentSettings(BaseSettings):
    ENVIRONMENT: EnvironmentOption


class CORSSettings(BaseSettings):
    CORS_ORIGINS: list[str] = ["http://localhost:5173", "http://127.0.0.1:5173"]
    CORS_METHODS: list[str] = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]
    CORS_HEADERS: list[str] = [
        "Authorization",
        "Content-Type",
        "X-CSRF-Token",
        "X-Client-Timezone",
        "X-Client-Date",
        "X-Client-Timestamp",
    ]


class Settings(
    AppSettings,
    PostgresSettings,
    HealthOSArchitectureSettings,
    LogtoSettings,
    CryptSettings,
    FirstUserSettings,
    TestSettings,
    ClientSideCacheSettings,
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
