"""Configuration loader from environment variables."""
import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()


@dataclass
class Config:
    # ── Identidad del artefacto ──────────────────────────────────────────────
    # SHA del commit horneado en la imagen (ARG GIT_SHA → ENV APP_VERSION en los
    # Dockerfiles). Se escribe en last_run.json para poder responder "qué código
    # produjo el reporte de esta mañana" sin entrar por SSH a mirar el árbol.
    # Vale "dev" cuando se corre fuera de una imagen construida por CI.
    app_version: str = field(default_factory=lambda: os.getenv("APP_VERSION", "dev"))

    # COMPRASAL
    comprasal_email: str = field(default_factory=lambda: os.environ["COMPRASAL_EMAIL"])
    comprasal_password: str = field(default_factory=lambda: os.environ["COMPRASAL_PASSWORD"])
    comprasal_totp_secret: str = field(default_factory=lambda: os.getenv("COMPRASAL_TOTP_SECRET", ""))

    # ── Proveedor de LLM ─────────────────────────────────────────────────────
    # Una sola perilla para las CINCO llamadas al modelo del sistema (triaje,
    # análisis, clasificación y las dos del trainer). "anthropic" | "oci".
    # Se normaliza acá —strip + lower— para que un "OCI " con espacio en el .env
    # de una VM no degrade en silencio al proveedor equivocado; la validación de
    # que el valor EXISTE la hace llm.validar_configuracion() al arrancar.
    llm_provider: str = field(default_factory=lambda: os.getenv("LLM_PROVIDER", "anthropic").strip().lower())

    # Segunda perilla, ortogonal a la de arriba: DENTRO de OCI, qué familia de
    # modelos se usa por defecto en las cuatro etapas. No hace nada mientras
    # LLM_PROVIDER=anthropic. "nativo" | "xai" | "google":
    #   nativo → meta.llama-4-scout / -maverick. Techo de salida MEDIDO: 4.096.
    #   xai    → xai.grok-4.6.            Techo medido: 200.000. Latencia ~6,3 s.
    #   google → google.gemini-2.5-flash. Techo medido: 65.536 (16× el de Meta:
    #            es la única familia con la que el análisis recibe de verdad los
    #            16.000 tokens de salida que pide).
    # ⚠️ xai y google los EXPONE OCI pero los RESUELVE llamando a servidores de
    # xAI y de Google: encenderlas saca los pliegos de los clientes de la
    # infraestructura de OCI y se los entrega a un tercero que ningún contrato
    # contempla. Por eso el default es "nativo" y llm.validar_configuracion()
    # loguea un WARNING nombrando al tercero cuando no lo es. Tampoco tienen
    # tarifa medida: la guardia de presupuesto las cobra como el modelo más caro.
    # Se normaliza acá —strip + lower— por la misma razón que llm_provider; que
    # el valor EXISTA lo valida llm.validar_configuracion() al arrancar.
    oci_familia: str = field(default_factory=lambda: os.getenv("OCI_FAMILIA", "nativo").strip().lower())

    # ── Libro de consumo del LLM (src/llm/registro.py) ───────────────────────
    # Una línea JSONL por llamada al modelo: caracteres enviados, tokens
    # reportados, latencia, familia y costo estimado. Encendido por DEFAULT: el
    # sistema va a correr contra OCI un tiempo para poder compararlo con Claude
    # con datos y no con estimaciones, y un libro que hay que acordarse de
    # encender llega vacío justo el día que hace falta leerlo.
    # Apagarlo (LLM_REGISTRO_HABILITADO=false) no rompe nada: solo deja el hueco.
    llm_registro_habilitado: bool = field(default_factory=lambda: os.getenv("LLM_REGISTRO_HABILITADO", "true").lower() == "true")

    # CAMBIO: era os.environ[...], que lanzaba KeyError al construir Config().
    # Con LLM_PROVIDER=oci la clave de Anthropic no existe, y ese KeyError mataba
    # la app antes de poder siquiera elegir proveedor. La validación se movió a
    # llm.validar_configuracion(), que solo exige la credencial del proveedor
    # ACTIVO. Ojo: el fallo por clave faltante deja de ocurrir al construir la
    # Config y pasa a ocurrir al arrancar main.py — más tarde, pero igual de
    # temprano que cualquier corrida real.
    anthropic_api_key: str = field(default_factory=lambda: os.getenv("ANTHROPIC_API_KEY", ""))

    # ── Credenciales de OCI Generative AI ────────────────────────────────────
    # Dos caminos, en este orden de precedencia: las cuatro variables de la API
    # key (el camino de la VM, que las recibe en el .env desde oci/lib/04-app.sh)
    # o un ~/.oci/config con perfiles (el camino de la laptop del operador).
    # Todas vacías por default: sin LLM_PROVIDER=oci no hacen falta.
    oci_user_ocid: str = field(default_factory=lambda: os.getenv("OCI_USER_OCID", ""))
    oci_tenancy_ocid: str = field(default_factory=lambda: os.getenv("OCI_TENANCY_OCID", ""))
    oci_fingerprint: str = field(default_factory=lambda: os.getenv("OCI_FINGERPRINT", ""))
    # Clave privada PEM COMPLETA, con los saltos de línea escapados como la
    # secuencia LITERAL \n para que entre en una línea del .env. Es un secreto:
    # NUNCA se loguea (para eso existe la property oci_credenciales_presentes).
    #
    # ⚠️ Contrato con el adaptador: este campo llega con la secuencia \n SIN
    # interpretar —oci/lib/04-app.sh la escribe sin comillas a propósito, para
    # que ni python-dotenv ni docker compose decidan por su cuenta si la
    # desescapan— así que quien lo consuma tiene que hacer
    # `.replace("\\n", "\n")` antes de pasárselo al SDK en `private_key_content`,
    # que espera saltos de línea de verdad. Verificado de punta a punta: el
    # replace reconstruye el archivo byte por byte y cryptography lo parsea.
    oci_key_content: str = field(default_factory=lambda: os.getenv("OCI_KEY_CONTENT", ""))
    # ⚠️ us-chicago-1, NO us-ashburn-1 (donde viven las VMs). Medido el
    # 2026-09-19: OCI Generative AI NO sirve inferencia en Ashburn, y cuando la
    # región no tiene el servicio devuelve `404 Entity with key <modelo> not
    # found` — un error que se confunde con un ID de modelo mal escrito o con
    # falta de permisos, y que ya costó una hora de diagnóstico. El tenant
    # multicomputos está suscrito a Chicago; la llamada sale cross-region desde
    # la VM (HTTPS a endpoint público: funciona, suma latencia).
    oci_region: str = field(default_factory=lambda: os.getenv("OCI_REGION", "us-chicago-1"))
    # Camino alternativo de credencial: archivo de perfiles del CLI de OCI.
    # Vacío = el default del SDK (~/.oci/config).
    oci_config_file: str = field(default_factory=lambda: os.getenv("OCI_CONFIG_FILE", ""))
    oci_config_profile: str = field(default_factory=lambda: os.getenv("OCI_CONFIG_PROFILE", "DEFAULT"))
    # Compartment donde se factura y autoriza la inferencia. Vacío = hay que
    # completarlo antes de encender el proveedor; no hay default razonable.
    oci_genai_compartment_ocid: str = field(default_factory=lambda: os.getenv("OCI_GENAI_COMPARTMENT_OCID", ""))

    # ── IDs de modelo por etapa y proveedor ──────────────────────────────────
    # Variables SEPARADAS por proveedor a propósito: si una sola variable
    # gobernara los dos, cambiar LLM_PROVIDER=oci en una VM ya desplegada le
    # mandaría "claude-haiku-4-5" como model_id a OCI y fallaría con un 404
    # confuso. Vacío = el default de src/llm/modelos.py para esa etapa, que es
    # funcional: no hace falta tocar ninguna de estas cuatro.
    oci_model_triage: str = field(default_factory=lambda: os.getenv("OCI_MODEL_TRIAGE", ""))
    oci_model_analysis: str = field(default_factory=lambda: os.getenv("OCI_MODEL_ANALYSIS", ""))
    oci_model_classifier: str = field(default_factory=lambda: os.getenv("OCI_MODEL_CLASSIFIER", ""))
    oci_model_trainer: str = field(default_factory=lambda: os.getenv("OCI_MODEL_TRAINER", ""))
    # Lado Anthropic de la misma tabla. Antes estaban hardcodeados dentro de
    # classifier/engine.py y classifier/trainer.py, lo que dejaba dos de los
    # cinco call sites fuera de toda configuración.
    model_classifier: str = field(default_factory=lambda: os.getenv("MODEL_CLASSIFIER", "claude-haiku-4-5-20251001"))
    model_trainer: str = field(default_factory=lambda: os.getenv("MODEL_TRAINER", "claude-haiku-4-5-20251001"))

    # ── Topes y tamaños de lote propios de OCI ───────────────────────────────
    # Tope de tokens de SALIDA. Los valores son MEDIDOS (2026-09-19), no
    # documentados: por encima el request se rechaza con `400 Invalid
    # 'maxTokens': Value is greater than maximum: <techo>`. No es un
    # truncamiento silencioso, es un error duro, así que el clamp del adaptador
    # es obligatorio y no una mejora.
    #
    # ⚠️ 0 = automático, y es el default A PROPÓSITO: el techo es **por modelo**
    # (`modelos.TECHO_SALIDA`) y no por proveedor. Dejar acá el 4096 de Meta
    # —que es lo que había hasta la Fase 9b— le recortaría a Gemini sus 65.536 a
    # 4.096 sin que nadie lo pida, anulando en silencio la razón de ser de la
    # familia. Esta perilla existe solo para BAJAR el techo a mano; nunca puede
    # subirlo por encima del máximo medido del modelo.
    oci_max_tokens_salida: int = field(default_factory=lambda: int(os.getenv("OCI_MAX_TOKENS_SALIDA", "0")))
    # Con OCI, un pliego ESCANEADO se analiza rasterizando sus primeras N
    # páginas: el adaptador no tiene equivalente del bloque `document` de
    # Anthropic, que recibe el PDF entero. Es una PÉRDIDA real —un pliego de 60
    # páginas se analiza viendo 20— y esta es la perilla: subirla sube el costo
    # y la presión de RAM linealmente.
    # 10 y no 20: es lo que Llama 4 acepta por request (`modelos.TECHO_IMAGENES`).
    # Pedir 20 no mandaba 20 — mandaba un 400 — y desde que existe el recorte
    # manda 10 igual, después de rasterizar diez páginas para tirarlas. En una
    # VM de 1 GB eso es RAM y CPU regalados.
    oci_analisis_raster_pages: int = field(default_factory=lambda: int(os.getenv("OCI_ANALISIS_RASTER_PAGES", "10")))
    # Procesos por llamada al clasificador con OCI, contra los 40 de Anthropic.
    # Aritmética: cada proceso pide ~350 tokens de JSON de vuelta; 40 × 350 no
    # entra en los 4096 de salida de arriba, 10 sí. Con ~6 procesos/día medidos,
    # casi nunca implica más de una llamada.
    oci_classifier_batch_size: int = field(default_factory=lambda: int(os.getenv("OCI_CLASSIFIER_BATCH_SIZE", "10")))
    # Timeout de una llamada al LLM, EN SEGUNDOS. Hasta la Fase 9 las llamadas
    # al modelo no tenían ninguno, lo que viola la regla del repo: una llamada
    # de red sin timeout puede colgar la corrida diaria para siempre.
    llm_timeout_s: int = field(default_factory=lambda: int(os.getenv("LLM_TIMEOUT_S", "600")))

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

    # ── Canales de alerta fuera de banda (ver src/notifier/alerts.py) ────────
    # Hasta la Fase 6 TODO salía por SMTP, incluido el aviso de que SMTP falló:
    # si el correo se caía, el incidente no llegaba a ningún lado. Estos canales
    # son la segunda vía. Todos opcionales; vacío = canal desactivado.
    alert_telegram_bot_token: str = field(
        default_factory=lambda: os.getenv("ALERT_TELEGRAM_BOT_TOKEN", "")
    )
    alert_telegram_chat_id: str = field(
        default_factory=lambda: os.getenv("ALERT_TELEGRAM_CHAT_ID", "")
    )
    # Webhook genérico estilo Slack/Discord (`{"text": …}`). Ojo: la URL ES la
    # credencial, por eso vive en .env y nunca se escribe a un log.
    alert_webhook_url: str = field(default_factory=lambda: os.getenv("ALERT_WEBHOOK_URL", ""))

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
    # ── Feedback de relevancia (botones Relevante/Irrelevante del correo) ─────
    # Flag propio: en false el correo sale sin botones y nada cambia. Los enlaces
    # se firman con HMAC (FEEDBACK_HMAC_SECRET) y apuntan a FEEDBACK_BASE_URL, el
    # servicio web residente en la VM (`src.web`). Sin URL o sin secreto el
    # feedback se considera apagado: no se emiten enlaces que no se puedan honrar.
    enable_feedback: bool = field(default_factory=lambda: os.getenv("ENABLE_FEEDBACK", "false").lower() == "true")
    feedback_base_url: str = field(default_factory=lambda: os.getenv("FEEDBACK_BASE_URL", "").strip().rstrip("/"))
    feedback_hmac_secret: str = field(default_factory=lambda: os.getenv("FEEDBACK_HMAC_SECRET", "").strip())
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
    def llm_registro_path(self) -> str:
        """Libro append-only del consumo del LLM (JSONL). **NO se resetea.**

        Al revés que `budget_path`, que es un agregado del día y se pisa cada
        24 h, este archivo solo crece: es la serie temporal con la que se va a
        contrastar la factura real de OCI contra los tokens reportados.

        `LLM_REGISTRO_PATH` lo puede mover fuera de `data_dir` —a un volumen que
        sobreviva al `docker run --rm`, por ejemplo— sin tocar nada más.
        """
        return os.getenv("LLM_REGISTRO_PATH", "").strip() or os.path.join(
            self.data_dir, "llm_consumo.jsonl"
        )

    @property
    def screenshots_dir(self) -> str:
        return os.path.join(self.data_dir, "screenshots")

    @property
    def debug_dir(self) -> str:
        return os.path.join(self.data_dir, "debug")

    @property
    def totp_enabled(self) -> bool:
        return bool(self.comprasal_totp_secret)

    @property
    def oci_credenciales_presentes(self) -> bool:
        """¿Hay con qué firmar una llamada a OCI GenAI? Sin revelar nada.

        Existe para poder loguear el estado de la credencial al arrancar:
        `OCI_KEY_CONTENT` es una clave privada PEM y no puede aparecer en un
        log ni truncada, así que lo único publicable es este booleano.

        Mira los dos caminos en el mismo orden de precedencia que usa el
        adaptador: las cuatro variables de la API key (el camino de la VM) o un
        archivo de perfiles en disco (el camino de la laptop). Responde por la
        PRESENCIA, no por la validez: una clave vencida da True acá y falla en
        la llamada, que es donde corresponde enterarse.
        """
        por_env = all(
            (self.oci_user_ocid, self.oci_tenancy_ocid, self.oci_fingerprint, self.oci_key_content)
        )
        ruta = self.oci_config_file or os.path.expanduser("~/.oci/config")
        return bool(por_env or os.path.isfile(ruta))


# Singleton
_config: Config | None = None


def get_config() -> Config:
    global _config
    if _config is None:
        _config = Config()
    return _config
