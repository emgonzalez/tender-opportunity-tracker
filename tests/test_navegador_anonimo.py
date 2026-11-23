"""Tests de src.scraper.navegador_anonimo — sin red ni Chromium: Playwright falso en `sys.modules`.

Lo que se fija: el contexto nace **sin** `user_agent`, `storage_state` ni `extra_http_headers`; los
args no traen `--user-agent`; el `fetch` va con `credentials: "omit"`; un bloqueo del WAF NO se
reintenta; un 5xx se reintenta 2 veces y luego es `PortalNoDisponible`; la pausa entre llamadas.
"""
from __future__ import annotations

import ast
import asyncio
import base64
import json
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.portales.contrato import BloqueoAcceso, ContratoRoto, PortalNoDisponible, TransporteJSON
from src.scraper import navegador_anonimo as nav
from src.scraper import sicop

FIX = Path(__file__).parent / "fixtures" / "sicop"
WAF = (FIX / "prod-api_bloqueo_waf.html").read_text(encoding="utf-8")
VACIO = (FIX / "epCartel_searchEpCartels_vacio.json").read_text(encoding="utf-8")


def _json(texto=VACIO, status=200):
    return {"status": status, "contentType": "application/json", "texto": texto}


class PaginaFalsa:
    def __init__(self, respuestas):
        self.respuestas = list(respuestas)
        self.evaluados: list[tuple[str, object]] = []
        self.goto_args = None

    async def goto(self, url, **kw):
        self.goto_args = (url, kw)

    async def evaluate(self, script, args=None):
        self.evaluados.append((script, args))
        if "navigator.userAgent" in script:
            return "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) HeadlessChrome/130.0 Safari/537.36"
        return self.respuestas.pop(0)


class PlaywrightFalso:
    """Registra lo que el navegador pide a Playwright."""

    def __init__(self, respuestas):
        self.pagina = PaginaFalsa(respuestas)
        self.launch_kwargs = None
        self.context_args = None
        self.cerrado = []
        outer = self

        class Contexto:
            async def new_page(self):
                return outer.pagina

            async def close(self):
                outer.cerrado.append("context")

        class Browser:
            async def new_context(self, *a, **kw):
                outer.context_args = (a, kw)
                return Contexto()

            async def close(self):
                outer.cerrado.append("browser")

        class Chromium:
            async def launch(self, **kw):
                outer.launch_kwargs = kw
                return Browser()

        class PW:
            chromium = Chromium()

            async def stop(self):
                outer.cerrado.append("pw")

        class Manager:
            async def start(self):
                return PW()

        self.manager = Manager

    def instalar(self, monkeypatch):
        modulo = types.ModuleType("playwright.async_api")
        modulo.async_playwright = lambda: self.manager()
        monkeypatch.setitem(sys.modules, "playwright.async_api", modulo)


@pytest.fixture
def sin_espera(monkeypatch):
    esperas: list[float] = []

    async def dormir(s):
        esperas.append(s)

    monkeypatch.setattr(nav, "_dormir", dormir)
    return esperas


def _cfg(**extra):
    return SimpleNamespace(headless=True, chromium_path="", low_memory_mode=False, **extra)


def _correr(respuestas, accion, monkeypatch, **kw):
    pw = PlaywrightFalso(respuestas)
    pw.instalar(monkeypatch)

    async def flujo():
        async with nav.NavegadorAnonimo(_cfg(**kw.pop("cfg", {})), sicop.ORIGEN_SPA, **kw) as n:
            return await accion(n)

    return pw, asyncio.run(flujo())


