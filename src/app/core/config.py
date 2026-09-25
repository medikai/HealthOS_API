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


class RedisSettings(BaseSettings):
    REDIS_QUEUE_HOST: str = "localhost"
    REDIS_QUEUE_PORT: int = 6379


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


class PatientDocumentSettings(BaseSettings):
    GCS_BUCKET_NAME: str | None = None
    GCS_SIGNING_SERVICE_ACCOUNT: str | None = None


class CommunicationSettings(BaseSettings):
    """Durable delivery/job worker configuration for the communication domain."""

    COMMUNICATION_ENABLED: bool = True
    COMMUNICATION_WORKER_BATCH_SIZE: int = 25
    COMMUNICATION_WORKER_POLL_SECONDS: float = 2.0
    # Hard ceiling for one dispatch pass. A stalled provider/network call must
    # never pause the claim/reclaim loop indefinitely; a timed-out pass leaves
    # jobs leased and they are reclaimed after COMMUNICATION_JOB_LEASE_SECONDS.
    # This is a hang backstop, not a latency SLA: keep it comfortably above a
    # full batch's normal publish time.
    COMMUNICATION_WORKER_PASS_TIMEOUT_SECONDS: float = 120.0
    COMMUNICATION_WORKER_MAINTENANCE_EVERY_PASSES: int = 60
    COMMUNICATION_JOB_LEASE_SECONDS: int = 60
    COMMUNICATION_JOB_MAX_ATTEMPTS: int = 5
    COMMUNICATION_JOB_BASE_BACKOFF_SECONDS: int = 5
    COMMUNICATION_JOB_MAX_BACKOFF_SECONDS: int = 300
    COMMUNICATION_JOB_RETENTION_DAYS: int = 30
    COMMUNICATION_DEV_INPROCESS_DISPATCH: bool = False
    COMMUNICATION_MAX_PRESENCE_CHANNELS: int = 10
    COMMUNICATION_CHAT_MAX_GROUP_MEMBERS: int = 20
    COMMUNICATION_CHAT_MAX_MESSAGE_LENGTH: int = 4000
    COMMUNICATION_CHAT_HISTORY_PAGE_SIZE: int = 50
    COMMUNICATION_CHAT_NOTIFY_DEBOUNCE_SECONDS: int = 300
    # App-level key protecting short-lived secret email context (OTP/reset URLs).
    # Falls back to SECRET_KEY derivation when unset.
    COMMUNICATION_SECRET_CONTEXT_KEY: SecretStr | None = None


class FirebaseSettings(BaseSettings):
    """Backend-only FCM server configuration.

    Missing project/credentials means push is unavailable (never a dummy send);
    the SDK is imported lazily so absence does not break unrelated APIs. The
    public web config/VAPID key are frontend settings, not these values.
    """

    FCM_ENABLED: bool = True
    FIREBASE_PROJECT_ID: str | None = None
    # Path to a service-account JSON file (server-only). Preferred over inline
    # credentials; never committed and never logged.
    FIREBASE_SERVICE_ACCOUNT_FILE: str | None = None
    FIREBASE_CREDENTIALS_JSON: SecretStr | None = None
    FCM_DEFAULT_TTL_SECONDS: int = 3600


class RecoverySettings(BaseSettings):
    """Local-auth password recovery policy and limits (auth-owned)."""

    AUTH_RECOVERY_CODE_TTL_SECONDS: int = 600
    AUTH_RECOVERY_RESEND_COOLDOWN_SECONDS: int = 60
    AUTH_RECOVERY_MAX_ATTEMPTS: int = 5
    AUTH_RECOVERY_ACCOUNT_LIMIT: int = 5
    AUTH_RECOVERY_IP_LIMIT: int = 20
    AUTH_RECOVERY_THROTTLE_WINDOW_SECONDS: int = 3600
    AUTH_RECOVERY_GRANT_TTL_SECONDS: int = 600
    AUTH_RECOVERY_CODE_LENGTH: int = 6
    # Keyed verifier pepper; falls back to SECRET_KEY derivation when unset.
    AUTH_RECOVERY_PEPPER: SecretStr | None = None


class ZeptoMailSettings(BaseSettings):
    """Backend-only ZeptoMail transport configuration.

    A missing ``ZEPTOMAIL_SEND_TOKEN`` means email is unavailable, never a
    dummy-success send.
    """

    ZEPTOMAIL_API_BASE_URL: str = "https://api.zeptomail.com"
    ZEPTOMAIL_SEND_TOKEN: SecretStr | None = None
    EMAIL_FROM_ADDRESS: str = "noreply@medikai.in"
    EMAIL_FROM_NAME: str = "Medikai Infodesk"
    EMAIL_TIMEOUT_SECONDS: float = 10.0
    EMAIL_MAX_ATTEMPTS: int = 5


class AblySettings(BaseSettings):
    """Backend-only Ably credentials and scoped client-token policy.

    The server API key must never be exposed to the browser; only signed,
    short-lived token requests or token details are returned.
    """

    ABLY_API_KEY: SecretStr | None = None
    ABLY_CHANNEL_NAMESPACE: str = "dev"
    ABLY_TOKEN_TTL_SECONDS: int = 1800
    ABLY_TOKEN_RENEW_AFTER_SECONDS: int = 900
    ABLY_PRESENCE_ENABLED: bool = True


class Settings(
    AppSettings,
    PostgresSettings,
    RedisSettings,
    HealthOSArchitectureSettings,
    LogtoSettings,
    CryptSettings,
    FirstUserSettings,
    TestSettings,
    ClientSideCacheSettings,
    CRUDAdminSettings,
    EnvironmentSettings,
    CORSSettings,
    PatientDocumentSettings,
    CommunicationSettings,
    AblySettings,
    ZeptoMailSettings,
    RecoverySettings,
    FirebaseSettings,
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
