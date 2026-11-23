"""Tests de src.mercado.sicop_publica — sin red: transporte falso y fixtures de `tests/fixtures/sicop/`.

Contrato en `docs/arquitectura/sicop-api.md` §6 y `docs/arquitectura/portales.md` §2.4 y §10.4 (A8).
"""
import ast
import asyncio
import copy
import json
import sys
from datetime import date, datetime
from pathlib import Path

import pytest

from src.mercado import sicop_publica as sp
from src.portales.contrato import BloqueoAcceso, ContratoRoto, PortalNoDisponible

_RAIZ = Path(__file__).resolve().parents[1]
_FIX = _RAIZ / "tests" / "fixtures" / "sicop"


def _fixture(nombre: str):
    return json.loads((_FIX / nombre).read_text(encoding="utf-8"))


def _waf() -> str:
    return (_FIX / "prod-api_bloqueo_waf.html").read_text(encoding="utf-8")


class TransporteFalso:
    """`TransporteJSON` falso: entrega las respuestas en orden (o las lanza si son excepciones)."""

    def __init__(self, respuestas):
        self.respuestas = list(respuestas)
        self.llamadas: list[tuple[str, str, dict]] = []

    async def json(self, metodo, url, cuerpo=None):
        self.llamadas.append((metodo, url, copy.deepcopy(cuerpo)))
        r = self.respuestas.pop(0)
        if isinstance(r, BaseException):
            raise r
        return copy.deepcopy(r)

    async def binario(self, url, max_bytes):
        return None


class Dormir:
    def __init__(self):
        self.esperas: list[float] = []

    async def __call__(self, s):
        self.esperas.append(s)


def _cliente(transporte, **kw):
    dormir = Dormir()
    kw.setdefault("reloj", lambda: 0.0)   # reloj quieto: cada pausa espera el total
    return sp.ClienteSicopPublico(transporte, dormir=dormir, **kw), dormir


async def _todo(iterador):
    return [p async for p in iterador]


def _pagina(base: str, *, last: bool):
    p = _fixture(base)
    p["last"] = last
    return p


class TestUtilidades:
    def test_medianoche_cr_en_utc_como_la_spa(self):
        assert sp.iso_utc_medianoche_cr(date(2025, 11, 23)) == "2025-11-23T06:00:00.000Z"

    def test_cuerpo_descarta_filtros_vacios_y_corrige_el_tamano(self):
        c = sp.cuerpo_pagina(2, {"a": "x", "b": None, "c": ""}, tamano=100)
        assert c["pageNumber"] == 2 and c["pageSize"] == 50
        assert c["formFilters"] == [{"field": "a", "value": "x"}]

    @pytest.mark.parametrize("valor, esperado", [
        ("30/10/2026 17:26", datetime(2026, 10, 30, 17, 26)),
        ("2026-04-13T15:52:16", datetime(2026, 4, 13, 15, 52, 16)),
        ("20-11-2026 19:49:29", datetime(2026, 11, 20, 19, 49, 29)),
        (None, None),
        ("basura", None),
    ])
    def test_parse_fecha_sicop(self, valor, esperado):
        assert sp.parse_fecha_sicop(valor) == esperado

    def test_bloqueo_waf(self):
        assert sp.es_bloqueo_waf(200, "text/html", _waf())
        assert sp.es_bloqueo_waf(200, "", "No fue posible acceder a la página")
        assert not sp.es_bloqueo_waf(200, "application/json", '{"content": []}')


class TestAdjudicadoAProveedor:
    def test_una_pagina_con_filtros_de_cedula_y_fechas(self):
        t = TransporteFalso([_fixture("epAwardedProcedureSupplier_listPage.json")])
        cliente, _ = _cliente(t)
        paginas = asyncio.run(_todo(cliente.adjudicado_a_proveedor("3102813498", date(2025, 11, 23), date(2026, 11, 23))))
        assert len(paginas) == 1 and paginas[0][0]["instCartelNo"] == "2025LD-000004-0000900006"
        metodo, url, cuerpo = t.llamadas[0]
        assert metodo == "POST" and url == sp.URL_ADJUDICADO_PROVEEDOR
        filtros = {f["field"]: f["value"] for f in cuerpo["formFilters"]}
        assert filtros == {"startDt": "2025-11-23T06:00:00.000Z", "endDt": "2026-11-23T06:00:00.000Z",
                           "supplierCd": "3102813498"}
        assert len(t.llamadas) == 1   # `last: true` corta


