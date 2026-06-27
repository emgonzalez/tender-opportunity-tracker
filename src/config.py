"""Configuration loader from environment variables."""
import os
from dataclasses import dataclass, field
from dotenv import load_dotenv

load_dotenv()


@dataclass
class Config:
    # COMPRASAL
    comprasal_email: str = field(default_factory=lambda: os.environ["COMPRASAL_EMAIL"])
    comprasal_password: str = field(default_factory=lambda: os.environ["COMPRASAL_PASSWORD"])
    comprasal_totp_secret: str = field(default_factory=lambda: os.getenv("COMPRASAL_TOTP_SECRET", ""))

    # Anthropic
    anthropic_api_key: str = field(default_factory=lambda: os.environ["ANTHROPIC_API_KEY"])

    # Email / SMTP (credenciales compartidas; destinatarios por cliente en clients/*.yaml)
    smtp_host: str = field(default_factory=lambda: os.getenv("SMTP_HOST", "smtp.gmail.com"))
    smtp_port: int = field(default_factory=lambda: int(os.getenv("SMTP_PORT", "587")))
    smtp_user: str = field(default_factory=lambda: os.environ["SMTP_USER"])
    smtp_password: str = field(default_factory=lambda: os.environ["SMTP_PASSWORD"])
    notification_from: str = field(default_factory=lambda: os.getenv("NOTIFICATION_FROM", os.environ["SMTP_USER"]))

    # Schedule (HH:MM strings, El Salvador time UTC-6)
    schedule_morning: str = field(default_factory=lambda: os.getenv("SCHEDULE_MORNING", "07:00"))
    schedule_afternoon: str = field(default_factory=lambda: os.getenv("SCHEDULE_AFTERNOON", "14:00"))

    # Bot
    headless: bool = field(default_factory=lambda: os.getenv("HEADLESS", "true").lower() == "true")
    log_level: str = field(default_factory=lambda: os.getenv("LOG_LEVEL", "INFO"))
    days_lookback: int = field(default_factory=lambda: int(os.getenv("DAYS_LOOKBACK", "1")))

    # Nombre del sistema (white-label)
    app_name: str = field(default_factory=lambda: os.getenv("APP_NAME", "tender-opportunity-tracker"))
    app_full_name: str = field(default_factory=lambda: os.getenv("APP_FULL_NAME", "Tender Opportunity Tracker"))

    # Directorio con los perfiles de clientes (clients/*.yaml)
    clients_dir: str = field(default_factory=lambda: os.getenv("CLIENTS_DIR", "/app/clients"))

    # Chromium (vacío = usar bundled de Playwright; /usr/bin/chromium = sistema ARM)
    chromium_path: str = field(default_factory=lambda: os.getenv("CHROMIUM_PATH", ""))

    # Modo bajo memoria — activa flags de Chromium para reducir su footprint
    # (apropiado en VMs con <= 2 GB RAM como VM.Standard.E2.1.Micro)
    low_memory_mode: bool = field(default_factory=lambda: os.getenv("LOW_MEMORY_MODE", "false").lower() == "true")

    # Paths
    data_dir: str = field(default_factory=lambda: os.getenv("DATA_DIR", "/app/data"))

    @property
    def storage_state_path(self) -> str:
        return os.path.join(self.data_dir, "storage_state.json")

    @property
    def last_run_path(self) -> str:
        return os.path.join(self.data_dir, "last_run.json")

    @property
    def screenshots_dir(self) -> str:
        return os.path.join(self.data_dir, "screenshots")

    @property
    def debug_dir(self) -> str:
        return os.path.join(self.data_dir, "debug")

    @property
    def totp_enabled(self) -> bool:
        return bool(self.comprasal_totp_secret)


# Singleton
_config: Config | None = None


def get_config() -> Config:
    global _config
    if _config is None:
        _config = Config()
    return _config
