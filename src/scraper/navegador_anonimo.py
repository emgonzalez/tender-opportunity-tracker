"""Navegador anónimo real (Playwright Chromium) que llama una API JSON desde la página del portal.

Implementa `src.portales.contrato.TransporteJSON`. Lo usa SICOP, cuyo WAF filtra a los clientes que no
son navegadores (`docs/arquitectura/sicop-api.md` §0 y §2; diseño en `docs/arquitectura/portales.md` §2.4).

Reglas:

- **Nunca se fija ni se falsifica el User-Agent** (ni `user_agent=` en el contexto ni `--user-agent=`
  en los args) ni se agregan cabeceras anti-bot: el UA es el del propio Chromium. Si el WAF bloquea al
  Chromium *headless*, la salida es correr *headful* bajo `xvfb` (`SICOP_HEADLESS=false`) o pedir
  autorización a Hacienda. Cambiar el UA sería evadir el filtro del operador.
- Sin login, sin `storage_state` y sin cookies propias: `fetch(..., {credentials: "omit"})`.
- Playwright se importa tarde, dentro de `__aenter__`: importar este módulo no lo carga.
- No se edita `browser.py`: de ahí solo se **importa** `build_chromium_args`.
- Pausa ≥ `pausa_s` (mínimo 0,5 s) antes de cada llamada, medida desde el fin de la anterior.
- Bloqueo del WAF → `BloqueoAcceso` sin reintento; 5xx/red/timeout → 2 reintentos (5 s y 15 s) y luego
  `PortalNoDisponible`.

Sonda para el operador (mide P8 del spike en la VM, sin tocar el UA)::

    python -m src.scraper.navegador_anonimo --sonda            # con SICOP_HEADLESS / HEADLESS
    python -m src.scraper.navegador_anonimo --sonda --headful  # bajo xvfb-run
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import logging
import os
import shutil
import subprocess
import sys
import time
from typing import Any

from src.portales.contrato import BloqueoAcceso, PortalNoDisponible
from src.scraper.sicop import (
    API_BASE,
    MARCA_BLOQUEO,
    ORIGEN_SPA,
    PORTAL_ID,
    RUTA_LISTADO,
    RUTA_TOTALES,
    _Transitorio,
    cuerpo_listado,
    es_bloqueo_waf,
    interpretar_respuesta,
)

logger = logging.getLogger(__name__)

PAUSA_DEFAULT = 1.0
PAUSA_MINIMA = 0.5
ESPERAS_REINTENTO = (5.0, 15.0)
TIMEOUT_FETCH_MS = 60_000
TIMEOUT_GOTO_MS = 60_000


def _reloj() -> float:
    return time.monotonic()


def _pantalla_virtual() -> tuple[Any, str]:
    """Levanta un `Xvfb` y devuelve `(proceso, DISPLAY)`.

    El WAF de SICOP rechaza al Chromium *headless* por su User-Agent («HeadlessChrome»). Con una pantalla
    virtual el navegador corre en su modo normal y con el User-Agent que trae de fábrica: **no se falsifica
    nada**. Falta `Xvfb` ⇒ `PortalNoDisponible` (la imagen lo instala; ver `Dockerfile`).
    """
    exe = shutil.which("Xvfb")
    if not exe:
        raise PortalNoDisponible(PORTAL_ID, "se pidió el modo con ventana (SICOP_HEADLESS=false) pero falta Xvfb",
                                 detalle="instalar xvfb en la imagen")
    for n in range(99, 109):
        if os.path.exists(f"/tmp/.X{n}-lock"):
            continue
        display = f":{n}"
        proc = subprocess.Popen([exe, display, "-screen", "0", "1280x800x24", "-nolisten", "tcp"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(50):  # hasta ~5 s a que exista el socket
            if os.path.exists(f"/tmp/.X11-unix/X{n}"):
                return proc, display
            if proc.poll() is not None:
                break
            time.sleep(0.1)
        proc.terminate()
    raise PortalNoDisponible(PORTAL_ID, "no se pudo levantar Xvfb", detalle="sin pantalla virtual disponible")


async def _dormir(segundos: float) -> None:
    await asyncio.sleep(segundos)

# fetch desde la página: sin credenciales y sin cabeceras más allá del Content-Type del cuerpo.
_JS_FETCH = """
async ([metodo, url, cuerpo, timeoutMs]) => {
  const ctrl = new AbortController();
  const t = setTimeout(() => ctrl.abort(), timeoutMs);
  const init = {method: metodo, credentials: "omit", signal: ctrl.signal};
  if (cuerpo !== null) { init.headers = {"Content-Type": "application/json"}; init.body = cuerpo; }
  try {
    const r = await fetch(url, init);
    return {status: r.status, contentType: r.headers.get("content-type") || "", texto: await r.text()};
  } catch (e) {
    return {status: 0, contentType: "", texto: String(e)};
  } finally { clearTimeout(t); }
}
"""

_JS_BINARIO = """
async ([url, maxBytes, timeoutMs]) => {
  const ctrl = new AbortController();
  const t = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    const r = await fetch(url, {credentials: "omit", signal: ctrl.signal});
    const ct = r.headers.get("content-type") || "";
    const largo = parseInt(r.headers.get("content-length") || "-1", 10);
    if (largo > maxBytes) { ctrl.abort(); return {status: r.status, contentType: ct, grande: true}; }
    if (ct.includes("text/html") || ct.includes("text/plain")) {
      return {status: r.status, contentType: ct, texto: (await r.text()).slice(0, 2000)};
    }
    const buf = new Uint8Array(await r.arrayBuffer());
    if (buf.length > maxBytes) return {status: r.status, contentType: ct, grande: true};
    let bin = "";
    for (let i = 0; i < buf.length; i += 0x8000) bin += String.fromCharCode.apply(null, buf.subarray(i, i + 0x8000));
    return {status: r.status, contentType: ct, b64: btoa(bin)};
  } catch (e) {
    return {status: 0, contentType: "", texto: String(e)};
  } finally { clearTimeout(t); }
}
"""


def _env_bool(nombre: str) -> bool | None:
    valor = os.getenv(nombre)
    if valor is None or not valor.strip():
        return None
    return valor.strip().lower() in ("1", "true", "yes", "si", "sí")


def resolver_headless(config, headless: bool | None = None) -> bool:
    """Argumento → `config.sicop_headless` → `SICOP_HEADLESS` → `config.headless` → True."""
    if headless is not None:
        return bool(headless)
    valor = getattr(config, "sicop_headless", None)
    if valor is not None:
        return bool(valor)
    valor = _env_bool("SICOP_HEADLESS")
    if valor is not None:
        return valor
    return bool(getattr(config, "headless", True))


def resolver_pausa(config, pausa_s: float | None = None) -> float:
    valor = pausa_s if pausa_s is not None else getattr(config, "sicop_pausa_s", None)
    try:
        valor = float(valor) if valor is not None else PAUSA_DEFAULT
    except (TypeError, ValueError):
        valor = PAUSA_DEFAULT
    return max(PAUSA_MINIMA, valor)


def _args_chromium(config) -> list[str]:
    from src.scraper.browser import build_chromium_args  # solo se importa; browser.py no se edita

    args = build_chromium_args(config)
    if any(a.lower().startswith("--user-agent") for a in args):
        raise ValueError("NavegadorAnonimo no admite --user-agent: el UA no se falsifica")
    return args


class NavegadorAnonimo:
    """Chromium real y anónimo con la SPA de `origen` abierta; implementa `TransporteJSON`."""

    def __init__(self, config, origen: str = ORIGEN_SPA, *, headless: bool | None = None,
                 pausa_s: float | None = None) -> None:
        self.config = config
        self.origen = origen
        self.headless = resolver_headless(config, headless)
        self.pausa_s = resolver_pausa(config, pausa_s)
        self.llamadas = 0
        self._ultima: float | None = None
        self._pw = None
        self._browser = None
        self._context = None
        self._page = None
        self._xvfb = None

    # ── ciclo de vida ────────────────────────────────────────────────────────────────────────────
    async def __aenter__(self) -> NavegadorAnonimo:
        from playwright.async_api import async_playwright  # importación tardía

        self._pw = await async_playwright().start()
        try:
            extra: dict[str, Any] = {}
            if not self.headless and not os.environ.get("DISPLAY"):
                # Modo con ventana sin pantalla (servidor): una pantalla virtual propia, no `xvfb-run`.
                self._xvfb, display = _pantalla_virtual()
                extra["env"] = {**os.environ, "DISPLAY": display}
            self._browser = await self._pw.chromium.launch(
                headless=self.headless,
                executable_path=getattr(self.config, "chromium_path", "") or None,
                args=_args_chromium(self.config),
                **extra,
            )
            # Sin storage_state, sin user_agent y sin extra_http_headers: un visitante anónimo.
            self._context = await self._browser.new_context()
            self._page = await self._context.new_page()
            try:
                await self._page.goto(self.origen, wait_until="domcontentloaded", timeout=TIMEOUT_GOTO_MS)
            except Exception as exc:  # noqa: BLE001 — cualquier fallo de red al abrir la SPA es transitorio
                raise PortalNoDisponible(PORTAL_ID, "no se pudo abrir la SPA", detalle=f"{type(exc).__name__}: {exc}") from exc
        except BaseException:
            await self.__aexit__(None, None, None)
            raise
        return self

    async def __aexit__(self, *exc: object) -> None:
        for recurso, metodo in ((self._context, "close"), (self._browser, "close"), (self._pw, "stop")):
            if recurso is None:
                continue
            try:
                await getattr(recurso, metodo)()
            except Exception:  # noqa: BLE001 — cerrar nunca debe tapar el error original
                logger.debug("NavegadorAnonimo: fallo al cerrar", exc_info=True)
        self._context = self._browser = self._pw = self._page = None
        if self._xvfb is not None:
            try:
                self._xvfb.terminate()
            except Exception:  # noqa: BLE001 — cerrar nunca debe tapar el error original
                logger.debug("NavegadorAnonimo: fallo al cerrar Xvfb", exc_info=True)
            self._xvfb = None

    # ── cortesía ─────────────────────────────────────────────────────────────────────────────────
    async def _pausar(self) -> None:
        if self._ultima is not None:
            espera = self.pausa_s - (_reloj() - self._ultima)
            if espera > 0:
                await _dormir(espera)

    async def _evaluar(self, script: str, args: list) -> dict:
        if self._page is None:
            raise RuntimeError("NavegadorAnonimo se usa dentro de `async with`")
        await self._pausar()
        try:
            self.llamadas += 1
            return await self._page.evaluate(script, args)
        finally:
            self._ultima = _reloj()

    # ── TransporteJSON ───────────────────────────────────────────────────────────────────────────
    async def respuesta_cruda(self, metodo: str, url: str, cuerpo: Any | None = None) -> dict:
        """Una sola llamada, sin interpretar: `{status, contentType, texto}`."""
        cuerpo_txt = None if cuerpo is None else json.dumps(cuerpo, ensure_ascii=False)
        return await self._evaluar(_JS_FETCH, [metodo.upper(), url, cuerpo_txt, TIMEOUT_FETCH_MS])

    async def json(self, metodo: str, url: str, cuerpo: Any | None = None) -> Any:
        intentos = len(ESPERAS_REINTENTO) + 1
        for intento in range(intentos):
            r = await self.respuesta_cruda(metodo, url, cuerpo)
            try:
                return interpretar_respuesta(
                    int(r.get("status") or 0), str(r.get("contentType") or ""), str(r.get("texto") or ""), url=url,
                )
            except _Transitorio as exc:
                if intento + 1 >= intentos:
                    raise PortalNoDisponible(PORTAL_ID, "SICOP no responde tras reintentos", detalle=str(exc)) from exc
                espera = ESPERAS_REINTENTO[intento]
                logger.warning("SICOP: %s; reintento %s en %.0f s", exc, intento + 1, espera)
                await _dormir(espera)
        raise AssertionError("inalcanzable")  # pragma: no cover

    async def binario(self, url: str, max_bytes: int) -> bytes | None:
        """Bytes del archivo, None si supera `max_bytes` o no es 200; `BloqueoAcceso` si es el WAF."""
        r = await self._evaluar(_JS_BINARIO, [url, int(max_bytes), TIMEOUT_FETCH_MS])
        status = int(r.get("status") or 0)
        texto = str(r.get("texto") or "")
        if MARCA_BLOQUEO in texto or (status == 200 and "b64" not in r and not r.get("grande")
                                       and es_bloqueo_waf(status, str(r.get("contentType") or ""), texto)):
            raise BloqueoAcceso(PORTAL_ID, "el WAF de SICOP bloqueó la descarga", detalle=f"HTTP {status}: {texto[:200]}")
        if r.get("grande"):
            logger.info("SICOP: archivo mayor a %s bytes, no se baja", max_bytes)
            return None
        if status != 200 or "b64" not in r:
            logger.warning("SICOP: descarga fallida HTTP %s: %s", status, texto[:120])
            return None
        return base64.b64decode(r["b64"])

    async def user_agent(self) -> str:
        """El UA que el navegador manda **de verdad** (solo lectura, para la sonda)."""
        if self._page is None:
            raise RuntimeError("NavegadorAnonimo se usa dentro de `async with`")
        return str(await self._page.evaluate("() => navigator.userAgent"))


# ── Sonda (P8): ¿el Chromium de esta máquina pasa el WAF sin tocar el UA? ───────────────────────────

def _config_para_sonda():
    try:
        from src.config import get_config
        return get_config()
    except Exception:  # noqa: BLE001 — la sonda no necesita credenciales de nadie
        from types import SimpleNamespace
        return SimpleNamespace(
            headless=(_env_bool("HEADLESS") is not False),
            chromium_path=os.getenv("CHROMIUM_PATH", ""),
            low_memory_mode=bool(_env_bool("LOW_MEMORY_MODE")),
        )


async def sonda(config, *, headless: bool | None = None) -> dict:
    """Abre la SPA, lee el UA real y hace 2 llamadas baratas (totales y 1 página de 10). No reintenta."""
    from datetime import datetime, timedelta

    from src.scraper.sicop import ZONA

    resultado: dict[str, Any] = {"headless": resolver_headless(config, headless), "llamadas": []}
    hoy = datetime.now(ZONA).date()
    async with NavegadorAnonimo(config, ORIGEN_SPA, headless=headless) as nav:
        ua = await nav.user_agent()
        resultado["user_agent"] = ua
        resultado["ua_dice_headless"] = "HeadlessChrome" in ua
        pruebas = (
            ("GET", API_BASE + RUTA_TOTALES, None),
            ("POST", API_BASE + RUTA_LISTADO, cuerpo_listado(hoy - timedelta(days=1), hoy, 0, 10)),
        )
        for metodo, url, cuerpo in pruebas:
            r = await nav.respuesta_cruda(metodo, url, cuerpo)
            status, ct, texto = int(r.get("status") or 0), str(r.get("contentType") or ""), str(r.get("texto") or "")
            resultado["llamadas"].append({
                "ruta": url.replace(API_BASE, ""), "status": status, "content_type": ct,
                "bloqueo_waf": es_bloqueo_waf(status, ct, texto), "muestra": texto[:160],
            })
    resultado["pasa_waf"] = all(not c["bloqueo_waf"] and c["status"] == 200 for c in resultado["llamadas"])
    return resultado


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m src.scraper.navegador_anonimo",
        description="Sonda del WAF de SICOP con un Chromium real y anónimo (sin tocar el User-Agent).",
    )
    parser.add_argument("--sonda", action="store_true", help="abre la SPA y hace 2 llamadas baratas")
    modo = parser.add_mutually_exclusive_group()
    modo.add_argument("--headless", dest="headless", action="store_true", default=None, help="forzar headless")
    modo.add_argument("--headful", dest="headless", action="store_false", help="forzar headful (usar con xvfb-run)")
    args = parser.parse_args(argv)
    if not args.sonda:
        parser.print_help()
        return 1
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    try:
        resultado = asyncio.run(sonda(_config_para_sonda(), headless=args.headless))
    except Exception as exc:  # noqa: BLE001 — la sonda informa, no explota
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False))
        return 1
    print(json.dumps(resultado, ensure_ascii=False, indent=2))
    return 0 if resultado["pasa_waf"] else 2


if __name__ == "__main__":
    sys.exit(main())