class TestAdjudicadoEnFirme:
    def test_pagina_hasta_last(self):
        t = TransporteFalso([_pagina("epAdjuFirmAwarded_listPage.json", last=False),
                             _pagina("epAdjuFirmAwarded_listPage.json", last=True)])
        cliente, dormir = _cliente(t)
        paginas = asyncio.run(_todo(cliente.adjudicado_en_firme(date(2026, 10, 1), date(2026, 11, 1))))
        assert [len(p) for p in paginas] == [3, 3]
        assert [c[2]["pageNumber"] for c in t.llamadas] == [0, 1]
        assert t.llamadas[0][1] == sp.URL_ADJUDICADO_FIRME
        filtros = {f["field"] for f in t.llamadas[0][2]["formFilters"]}
        assert filtros == {"bgnYmd", "endYmd"}
        assert cliente.truncado is False
        assert dormir.esperas == [1.0]   # pausa entre las dos llamadas

    def test_tope_de_paginas_trunca_y_avisa(self, caplog):
        t = TransporteFalso([_pagina("epAdjuFirmAwarded_listPage.json", last=False)] * 3)
        cliente, _ = _cliente(t, max_paginas=2)
        with caplog.at_level("WARNING"):
            paginas = asyncio.run(_todo(cliente.adjudicado_en_firme(date(2026, 10, 1), date(2026, 11, 1))))
        assert len(paginas) == 2 and len(t.llamadas) == 2
        assert cliente.truncado is True
        assert "tope" in caplog.text

    def test_pagina_vacia_corta(self):
        vacia = {"content": [], "last": False, "totalPages": 5}
        t = TransporteFalso([vacia])
        cliente, _ = _cliente(t)
        assert asyncio.run(_todo(cliente.adjudicado_en_firme(date(2026, 10, 1), date(2026, 11, 1)))) == []
        assert len(t.llamadas) == 1


