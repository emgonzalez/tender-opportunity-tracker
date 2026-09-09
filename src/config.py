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

    # ── Modo validación: redirige TODO el correo a estas direcciones ──────────
    # Si tiene valor, NINGÚN email (reportes ni alertas) llega a los destinatarios
    # de clients/*.yaml: todo se redirige aquí. Pensado para validar en un tenant
    # de prueba sin escribirle a los clientes reales.
    # Vive en .env (gitignored), así que no puede filtrarse a producción por commit.
    notification_override_to: str = field(
        default_factory=lambda: os.getenv("NOTIFICATION_OVERRIDE_TO", "")
    )

    @property
    def override_recipients(self) -> list[str]:
        """Lista de destinatarios del override, o [] si no está activo."""
        raw = (self.notification_override_to or "").strip()
        if not raw:
            return []
        return [addr.strip() for addr in raw.split(",") if addr.strip()]

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

    # ── Fase 2 · Drill-down de pliegos (MVP Track A) ──────────────────────
    # Feature flag maestro: en false el pipeline se comporta como v0.1.0.
    enable_drilldown: bool = field(default_factory=lambda: os.getenv("ENABLE_DRILLDOWN", "false").lower() == "true")
    # Modelos runtime de Claude para el drill-down (IDs configurables)
    model_triage: str = field(default_factory=lambda: os.getenv("MODEL_TRIAGE", "claude-haiku-4-5"))
    model_analysis: str = field(default_factory=lambda: os.getenv("MODEL_ANALYSIS", "claude-sonnet-5"))
    # Guardias de tamaño del PDF (límites de la API de Claude: 32 MB / 100 páginas)
    pliego_max_mb: int = field(default_factory=lambda: int(os.getenv("PLIEGO_MAX_MB", "32")))
    pliego_max_pages: int = field(default_factory=lambda: int(os.getenv("PLIEGO_MAX_PAGES", "100")))
    # Nº de páginas iniciales que se mandan al triaje (solo texto, barato)
    triage_pages: int = field(default_factory=lambda: int(os.getenv("TRIAGE_PAGES", "15")))
    # Paginas RASTERIZADAS que se mandan al triaje cuando el pliego es escaneado.
    # Cada pagina como imagen cuesta ~1.500-2.000 tokens, asi que este numero es
    # el principal regulador del costo del triaje. 5 alcanza para leer el
    # "Objeto de la Contratacion" y decidir GO/NO-GO.
    triage_raster_pages: int = field(default_factory=lambda: int(os.getenv("TRIAGE_RASTER_PAGES", "5")))
    # Presupuesto diario de gasto del drill-down, EN DÓLARES. En tokens no se
    # puede: el triaje (Haiku, $1/MTok) y el análisis (Sonnet, $3) no cuestan lo
    # mismo, así que un contador único de tokens no se traduce a dinero.
    daily_budget_usd: float = field(default_factory=lambda: float(os.getenv("DAILY_BUDGET_USD", "5.00")))
    # Tokens por página que se le atribuyen al análisis para la estimación
    # pre-flight. Es el único coeficiente sin medir de la guardia (el PDF lo
    # rasteriza Anthropic del lado servidor); `scripts/calibrar_cuota.py` lo
    # contrasta contra el consumo real ya persistido en el historial.
    analisis_tokens_por_pagina: int = field(default_factory=lambda: int(os.getenv("ANALISIS_TOKENS_POR_PAGINA", "3000")))
    # Directorio temporal para los PDFs descargados (se borran tras analizar)
    drilldown_tmp_dir: str = field(default_factory=lambda: os.getenv("DRILLDOWN_TMP_DIR", "/tmp/amalia_pliegos"))

    # ── Fase 2 · Track B — Memoria institucional (Oracle ADB 23ai) ────────
    # Feature flag independiente de ENABLE_DRILLDOWN: en false, todo Track B
    # es no-op y el sistema se comporta exactamente como Track A.
    enable_memoria: bool = field(default_factory=lambda: os.getenv("ENABLE_MEMORIA", "false").lower() == "true")
    adb_user: str = field(default_factory=lambda: os.getenv("ADB_USER", "AMALIA"))
    adb_password: str = field(default_factory=lambda: os.getenv("ADB_PASSWORD", ""))
    # Connection string TLS completo (port 1521, sin wallet). Se obtiene de
    # la ADB tras habilitar TLS one-way + ACL de red (ver oci/lib/05-adb.sh).
    adb_dsn: str = field(default_factory=lambda: os.getenv("ADB_DSN", ""))
    # Modelo ONNX cargado dentro de la BD (embeddings in-database, 384 dims)
    embedding_model_name: str = field(default_factory=lambda: os.getenv("EMBEDDING_MODEL_NAME", "MULTILINGUAL_E5_SMALL"))
    rag_top_k: int = field(default_factory=lambda: int(os.getenv("RAG_TOP_K", "5")))
    chunk_tokens: int = field(default_factory=lambda: int(os.getenv("CHUNK_TOKENS", "800")))
    chunk_overlap: int = field(default_factory=lambda: int(os.getenv("CHUNK_OVERLAP", "100")))
    # Máximo de precedentes inter-documento a mostrar en el reporte
    precedentes_max: int = field(default_factory=lambda: int(os.getenv("PRECEDENTES_MAX", "3")))
    # Bucket de OCI Object Storage (data-lake temporal / handoff entre etapas).
    # Vacío = usar disco local (comportamiento de Track A).
    objstore_bucket: str = field(default_factory=lambda: os.getenv("OBJSTORE_BUCKET", ""))

    @property
    def storage_state_path(self) -> str:
        return os.path.join(self.data_dir, "storage_state.json")

    @property
    def last_run_path(self) -> str:
        return os.path.join(self.data_dir, "last_run.json")

    @property
    def budget_path(self) -> str:
        """Libro mayor del gasto diario del drill-down (USD). Se resetea por fecha."""
        return os.path.join(self.data_dir, "gasto_diario.json")

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