class TestSinUserAgentFalso:
    def test_contexto_sin_ua_ni_estado_ni_cabeceras(self, monkeypatch, sin_espera):
        pw, _ = _correr([], lambda n: asyncio.sleep(0), monkeypatch)
        args, kwargs = pw.context_args
        assert args == () and kwargs == {}
        assert "user_agent" not in kwargs and "storage_state" not in kwargs and "extra_http_headers" not in kwargs

    def test_launch_sin_user_agent_y_con_args_de_browser(self, monkeypatch, sin_espera):
        from src.scraper.browser import build_chromium_args

        pw, _ = _correr([], lambda n: asyncio.sleep(0), monkeypatch)
        kw = pw.launch_kwargs
        assert not any("user-agent" in a.lower() for a in kw["args"])
        assert kw["args"] == build_chromium_args(_cfg())
        assert kw["headless"] is True and kw["executable_path"] is None
        assert "user_agent" not in kw

    def test_abre_la_spa_y_cierra_todo(self, monkeypatch, sin_espera):
        pw, _ = _correr([], lambda n: asyncio.sleep(0), monkeypatch)
        assert pw.pagina.goto_args[0] == sicop.ORIGEN_SPA
        assert pw.pagina.goto_args[1]["wait_until"] == "domcontentloaded"
        assert pw.cerrado == ["context", "browser", "pw"]

    def test_args_con_user_agent_se_rechazan(self, monkeypatch):
        import src.scraper.browser as browser

        monkeypatch.setattr(browser, "build_chromium_args", lambda c: ["--user-agent=Chrome"])
        with pytest.raises(ValueError):
            nav._args_chromium(_cfg())

    def test_fuente_sin_user_agent_ni_cabeceras_anti_bot(self):
        arbol = ast.parse(Path(nav.__file__).read_text(encoding="utf-8"))
        prohibidos = {"user_agent", "extra_http_headers", "storage_state"}
        kwargs = {k.arg for n in ast.walk(arbol) if isinstance(n, ast.Call) for k in n.keywords}
        assert not kwargs & prohibidos
        atributos = {n.attr for n in ast.walk(arbol) if isinstance(n, ast.Attribute)}
        assert not atributos & {"set_extra_http_headers", "add_init_script", "add_cookies", "route"}
        docstrings = {
            id(n.body[0].value) for n in ast.walk(arbol)
            if isinstance(n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            and n.body and isinstance(n.body[0], ast.Expr) and isinstance(n.body[0].value, ast.Constant)
        }
        cadenas = [n.value for n in ast.walk(arbol)
                   if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docstrings]
        assert not any(c.strip().lower() == "user-agent" or c.lower().startswith("--user-agent=") for c in cadenas)
        assert not any('"user-agent"' in c.lower() for c in cadenas)  # ni como cabecera dentro del JS
        assert any('credentials: "omit"' in c for c in cadenas)

    def test_playwright_se_importa_tarde(self):
        arbol = ast.parse(Path(nav.__file__).read_text(encoding="utf-8"))
        de_modulo = [n for n in arbol.body if isinstance(n, (ast.Import, ast.ImportFrom))]
        assert not any((getattr(n, "module", "") or "").startswith("playwright") for n in de_modulo)

    def test_cumple_transporte_json(self):
        assert isinstance(nav.NavegadorAnonimo(_cfg(), sicop.ORIGEN_SPA), TransporteJSON)


class TestRespuestas:
    def test_json_ok_y_fetch_sin_credenciales(self, monkeypatch, sin_espera):
        pw, datos = _correr([_json()], lambda n: n.json("POST", sicop.API_BASE + sicop.RUTA_LISTADO, {"a": 1}), monkeypatch)
        assert datos["content"] == []
        script, args = pw.pagina.evaluados[0]
        assert 'credentials: "omit"' in script
        assert args[0] == "POST" and json.loads(args[2]) == {"a": 1}

    def test_get_sin_cuerpo(self, monkeypatch, sin_espera):
        pw, _ = _correr([_json("{}")], lambda n: n.json("get", sicop.API_BASE + sicop.RUTA_TOTALES), monkeypatch)
        assert pw.pagina.evaluados[0][1][0] == "GET" and pw.pagina.evaluados[0][1][2] is None

    def test_bloqueo_waf_sin_reintento(self, monkeypatch, sin_espera):
        respuestas = [{"status": 200, "contentType": "text/html", "texto": WAF}, _json()]
        pw = PlaywrightFalso(respuestas)
        pw.instalar(monkeypatch)

        async def flujo():
            async with nav.NavegadorAnonimo(_cfg(), sicop.ORIGEN_SPA) as n:
                await n.json("POST", sicop.API_BASE + sicop.RUTA_LISTADO, {})

        with pytest.raises(BloqueoAcceso):
            asyncio.run(flujo())
        assert len(pw.pagina.evaluados) == 1  # contador de llamadas = 1: no se reintenta un bloqueo
        assert sin_espera == []

    def test_422_es_contrato_roto_sin_reintento(self, monkeypatch, sin_espera):
        texto = (FIX / "epCartel_searchEpCartels_422_rango_fechas.json").read_text(encoding="utf-8")
        pw = PlaywrightFalso([_json(texto, 422)])
        pw.instalar(monkeypatch)

        async def flujo():
            async with nav.NavegadorAnonimo(_cfg(), sicop.ORIGEN_SPA) as n:
                await n.json("POST", sicop.API_BASE + sicop.RUTA_LISTADO, {})

        with pytest.raises(ContratoRoto) as e:
            asyncio.run(flujo())
        assert "rango máximo" in e.value.detalle
        assert len(pw.pagina.evaluados) == 1

    def test_5xx_dos_reintentos_y_portal_no_disponible(self, monkeypatch, sin_espera):
        caida = {"status": 503, "contentType": "text/html", "texto": "<html>503</html>"}
        pw = PlaywrightFalso([caida, caida, caida])
        pw.instalar(monkeypatch)

        async def flujo():
            async with nav.NavegadorAnonimo(_cfg(), sicop.ORIGEN_SPA) as n:
                await n.json("POST", sicop.API_BASE + sicop.RUTA_LISTADO, {})

        with pytest.raises(PortalNoDisponible):
            asyncio.run(flujo())
        assert len(pw.pagina.evaluados) == 3
        assert [e for e in sin_espera if e >= 5] == [5.0, 15.0]

    def test_error_de_red_se_recupera_al_reintentar(self, monkeypatch, sin_espera):
        red = {"status": 0, "contentType": "", "texto": "TypeError: Failed to fetch"}
        pw, datos = _correr([red, _json()], lambda n: n.json("GET", sicop.API_BASE + sicop.RUTA_TOTALES), monkeypatch)
        assert datos["totalElements"] == 0 and len(pw.pagina.evaluados) == 2


class TestPausa:
    def test_pausa_entre_llamadas(self, monkeypatch, sin_espera):
        reloj = iter([100.0, 100.1, 100.2, 100.3])
        monkeypatch.setattr(nav, "_reloj", lambda: next(reloj))

        async def dos(n):
            await n.json("GET", sicop.API_BASE + sicop.RUTA_TOTALES)
            await n.json("GET", sicop.API_BASE + sicop.RUTA_TOTALES)

        _correr([_json(), _json()], dos, monkeypatch, pausa_s=2.0)
        assert len(sin_espera) == 1 and sin_espera[0] == pytest.approx(1.9)

    def test_sin_pausa_antes_de_la_primera(self, monkeypatch, sin_espera):
        _correr([_json()], lambda n: n.json("GET", sicop.API_BASE + sicop.RUTA_TOTALES), monkeypatch)
        assert sin_espera == []

    @pytest.mark.parametrize("pedida,efectiva", [(None, 1.0), (0.1, 0.5), (0.5, 0.5), (3, 3.0)])
    def test_pausa_minima(self, pedida, efectiva):
        assert nav.NavegadorAnonimo(_cfg(), sicop.ORIGEN_SPA, pausa_s=pedida).pausa_s == efectiva

    def test_pausa_desde_config(self):
        assert nav.NavegadorAnonimo(_cfg(sicop_pausa_s=2.5), sicop.ORIGEN_SPA).pausa_s == 2.5


class TestHeadless:
    def test_argumento_manda(self, monkeypatch):
        monkeypatch.setenv("SICOP_HEADLESS", "true")
        assert nav.resolver_headless(_cfg(sicop_headless=True), False) is False

    def test_config_sicop_headless(self, monkeypatch):
        monkeypatch.delenv("SICOP_HEADLESS", raising=False)
        assert nav.resolver_headless(_cfg(sicop_headless=False)) is False

    def test_variable_de_entorno(self, monkeypatch):
        monkeypatch.setenv("SICOP_HEADLESS", "false")
        assert nav.resolver_headless(_cfg()) is False

    def test_por_defecto_headless_global(self, monkeypatch):
        monkeypatch.delenv("SICOP_HEADLESS", raising=False)
        assert nav.resolver_headless(SimpleNamespace(headless=False)) is False
        assert nav.resolver_headless(SimpleNamespace()) is True

    def test_headful_llega_a_launch(self, monkeypatch, sin_espera):
        monkeypatch.setenv("DISPLAY", ":0")  # ya hay pantalla: no se levanta Xvfb
        pw, _ = _correr([], lambda n: asyncio.sleep(0), monkeypatch, headless=False)
        assert pw.launch_kwargs["headless"] is False
        assert "env" not in pw.launch_kwargs


class FakeProc:
    def __init__(self):
        self.terminado = False

    def poll(self):
        return None

    def terminate(self):
        self.terminado = True


class TestPantallaVirtual:
    def test_headful_sin_display_levanta_xvfb_y_lo_cierra(self, monkeypatch, sin_espera):
        monkeypatch.delenv("DISPLAY", raising=False)
        proc = FakeProc()
        monkeypatch.setattr(nav, "_pantalla_virtual", lambda: (proc, ":99"))
        pw, _ = _correr([], lambda n: asyncio.sleep(0), monkeypatch, headless=False)
        assert pw.launch_kwargs["headless"] is False
        assert pw.launch_kwargs["env"]["DISPLAY"] == ":99"
        assert proc.terminado is True

    def test_headless_no_levanta_xvfb(self, monkeypatch, sin_espera):
        monkeypatch.delenv("DISPLAY", raising=False)

        def no_debe_llamarse():
            raise AssertionError("no debe levantarse Xvfb en headless")

        monkeypatch.setattr(nav, "_pantalla_virtual", no_debe_llamarse)
        pw, _ = _correr([], lambda n: asyncio.sleep(0), monkeypatch, headless=True)
        assert pw.launch_kwargs["headless"] is True
        assert "env" not in pw.launch_kwargs

    def test_xvfb_se_cierra_aunque_falle_el_arranque(self, monkeypatch, sin_espera):
        monkeypatch.delenv("DISPLAY", raising=False)
        proc = FakeProc()
        monkeypatch.setattr(nav, "_pantalla_virtual", lambda: (proc, ":99"))
        pw = PlaywrightFalso([])
        pw.instalar(monkeypatch)

        async def goto_falla(*a, **kw):
            raise RuntimeError("net::ERR")

        pw.pagina.goto = goto_falla

        async def flujo():
            async with nav.NavegadorAnonimo(_cfg(), sicop.ORIGEN_SPA, headless=False):
                pass

        with pytest.raises(PortalNoDisponible):
            asyncio.run(flujo())
        assert proc.terminado is True

    def test_sin_binario_xvfb_es_portal_no_disponible(self, monkeypatch):
        monkeypatch.setattr(nav.shutil, "which", lambda nombre: None)
        with pytest.raises(PortalNoDisponible) as exc:
            nav._pantalla_virtual()
        assert "Xvfb" in str(exc.value)

    def test_levanta_xvfb_con_display_libre_y_sin_escuchar_por_red(self, monkeypatch):
        llamadas = []

        class P(FakeProc):
            pass

        def popen(cmd, **kw):
            llamadas.append(cmd)
            return P()

        monkeypatch.setattr(nav.shutil, "which", lambda nombre: "/usr/bin/Xvfb")
        monkeypatch.setattr(nav.subprocess, "Popen", popen)
        existentes = {"/tmp/.X99-lock", "/tmp/.X11-unix/X100"}  # :99 ocupado; el socket de :100 aparece
        monkeypatch.setattr(nav.os.path, "exists",
                            lambda ruta: ruta in existentes)
        proc, display = nav._pantalla_virtual()
        assert display == ":100"
        assert llamadas[0][:2] == ["/usr/bin/Xvfb", ":100"]
        assert "-nolisten" in llamadas[0] and "tcp" in llamadas[0]


class TestBinario:
    def test_bytes(self, monkeypatch, sin_espera):
        b64 = base64.b64encode(b"%PDF-1.7").decode()
        _, datos = _correr([{"status": 200, "contentType": "application/octet-stream", "b64": b64}],
                           lambda n: n.binario("https://x/f", 100), monkeypatch)
        assert datos == b"%PDF-1.7"

    def test_grande_es_none(self, monkeypatch, sin_espera):
        _, datos = _correr([{"status": 200, "contentType": "application/octet-stream", "grande": True}],
                           lambda n: n.binario("https://x/f", 1), monkeypatch)
        assert datos is None

    def test_waf_en_descarga(self, monkeypatch, sin_espera):
        pw = PlaywrightFalso([{"status": 200, "contentType": "text/html", "texto": WAF}])
        pw.instalar(monkeypatch)

        async def flujo():
            async with nav.NavegadorAnonimo(_cfg(), sicop.ORIGEN_SPA) as n:
                await n.binario("https://x/f", 100)

        with pytest.raises(BloqueoAcceso):
            asyncio.run(flujo())

    def test_404_es_none(self, monkeypatch, sin_espera):
        _, datos = _correr([{"status": 404, "contentType": "application/json", "texto": "{}"}],
                           lambda n: n.binario("https://x/f", 100), monkeypatch)
        assert datos is None


class TestSonda:
    def test_sonda_pasa(self, monkeypatch, sin_espera):
        pw = PlaywrightFalso([_json('{"bienes": 1}'), _json()])
        pw.instalar(monkeypatch)
        r = asyncio.run(nav.sonda(_cfg()))
        assert r["pasa_waf"] is True and r["ua_dice_headless"] is True
        assert [c["status"] for c in r["llamadas"]] == [200, 200]
        assert pw.context_args == ((), {})

    def test_sonda_bloqueada_sale_con_2(self, monkeypatch, sin_espera, capsys):
        bloqueo = {"status": 200, "contentType": "text/html", "texto": WAF}
        PlaywrightFalso([bloqueo, bloqueo]).instalar(monkeypatch)
        monkeypatch.setattr(nav, "_config_para_sonda", _cfg)
        assert nav.main(["--sonda"]) == 2
        salida = json.loads(capsys.readouterr().out)
        assert salida["pasa_waf"] is False and all(c["bloqueo_waf"] for c in salida["llamadas"])

    def test_sin_sonda_muestra_ayuda(self, capsys):
        assert nav.main([]) == 1
