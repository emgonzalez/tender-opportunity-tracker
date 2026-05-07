FROM mcr.microsoft.com/playwright/python:v1.50.0-noble

# Set timezone to El Salvador (UTC-6)
ENV TZ=America/El_Salvador
ENV PYTHONUNBUFFERED=1
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

# Create non-root user for security (Chromium should not run as root)
RUN groupadd -r botuser && useradd -r -g botuser -m -d /home/botuser botuser

# Set working directory
WORKDIR /app

# Install Python dependencies first (layer cache optimization)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright browsers as root, then fix permissions
RUN playwright install chromium
RUN chown -R botuser:botuser /root/.cache/ms-playwright 2>/dev/null || true
RUN mkdir -p /home/botuser/.cache && \
    cp -r /root/.cache/ms-playwright /home/botuser/.cache/ 2>/dev/null || true && \
    chown -R botuser:botuser /home/botuser/.cache

# Copy application source
COPY src/ ./src/
COPY templates/ ./templates/

# Create data directories with proper ownership
RUN mkdir -p data/screenshots data/debug && \
    chown -R botuser:botuser /app

# Switch to non-root user
USER botuser

# Default command: run the scheduler (main entry point)
CMD ["python", "-m", "src.main"]