class TestErrores:
    def _correr(self, respuestas, **kw):
        t = TransporteFalso(respuestas)
        cliente, dormir = _cliente(t, **kw)
        return t, cliente, dormir

    def test_waf_html_es_bloqueo_sin_reintento(self):
        t, cliente, _ = self._correr([_waf(), _waf(), _waf()])
        with pytest.raises(BloqueoAcceso):
            asyncio.run(_todo(cliente.adjudicado_en_firme(date(2026, 10, 1), date(2026, 11, 1))))
        assert len(t.llamadas) == 1

    def test_waf_en_sobre_del_navegador_es_bloqueo_sin_reintento(self):
        sobre = {"status": 200, "contentType": "text/html; charset=utf-8", "texto": _waf()}
        t, cliente, _ = self._correr([sobre, sobre])
        with pytest.raises(BloqueoAcceso):
            asyncio.run(_todo(cliente.adjudicado_a_proveedor("9000018410", date(2026, 1, 1), date(2026, 2, 1))))
        assert len(t.llamadas) == 1

    def test_bloqueo_lanzado_por_el_transporte_no_se_reintenta(self):
        t, cliente, _ = self._correr([BloqueoAcceso("sicop_cr", "waf"), {"content": []}])
        with pytest.raises(BloqueoAcceso):
            asyncio.run(_todo(cliente.adjudicado_en_firme(date(2026, 10, 1), date(2026, 11, 1))))
        assert len(t.llamadas) == 1

    def test_422_es_contrato_roto(self):
        error = _fixture("epCartel_searchEpCartels_422_tamano_pagina.json")
        t, cliente, _ = self._correr([error, error])
        with pytest.raises(ContratoRoto):
            asyncio.run(_todo(cliente.adjudicado_en_firme(date(2026, 10, 1), date(2026, 11, 1))))
        assert len(t.llamadas) == 1

    def test_5xx_reintenta_dos_veces_y_se_rinde(self):
        sobre = {"status": 503, "contentType": "application/json", "texto": "{}"}
        t, cliente, dormir = self._correr([sobre] * 3)
        with pytest.raises(PortalNoDisponible):
            asyncio.run(_todo(cliente.adjudicado_en_firme(date(2026, 10, 1), date(2026, 11, 1))))
        assert len(t.llamadas) == 3
        assert 5.0 in dormir.esperas and 15.0 in dormir.esperas

    def test_transitorio_y_luego_ok(self):
        t, cliente, _ = self._correr([TimeoutError("lento"), _pagina("epAdjuFirmAwarded_listPage.json", last=True)])
        paginas = asyncio.run(_todo(cliente.adjudicado_en_firme(date(2026, 10, 1), date(2026, 11, 1))))
        assert len(paginas) == 1 and len(t.llamadas) == 2

    def test_sin_content_es_contrato_roto(self):
        t, cliente, _ = self._correr([{"algo": 1}])
        with pytest.raises(ContratoRoto, match="content"):
            asyncio.run(_todo(cliente.adjudicado_en_firme(date(2026, 10, 1), date(2026, 11, 1))))

    def test_fila_sin_numero_es_contrato_roto(self):
        p = _pagina("epAdjuFirmAwarded_listPage.json", last=True)
        del p["content"][1]["instCartelNo"]
        t, cliente, _ = self._correr([p])
        with pytest.raises(ContratoRoto, match="instCartelNo"):
            asyncio.run(_todo(cliente.adjudicado_en_firme(date(2026, 10, 1), date(2026, 11, 1))))

    def test_pausa_minima_de_un_segundo(self):
        t = TransporteFalso([_pagina("epAdjuFirmAwarded_listPage.json", last=False),
                             _pagina("epAdjuFirmAwarded_listPage.json", last=True)])
        cliente, dormir = _cliente(t, pausa_s=0.1)
        asyncio.run(_todo(cliente.adjudicado_en_firme(date(2026, 10, 1), date(2026, 11, 1))))
        assert cliente.pausa_s == 1.0 and dormir.esperas == [1.0]


class TestBuscarProveedor:
    def test_devuelve_las_filas(self):
        t = TransporteFalso([_fixture("umPublicSupplier_searchUmPublicSupplier.json")])
        cliente, _ = _cliente(t)
        filas = asyncio.run(cliente.buscar_proveedor(nombre="MULTICOMPUTOS"))
        assert filas[0]["bizRegNo"] == "9000018410"
        assert t.llamadas[0][1] == sp.URL_BUSCAR_PROVEEDOR


class TestNormalizacion:
    def test_fila_firme_montos_separados_y_sin_adjudicatario(self):
        crudo = _fixture("epAdjuFirmAwarded_listPage.json")["content"][0]
        f = sp.fila_firme(crudo)
        assert f["portal_id"] == "sicop_cr"
        assert f["codigo_proceso"] == "2026PX-000084-0018100001"
        assert f["id_externo"] == "20261004283" and f["partida"] == "1"
        assert f["modalidad_codigo"] == "PX"
        assert f["fecha_adjudicacion"] == datetime(2026, 10, 30, 17, 26)
        assert f["monto_crc"] == 244800 and f["monto_usd"] == pytest.approx(537.502195678904)
        assert f["proveedor_id"] == sp.PROVEEDOR_DESCONOCIDO and f["es_propio"] is False
        assert f["nombre_proceso"] == "SEMINARIO"   # sin el espacio final de SICOP
        assert f["datos_crudos"] is crudo

    def test_fila_adjudicada_sin_moneda_no_se_rotula(self):
        crudo = _fixture("epAwardedProcedureSupplier_listPage.json")["content"][0]
        f = sp.fila_adjudicada_a(crudo, "3102813498", es_propio=True)
        assert f["partida"] == sp.PARTIDA_SIN_DETALLE
        assert f["proveedor_id"] == "3102813498" and f["es_propio"] is True
        assert f["monto_crc"] is None and f["monto_usd"] is None   # currencyType null (D9)
        assert f["datos_crudos"]["submitAmt"] == pytest.approx(12545995.2366)
        assert f["fecha_adjudicacion"] == datetime(2026, 4, 13, 15, 52, 16)

    @pytest.mark.parametrize("moneda, col", [("CRC", "monto_crc"), ("USD", "monto_usd"), ("usd", "monto_usd")])
    def test_fila_adjudicada_con_moneda(self, moneda, col):
        crudo = dict(_fixture("epAwardedProcedureSupplier_listPage.json")["content"][0], currencyType=moneda)
        f = sp.fila_adjudicada_a(crudo, "3102813498", es_propio=True)
        otra = "monto_usd" if col == "monto_crc" else "monto_crc"
        assert f[col] == pytest.approx(12545995.2366) and f[otra] is None


