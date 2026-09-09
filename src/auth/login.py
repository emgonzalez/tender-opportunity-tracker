"""
Login en COMPRASAL con Playwright API SÍNCRONA.

════════════════════════════════════════════════════════════════
COMPORTAMIENTO ACTUAL DEL FLUJO DE LOGIN — NO MODIFICAR SIN LEER
════════════════════════════════════════════════════════════════

El flujo completo es:

  1. Se lee el OTP de stdin (o se genera con TOTP) ANTES de lanzar el browser.
     Motivo: sync_playwright corre un event loop asyncio en background. Si
     input() bloquea el thread principal mientras el browser está activo, el
     driver Node.js de Playwright mata el proceso con os._exit(). Leyendo el
     OTP antes de entrar al contexto de Playwright el problema desaparece.

  2. Se navega a COMPRASAL_LOGIN_URL, se llenan email y password y se hace
     click en "INICIAR SESIÓN".

  3. Aparece la pantalla de OTP con 6 cajas individuales (selectores
     otp-field-box--0 … otp-field-box--5). Se llena cada caja con page.fill().

  4. COMPRASAL hace AUTO-SUBMIT ~200-400 ms después de que se llena la última
     caja. El botón CONFIRMAR entra en estado `v-btn--loading` y un overlay
     (<div class="d-flex justify-space-betweeen...">) intercepta todos los
     eventos de puntero.

     ──► POR ESTO no se puede hacer page.click() directamente:
         - Sin force=True → timeout 30 s porque el overlay bloquea el click.
         - Con force=True → el click sintético se despacha antes de que
           Vue.js haya procesado los valores de las cajas (161 ms < 200 ms de
           auto-submit) y el formulario no se envía.

  5. La solución correcta:
       a. Esperar 600 ms después de llenar la última caja (margen sobre los
          ~200-400 ms del auto-submit).
       b. Evaluar via JS si existe un botón con clase `v-btn--loading`.
          - True  → auto-submit activo, NO hacer click adicional.
          - False → el auto-submit no se activó, hacer page.click() normal.
       c. Esperar con wait_for_function("!location.href.includes('login')")
          en lugar de wait_for_load_state("networkidle").

     ──► POR ESTO no se usa wait_for_load_state("networkidle"):
         La SPA de Vue.js procesa el OTP sin requests de red visibles;
         networkidle se dispara en ~175 ms mientras la página aún está en
         /login. wait_for_function hace polling de window.location.href y
         detecta la navegación real (hash routing: /#/login → /#/procesos).

  6. Tras la navegación se guarda el storage_state (cookies + localStorage)
     en data/storage_state.json.

════════════════════════════════════════════════════════════════

La API síncrona se usa aquí intencionalmente: este script es interactivo
(requiere input() del operador para el OTP), y mezclar input() bloqueante
con el event loop de asyncio causa que Playwright pierda la conexión con
Chromium mientras espera. La API síncrona elimina ese problema.

El pipeline principal (extractor, session) usa async Playwright — correcto
para operaciones paralelas y scheduling. Este script solo se invoca una vez
manualmente para obtener la sesión inicial.

Uso:
    python -m src.auth.login --manual
    python -m src.auth.login --inspect
    python -m src.auth.login --inspect-otp
"""
import atexit
import logging
import os
import signal
import sys
import time
import traceback

from playwright.sync_api import Page, sync_playwright
from playwright.sync_api import TimeoutError as PlaywrightTimeout

from src.auth.totp import generate_totp
from src.config import get_config
from src.scraper.browser import build_chromium_args

logger = logging.getLogger(__name__)

# ── Debug log file (escribe al disco directamente, sobrevive al TTY) ─────────

_debug_log = None

def _dlog(msg: str) -> None:
    """Escribe a /app/data/login_debug.log con fsync. Nunca se pierde."""
    global _debug_log
    try:
        if _debug_log is None:
            _debug_log = open("/app/data/login_debug.log", "a", buffering=1)
        import datetime
        line = f"[{datetime.datetime.now().isoformat()}] {msg}\n"
        _debug_log.write(line)
        _debug_log.flush()
        os.fsync(_debug_log.fileno())
        # También escribir directo al fd de stderr (bypassa cualquier buffer Python)
        os.write(2, line.encode())
    except Exception as e:
        try:
            os.write(2, f"[DLOG ERROR] {e}\n".encode())
        except Exception:
            pass

def _dlog_exit(reason: str) -> None:
    _dlog(f"=== PROCESO TERMINANDO: {reason} ===")
    if _debug_log:
        try:
            _debug_log.flush()
            os.fsync(_debug_log.fileno())
        except Exception:
            pass

