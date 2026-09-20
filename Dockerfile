FROM mcr.microsoft.com/playwright/python:v1.50.0-noble

# Set timezone to El Salvador (UTC-6)
ENV TZ=America/El_Salvador
ENV PYTHONUNBUFFERED=1
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

# Create non-root user for security (Chromium should not run as root)
RUN groupadd -r botuser && useradd -r -g botuser -m -d /home/botuser botuser

# Set working directory
WORKDIR /app

# Install Python dependencies first (layer cache optimization).
# Solo se copia pyproject.toml, sin src/: con `packages.find` sin coincidencias
# setuptools construye una distribución vacía, y el efecto de `pip install .` es
# instalar exactamente las dependencias declaradas. Así la capa cara sigue
# invalidándose solo cuando cambian las dependencias, no en cada cambio de código.
COPY pyproject.toml .
# Apagado por default: la imagen que va a producción NO lleva el SDK de OCI.
# Los dos costos, medidos: `import oci` suma +39-42 MB de RSS, y el SDK engorda
# esta imagen **+405 MB** (2543 → 2948 MB) porque instala los clientes generados
# de todos los servicios de OCI para usar un solo endpoint de inferencia, más
# cryptography, pyOpenSSL y aiohttp. Hoy los cuatro tenants corren con
# LLM_PROVIDER=anthropic, así que nadie tiene por qué cargar con eso: se
# construye con --build-arg INSTALL_OCI=1 solo para el tenant de dev, mientras
# se valida el proveedor alternativo.
#
# El ARG va ACÁ, junto a la capa que modifica, y no abajo con GIT_SHA: participa
# de la clave de caché de este RUN, así que las dos variantes conviven en la
# caché sin pisarse, y cambiarlo no invalida nada aguas arriba.
ARG INSTALL_OCI=0
RUN if [ "$INSTALL_OCI" = "1" ]; then pip install --no-cache-dir ".[oci]"; \
    else pip install --no-cache-dir . ; fi

# Install Playwright browsers as root, then fix permissions
RUN playwright install chromium
RUN chown -R botuser:botuser /root/.cache/ms-playwright 2>/dev/null || true
RUN mkdir -p /home/botuser/.cache && \
    cp -r /root/.cache/ms-playwright /home/botuser/.cache/ 2>/dev/null || true && \
    chown -R botuser:botuser /home/botuser/.cache

# SHA del commit que se está construyendo (lo pasa CI con --build-arg).
# Va AQUÍ, después de las capas de dependencias y de Chromium: cambia en cada
# commit, así que ponerlo antes invalidaría la caché de todo lo caro.
ARG GIT_SHA=dev
ENV APP_VERSION=$GIT_SHA

# Copy application source
COPY src/ ./src/
COPY templates/ ./templates/

# Create data directories with proper ownership (chmod 777 allows any user to write, needed for volume mounts)
RUN mkdir -p data/screenshots data/debug && \
    chmod -R 777 data && \
    chown -R botuser:botuser /app

# Switch to non-root user
USER botuser

# Default command: run the scheduler (main entry point)
CMD ["python", "-m", "src.main"]
