"""Gestión de sesión de Playwright: guardar, cargar y validar storage_state.json."""
import logging
import os
from datetime import datetime

from playwright.async_api import BrowserContext, async_playwright

from src.config import Config
from src.scraper.browser import build_chromium_args

logger = logging.getLogger(__name__)

COMPRASAL_BASE = "https://www.comprasal.gob.sv"
COMPRASAL_PROCESOS_URL = f"{COMPRASAL_BASE}/#/procesos-de-compra"
LOGIN_INDICATOR = "/login"


async def save_session(context: BrowserContext, storage_state_path: str) -> None:
    """Guarda las cookies y localStorage del contexto actual en storage_state.json."""
    os.makedirs(os.path.dirname(storage_state_path), exist_ok=True)
    await context.storage_state(path=storage_state_path)
    logger.info("Sesión guardada en %s", storage_state_path)


async def is_session_valid(config: Config) -> bool:
    """
    Verifica si la sesión guardada sigue activa.
    Carga el storage_state, navega a una URL protegida y comprueba
    que no hay redirección al login.
    """
    if not os.path.exists(config.storage_state_path):
        logger.info("No existe storage_state.json — sesión no disponible.")
        return False

    # Verificar antigüedad del archivo (heurística: si tiene más de 12h, forzar revalidación)
    age_hours = (datetime.now().timestamp() - os.path.getmtime(config.storage_state_path)) / 3600
    if age_hours > 12:
        logger.info("storage_state.json tiene %.1f horas — revalidando con el servidor.", age_hours)

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                executable_path=config.chromium_path or None,
                args=build_chromium_args(config),
            )
            context = await browser.new_context(
                storage_state=config.storage_state_path,
                user_agent=(
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
                ),
            )
            page = await context.new_page()

            # 'domcontentloaded' basta para evaluar la URL final post-redirect; el
            # 'networkidle' se cuelga por polling de la SPA y mata la validación.
            await page.goto(COMPRASAL_PROCESOS_URL, wait_until="domcontentloaded", timeout=90_000)

            # COMPRASAL es una SPA (Vue.js). El guard de autenticación evalúa la ruta
            # DESPUÉS de que networkidle dispara (~175ms). Esperamos hasta que la URL
            # se estabilice en la página de procesos O en login — lo que llegue primero.
            try:
                await page.wait_for_function(
                    "() => window.location.href.includes('/login') "
                    "|| window.location.href.includes('/procesos')",
                    timeout=5_000,
                )
            except Exception:
                pass  # timeout sin redirect = URL ya es la correcta

            current_url = page.url

            await browser.close()

        if LOGIN_INDICATOR in current_url:
            logger.warning("Sesión inválida: redirigido a login (%s).", current_url)
            return False

        logger.info("Sesión válida (URL actual: %s).", current_url)
        return True

    except Exception as exc:
        logger.error("Error al validar sesión: %s", exc)
        return False