# Capturar señales y salida para el log
atexit.register(lambda: _dlog_exit("atexit"))
signal.signal(signal.SIGTERM, lambda s, f: (_dlog_exit("SIGTERM"), sys.exit(1)))
# SIGPIPE se deja en SIG_IGN (default de CPython): un broken pipe se convierte en
# BrokenPipeError catcheable. SIG_DFL mataría el proceso silenciosamente.

COMPRASAL_LOGIN_URL = "https://www.comprasal.gob.sv/#/login"

SEL_EMAIL   = 'input[name="login"]'
SEL_PASSWORD = 'input[name="password"], input#password'
SEL_SUBMIT  = 'button[type="submit"]'
# OTP: 6 cajas separadas, clase otp-field-box--N
SEL_OTP_BOX    = '[class*="otp-field-box--{i}"]'
SEL_OTP_SUBMIT = 'button:has-text("CONFIRMAR")'


# ── Helpers ──────────────────────────────────────────────────────────────────

def _dump_page_debug(page: Page, debug_dir: str, screenshots_dir: str) -> None:
    """Guarda HTML y screenshot; lista todos los inputs/buttons del DOM actual."""
    import datetime
    os.makedirs(debug_dir, exist_ok=True)
    os.makedirs(screenshots_dir, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")

    html_path = os.path.join(debug_dir, f"login_page_{ts}.html")
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(page.content())
    logger.info("HTML del login guardado en: %s", html_path)

    shot_path = os.path.join(screenshots_dir, f"login_page_{ts}.png")
    page.screenshot(path=shot_path, full_page=True, timeout=60_000)
    logger.info("Screenshot guardado en: %s", shot_path)

    inputs = page.evaluate("""
        () => Array.from(document.querySelectorAll('input, button[type="submit"], button[type="button"]')).map(el => ({
            tag: el.tagName,
            type: el.type || '',
            name: el.name || '',
            id: el.id || '',
            placeholder: el.placeholder || '',
            class: el.className || '',
            text: el.innerText?.slice(0, 50) || '',
        }))
    """)
    print("\n" + "=" * 60)
    print("ELEMENTOS INTERACTIVOS ENCONTRADOS EN LA PÁGINA:")
    print("=" * 60)
    for el in inputs:
        print(f"  <{el['tag'].lower()} type='{el['type']}' name='{el['name']}' "
              f"id='{el['id']}' placeholder='{el['placeholder']}' "
              f"class='{el['class'][:40]}'>  {el['text']}")
    print("=" * 60)
    print(f"\nHTML:       {html_path}")
    print(f"Screenshot: {shot_path}", flush=True)


def _fill_credentials(page: Page, email: str, password: str,
                      debug_dir: str, screenshots_dir: str) -> None:
    logger.info("Navegando al login de COMPRASAL...")
    page.goto(COMPRASAL_LOGIN_URL, wait_until="domcontentloaded", timeout=90_000)
    _dump_page_debug(page, debug_dir, screenshots_dir)

    logger.info("Ingresando credenciales...")
    page.wait_for_selector(SEL_EMAIL, timeout=15_000)
    page.fill(SEL_EMAIL, email)
    page.fill(SEL_PASSWORD, password)
    page.click(SEL_SUBMIT)
    logger.info("Submit enviado. Esperando respuesta de COMPRASAL...")


def _handle_otp(page: Page, otp_code: str, screenshots_dir: str) -> None:
    """
    Completa la pantalla de OTP con el código ya leído.

    Ver el docstring del módulo (puntos 3-5) para la explicación completa
    del flujo y los motivos de cada decisión de implementación.

    Resumen del comportamiento validado:
    - COMPRASAL auto-submite ~200-400 ms después de llenar la caja 5.
    - Esperamos 600 ms, detectamos v-btn--loading y saltamos el click si ya
      está en curso el auto-submit.
    - Usamos wait_for_function (URL polling) en lugar de networkidle porque
      la SPA resuelve networkidle antes de navegar.
    """
    _dlog("_handle_otp: inicio")
    first_box_sel = SEL_OTP_BOX.format(i=0)
    _dlog(f"_handle_otp: esperando selector '{first_box_sel}'")
    try:
        page.wait_for_selector(first_box_sel, timeout=10_000)
        _dlog("_handle_otp: selector OTP encontrado")
    except PlaywrightTimeout:
        _dlog("_handle_otp: TIMEOUT — pantalla OTP no detectada")
        logger.info("No se detectó pantalla de OTP — continuando.")
        return

    logger.info("Pantalla de OTP detectada. Rellenando %d dígitos...", len(otp_code))
    _dlog(f"_handle_otp: rellenando con otp_code={repr(otp_code)}")

    for i, digit in enumerate(otp_code):
        _dlog(f"_handle_otp: fill box {i}")
        page.fill(SEL_OTP_BOX.format(i=i), digit)
        _dlog(f"_handle_otp: box {i} OK")

    os.makedirs(screenshots_dir, exist_ok=True)
    page.screenshot(path=os.path.join(screenshots_dir, "otp_before_submit.png"), full_page=True)

    # COMPRASAL hace auto-submit al completar los 6 dígitos (~200ms después de
    # llenar la última caja). Esperar ese tiempo antes de evaluar el estado del botón.
    page.wait_for_timeout(600)

    try:
        btn_loading = page.evaluate("() => !!document.querySelector('button.v-btn--loading')")
        _dlog(f"_handle_otp: btn_loading={btn_loading}")
    except Exception:
        # La página ya navegó (auto-submit completado)
        btn_loading = True
        _dlog("_handle_otp: evaluate falló — página ya en navegación")

    if btn_loading:
        _dlog("_handle_otp: auto-submit activo")
    else:
        _dlog("_handle_otp: auto-submit no detectado — haciendo click en CONFIRMAR")
        try:
            page.click(SEL_OTP_SUBMIT, timeout=5_000)
            _dlog("_handle_otp: click ejecutado")
        except PlaywrightTimeout:
            _dlog("_handle_otp: timeout en click — continuando")

    # La SPA de Vue.js procesa el OTP sin requests de red visibles, por lo que
    # `networkidle` se dispara prematuramente. Esperar hasta que la URL cambie
    # (navegación real) en lugar de esperar solo el estado de red.
    _dlog("_handle_otp: esperando salida de /login")
    try:
        page.wait_for_function(
            "() => !window.location.href.includes('login')",
            timeout=20_000,
        )
        _dlog(f"_handle_otp: navegación detectada. URL={page.url}")
    except PlaywrightTimeout:
        _dlog("_handle_otp: timeout — OTP incorrecto o expirado")

    page.screenshot(path=os.path.join(screenshots_dir, "otp_after_submit.png"), full_page=True)
    _dlog(f"_handle_otp: fin. URL={page.url}")


# ── Login principal ───────────────────────────────────────────────────────────

def perform_login(headless: bool = True) -> bool:
    """Login completo en COMPRASAL. Guarda storage_state.json si tiene éxito."""
    config = get_config()

    # Leer OTP ANTES de lanzar el browser.
    # sync_playwright corre un event loop asyncio en background; si input() bloquea
    # el thread principal mientras el browser está activo, el driver Node.js de
    # Playwright mata el proceso con os._exit() (incapturable). Leyendo el OTP aquí,
    # fuera del contexto de Playwright, el problema desaparece.
    if config.totp_enabled:
        otp_code = generate_totp(config.comprasal_totp_secret)
        _dlog("perform_login: TOTP automático generado")
    else:
        # Leer OTP por stdin (pipe o TTY) ANTES de lanzar el browser.
        # Con `docker compose run -T` stdin es un pipe sin TTY — readline() funciona.
        # Con TTY interactivo el Docker Desktop de macOS cierra el stdout al presionar
        # Enter, matando el proceso con SIGPIPE antes de cualquier write posterior.
        if sys.stdin.isatty():
            # Si hay TTY real (ej: docker exec, terminal local) mostrar prompt interactivo
            print("\n" + "=" * 60)
            print("COMPRASAL requiere autenticación de dos factores.")
            print("Ingresa el código OTP de tu app de autenticación:")
            print("=" * 60, flush=True)
        _dlog("perform_login: leyendo OTP de stdin...")
        otp_code = sys.stdin.readline().strip()
        _dlog(f"perform_login: OTP leído: len={len(otp_code)} valor={repr(otp_code)}")

    _dlog("perform_login: iniciando sync_playwright context manager...")
    with sync_playwright() as p:
        _dlog("perform_login: sync_playwright OK — lanzando chromium")
        browser = p.chromium.launch(
            headless=headless,
            executable_path=config.chromium_path or None,
            args=build_chromium_args(config),
        )
        _dlog("perform_login: chromium lanzado — creando browser context")
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            ),
        )
        _dlog("perform_login: browser context creado — creando page")
        page = context.new_page()
        _dlog("perform_login: page creada — OK")
        os.makedirs(config.screenshots_dir, exist_ok=True)

        try:
            _dlog("perform_login: llamando _fill_credentials")
            _fill_credentials(
                page, config.comprasal_email, config.comprasal_password,
                config.debug_dir, config.screenshots_dir,
            )
            _dlog("perform_login: _fill_credentials OK — llamando _handle_otp")
            _handle_otp(page, otp_code, config.screenshots_dir)
            _dlog("perform_login: _handle_otp OK — esperando networkidle final")

            page.wait_for_load_state("networkidle", timeout=20_000)
            current_url = page.url
            _dlog(f"perform_login: URL final = {current_url}")
            logger.info("URL final tras login: %s", current_url)

            if "/login" in current_url:
                shot = os.path.join(config.screenshots_dir, "login_failed.png")
                page.screenshot(path=shot)
                _dlog("perform_login: LOGIN FALLIDO — URL contiene /login")
                logger.error("Login fallido — todavía en /login. Screenshot: %s", shot)
                return False

            # Guardar sesión
            os.makedirs(os.path.dirname(config.storage_state_path), exist_ok=True)
            context.storage_state(path=config.storage_state_path)
            _dlog(f"perform_login: SESION GUARDADA en {config.storage_state_path}")
            logger.info("Sesión guardada en %s", config.storage_state_path)
            print(f"\nSesión guardada exitosamente en: {config.storage_state_path}", flush=True)
            return True

        except BaseException:
            tb = traceback.format_exc()
            _dlog(f"perform_login: EXCEPCION CAPTURADA:\n{tb}")
            print(f"\n[ERROR LOGIN]\n{tb}", flush=True)
            try:
                shot = os.path.join(config.screenshots_dir, "login_error.png")
                page.screenshot(path=shot)
                _dlog(f"perform_login: screenshot de error guardado en {shot}")
            except Exception as se:
                _dlog(f"perform_login: no se pudo tomar screenshot: {se}")
            return False
        finally:
            _dlog("perform_login: bloque finally — cerrando browser")
            browser.close()