class TestEsquema:
    def test_bloque_adjudicacion_partida(self):
        sql = (_RAIZ / "db" / "schema.sql").read_text(encoding="utf-8")
        assert "CREATE TABLE adjudicacion_partida" in sql
        bloque = sql.split("CREATE TABLE adjudicacion_partida", 1)[1].split("]';", 1)[0]
        for col in ("portal_id", "codigo_proceso", "id_externo", "partida", "nombre_proceso", "institucion",
                    "modalidad_codigo", "fecha_adjudicacion", "monto_crc", "monto_usd", "proveedor_id",
                    "es_propio", "datos_crudos", "cargado_en"):
            assert f"\n      {col} " in bloque, col
        assert "PRIMARY KEY (portal_id, codigo_proceso, partida, proveedor_id)" in bloque
        assert "proveedor_id        VARCHAR2" in bloque
        # Idempotente como el resto del DDL: ORA-00955 se ignora.
        cola = sql.split("CREATE TABLE adjudicacion_partida", 1)[1]
        assert "SQLCODE != -955" in cola.split("/", 1)[0]


class TestModulo:
    def test_no_importa_el_extractor_de_a3_ni_playwright(self):
        arbol = ast.parse(Path(sp.__file__).read_text(encoding="utf-8"))
        modulos = {n.module for n in ast.walk(arbol) if isinstance(n, ast.ImportFrom)}
        modulos |= {a.name for n in ast.walk(arbol) if isinstance(n, ast.Import) for a in n.names}
        assert not any(m.startswith(("src.scraper", "playwright")) for m in modulos if m)
        # Ningún literal fija un User-Agent (D3): el transporte es el navegador tal cual.
        literales = [n.value.lower() for n in ast.walk(arbol) if isinstance(n, ast.Constant) and isinstance(n.value, str)
                     and n is not arbol.body[0].value]
        assert not any("user_agent" in t or "user-agent" in t for t in literales)

    def test_documenta_la_limitacion_de_perdido(self):
        assert "PERDIDO" in sp.__doc__ and "Ofertas" in sp.__doc__


class TestCLI:
    def _main(self, monkeypatch, argv):
        from src.mercado import __main__ as cli
        monkeypatch.setattr(sys, "argv", ["src.mercado", *argv])
        monkeypatch.setattr(cli, "get_config", lambda: object())
        monkeypatch.setattr(cli, "_setup_logging", lambda: None)
        llamado = {}
        monkeypatch.setattr(cli, "_correr_sicop", lambda cfg, args: llamado.update(vars(args)))
        cli.main()
        return llamado

    def test_sicop_dry_run_por_defecto(self, monkeypatch):
        args = self._main(monkeypatch, ["--portal", "sicop_cr"])
        assert args["sicop_aplicar"] is False and args["sicop_mercado"] is False
        assert args["cliente_sicop"] == "multicomputos_cr"

    def test_sicop_aplicar_y_mercado(self, monkeypatch):
        args = self._main(monkeypatch, ["--portal", "sicop_cr", "--aplicar", "--mercado", "--max-paginas", "5"])
        assert args["sicop_aplicar"] and args["sicop_mercado"] and args["max_paginas"] == 5

    def test_sicop_con_subcomando_es_error(self, monkeypatch):
        with pytest.raises(SystemExit):
            self._main(monkeypatch, ["--portal", "sicop_cr", "resolver", "--cliente", "x"])

    def test_comprasal_sin_subcomando_es_error(self, monkeypatch):
        with pytest.raises(SystemExit):
            self._main(monkeypatch, [])