# ── Modos de inspección (debug) ───────────────────────────────────────────────

def _inspect_login_page() -> None:
    """Vuelca HTML + screenshot de la página de login sin autenticarse."""
    config = get_config()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, executable_path=config.chromium_path or None, args=build_chromium_args(config))
        context = browser.new_context(user_agent=(
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ))
        page = context.new_page()
        logger.info("Navegando a %s ...", COMPRASAL_LOGIN_URL)
        page.goto(COMPRASAL_LOGIN_URL, wait_until="networkidle", timeout=30_000)
        _dump_page_debug(page, config.debug_dir, config.screenshots_dir)
        browser.close()


def _inspect_otp_page() -> None:
    """Llena credenciales, hace click en submit y vuelca el DOM de la pantalla de OTP."""
    config = get_config()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, executable_path=config.chromium_path or None, args=build_chromium_args(config))
        context = browser.new_context(user_agent=(
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ))
        page = context.new_page()
        try:
            logger.info("Navegando al login...")
            page.goto(COMPRASAL_LOGIN_URL, wait_until="networkidle", timeout=30_000)
            page.wait_for_selector(SEL_EMAIL, timeout=15_000)
            page.fill(SEL_EMAIL, config.comprasal_email)
            page.fill(SEL_PASSWORD, config.comprasal_password)
            logger.info("Credenciales ingresadas. Haciendo click en submit...")
            page.click(SEL_SUBMIT)
            time.sleep(5)
            print("\n" + "=" * 60)
            print(f"URL tras submit: {page.url}")
            print("=" * 60, flush=True)
            _dump_page_debug(page, config.debug_dir, config.screenshots_dir)
        finally:
            browser.close()


# ── CLI entry point ───────────────────────────────────────────────────────────

def main() -> None:
    import argparse

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    parser = argparse.ArgumentParser(description="Login manual en COMPRASAL")
    parser.add_argument("--manual",      action="store_true", help="Login interactivo (OTP por stdin)")
    parser.add_argument("--visible",     action="store_true", help="Muestra el navegador (headless=false)")
    parser.add_argument("--inspect",     action="store_true", help="Vuelca DOM de la página de login")
    parser.add_argument("--inspect-otp", action="store_true", help="Llena credenciales y vuelca DOM del OTP")
    args = parser.parse_args()

    if not any([args.manual, args.inspect, args.inspect_otp]):
        parser.print_help()
        sys.exit(1)

    if args.inspect:
        _inspect_login_page()
        sys.exit(0)

    if args.inspect_otp:
        _inspect_otp_page()
        sys.exit(0)

    _dlog("main: iniciando perform_login")
    try:
        success = perform_login(headless=not args.visible)
    except BaseException:
        _dlog(f"main: EXCEPCION NO CAPTURADA:\n{traceback.format_exc()}")
        raise
    _dlog(f"main: perform_login retornó success={success}")
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
