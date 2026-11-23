"""Tests de src.scraper.sicop — sin red: transporte falso con las fixtures reales del spike.

Contrato: docs/arquitectura/sicop-api.md; diseño: docs/arquitectura/portales.md §2.4 y §11 (A3).
"""
from __future__ import annotations

import ast
import asyncio
import copy
import json
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.portales import PORTALES
from src.portales.contrato import BloqueoAcceso, ContratoRoto, TransporteJSON
from src.scraper import sicop

FIX = Path(__file__).parent / "fixtures" / "sicop"
PORTAL = PORTALES["sicop_cr"]
ZONA = sicop.ZONA
PII = ("executorNm", "executorId", "writerNm", "FUNCIONARIO", "G0000000000000", "colonesAccount", "certCont")


def _fx(nombre: str):
    texto = (FIX / nombre).read_text(encoding="utf-8")
    return json.loads(texto) if nombre.endswith(".json") else texto


def _cfg(tmp_path, **extra):
    return SimpleNamespace(data_dir=str(tmp_path), **extra)


class Falso:
    """TransporteJSON falso: el listado sale de `paginas` en orden; las partidas, de `partidas`."""

    def __init__(self, paginas, partidas=None, error=None):
        self.paginas = list(paginas)
        self.partidas = partidas if partidas is not None else _fx("epCartelProd_findByEpCartelId.json")
        self.error = error
        self.llamadas: list[tuple[str, str, object]] = []

    async def json(self, metodo, url, cuerpo=None):
        self.llamadas.append((metodo, url, copy.deepcopy(cuerpo)))
        if self.error is not None:
            raise self.error
        if url.endswith(sicop.RUTA_LISTADO):
            return self.paginas.pop(0)
        if url.endswith(sicop.RUTA_PARTIDAS):
            return copy.deepcopy(self.partidas(cuerpo) if callable(self.partidas) else self.partidas)
        raise AssertionError(f"URL inesperada {url}")

    async def binario(self, url, max_bytes):
        return None

    def de(self, ruta):
        return [c for c in self.llamadas if c[1].endswith(ruta)]


def _pagina(filas, last=True):
    base = _fx("epCartel_searchEpCartels_pagina.json")
    base["content"], base["last"] = filas, last
    return base


def _filas():
    return _fx("epCartel_searchEpCartels_pagina.json")["content"]


def _fila(numero: str, cartel: str, reg: str, **extra):
    fila = dict(_filas()[0], instCartelNo=numero, cartelNo=cartel, regDt=reg)
    fila.update(extra)
    return fila


# Fecha fija para que las fixtures (20-11-2026) caigan siempre en la ventana.
AHORA = datetime(2026, 11, 21, 7, 0, tzinfo=ZONA)


@pytest.fixture(autouse=True)
def _reloj(monkeypatch):
    real = sicop.ventana
    monkeypatch.setattr(sicop, "ventana", lambda desde, ahora=None: real(desde, ahora or AHORA))


def _extraer(cfg, transporte, desde=datetime(2026, 11, 20, 0, 0, tzinfo=ZONA)):
    return asyncio.run(sicop.extraer(cfg, PORTAL, desde, transporte=transporte))


class TestConstantesYContrato:
    def test_constantes_e4(self):
        assert sicop.API_BASE == "https://prod-api.sicop.go.cr/"
        assert sicop.ORIGEN_SPA == "https://www.sicop.go.cr/app/"

    def test_falso_cumple_el_protocolo(self):
        assert isinstance(Falso([]), TransporteJSON)

    def test_sicop_no_importa_playwright_ni_el_navegador_al_cargarse(self):
        arbol = ast.parse(Path(sicop.__file__).read_text(encoding="utf-8"))
        de_modulo = [n for n in arbol.body if isinstance(n, (ast.Import, ast.ImportFrom))]
        nombres = [getattr(n, "module", None) or n.names[0].name for n in de_modulo]
        assert not any(m.startswith(("playwright", "src.scraper.navegador_anonimo")) for m in nombres)
        assert "src.scraper.sicop" in sys.modules

    def test_extraer_es_el_extractor_registrado(self):
        modulo, _, atributo = PORTAL.extractor.partition(":")
        assert modulo == "src.scraper.sicop" and atributo == "extraer"


class TestWAF:
    def test_fixture_de_bloqueo_es_bloqueo(self):
        assert sicop.es_bloqueo_waf(200, "text/html; charset=utf-8", _fx("prod-api_bloqueo_waf.html"))

    def test_texto_plano_con_200_es_bloqueo(self):
        assert sicop.es_bloqueo_waf(200, "text/plain", "No fue posible acceder a la página solicitada")

    def test_cuerpo_html_aunque_diga_json(self):
        assert sicop.es_bloqueo_waf(200, "application/json", "<html>…</html>")

    def test_json_real_no_es_bloqueo(self):
        assert not sicop.es_bloqueo_waf(200, "application/json", json.dumps(_fx("epCartel_searchEpCartels_vacio.json")))

    def test_422_json_no_es_bloqueo(self):
        assert not sicop.es_bloqueo_waf(422, "application/json", json.dumps(_fx("epCartel_searchEpCartels_422_rango_fechas.json")))

    def test_5xx_sin_marca_no_es_bloqueo(self):
        assert not sicop.es_bloqueo_waf(503, "text/html", "<html>Service Unavailable</html>")

    def test_interpretar_html_200_levanta_bloqueo(self):
        with pytest.raises(BloqueoAcceso):
            sicop.interpretar_respuesta(200, "text/html", _fx("prod-api_bloqueo_waf.html"))

    @pytest.mark.parametrize("fixture", [
        "epCartel_searchEpCartels_422_rango_fechas.json", "epCartel_searchEpCartels_422_tamano_pagina.json",
    ])
    def test_422_es_contrato_roto_con_mensaje(self, fixture):
        with pytest.raises(ContratoRoto) as e:
            sicop.interpretar_respuesta(422, "application/json", json.dumps(_fx(fixture)))
        assert not isinstance(e.value, BloqueoAcceso)
        assert e.value.detalle == _fx(fixture)["message"]

    def test_json_invalido_es_contrato_roto(self):
        with pytest.raises(ContratoRoto):
            sicop.interpretar_respuesta(200, "application/json", "{no-json")

    def test_5xx_es_transitorio(self):
        with pytest.raises(sicop._Transitorio):
            sicop.interpretar_respuesta(502, "text/html", "<html>bad gateway</html>")

    def test_bloqueo_se_propaga_sin_reintento(self, tmp_path):
        t = Falso([], error=BloqueoAcceso("sicop_cr", "waf"))
        with pytest.raises(BloqueoAcceso):
            _extraer(_cfg(tmp_path), t)
        assert len(t.llamadas) == 1


class TestListado:
    def test_cuerpo_de_la_corrida_diaria(self):
        hoy = AHORA.date() - timedelta(days=1)
        cuerpo = sicop.cuerpo_listado(hoy, hoy, 0, 50)
        esperado = _fx("epCartel_searchEpCartels_request.json")
        assert cuerpo["formFilters"] == esperado["formFilters"]
        assert cuerpo["pageSize"] == 50 and cuerpo["tableSorter"] == {"sorter": "regDt", "order": "desc"}

    def test_openbid_siempre_presente_y_menor_a_un_anio(self, tmp_path):
        t = Falso([_pagina([])])
        _extraer(_cfg(tmp_path), t)
        filtros = {f["field"]: f["value"] for f in t.llamadas[0][2]["formFilters"]}
        d1 = datetime.strptime(filtros["openbidDtFrom"], "%d/%m/%Y")
        d2 = datetime.strptime(filtros["openbidDtTo"], "%d/%m/%Y")
        assert (d2 - d1).days < 366
        assert filtros["regDtFrom"] == "20/11/2026" and filtros["regDtTo"] == "21/11/2026"
        assert filtros["cartelTestYn"] == "Y"

    def test_paginacion_dos_paginas_hasta_last(self, tmp_path):
        f1, f2 = _filas()
        t = Falso([_pagina([f1], last=False), _pagina([f2], last=True)])
        procesos = _extraer(_cfg(tmp_path), t)
        listados = t.de(sicop.RUTA_LISTADO)
        assert [c[2]["pageNumber"] for c in listados] == [0, 1]
        assert {p.numero_referencia for p in procesos} == {f1["instCartelNo"], f2["instCartelNo"]}

    def test_pagina_vacia_corta(self, tmp_path):
        t = Falso([_pagina([], last=False)])
        assert _extraer(_cfg(tmp_path), t) == []
        assert len(t.llamadas) == 1

    def test_vacio_real_es_lista_vacia(self, tmp_path):
        assert _extraer(_cfg(tmp_path), Falso([_fx("epCartel_searchEpCartels_vacio.json")])) == []

    def test_tope_de_paginas(self, tmp_path):
        t = Falso([_pagina([_filas()[0]], last=False) for _ in range(3)])
        _extraer(_cfg(tmp_path, sicop_max_paginas=2), t)
        assert len(t.de(sicop.RUTA_LISTADO)) == 2

    @pytest.mark.parametrize("valor,esperado", [(50, 50), (20, 20), (10, 10), (51, 50), (100, 50), (None, 50)])
    def test_page_size_se_corrige(self, valor, esperado):
        assert sicop.page_size(SimpleNamespace(sicop_page_size=valor)) == esperado

    def test_filtro_exacto_por_segundos(self, tmp_path):
        f1, f2 = _filas()  # 19:49:29 y 19:02:18
        desde = datetime(2026, 11, 20, 19, 49, 29, tzinfo=ZONA)
        procesos = _extraer(_cfg(tmp_path), Falso([_pagina([f1, f2])]), desde)
        assert [p.numero_referencia for p in procesos] == [f1["instCartelNo"]]

    def test_ventana_se_recorta_a_31_dias(self, tmp_path, caplog):
        t = Falso([_pagina([])])
        with caplog.at_level("WARNING"):
            _extraer(_cfg(tmp_path), t, desde=AHORA - timedelta(days=90))
        filtros = {f["field"]: f["value"] for f in t.llamadas[0][2]["formFilters"]}
        assert filtros["regDtFrom"] == (AHORA - timedelta(days=31)).strftime("%d/%m/%Y")
        assert "se recorta" in caplog.text

    def test_desde_naive_se_toma_en_costa_rica(self):
        efectivo, _ = sicop.ventana(datetime(2026, 11, 20, 8, 0), AHORA)
        assert efectivo.tzinfo is not None and efectivo.utcoffset() == timedelta(hours=-6)

    def test_deduplica_quedandose_con_la_mas_reciente(self, tmp_path):
        a = _fila("2026LD-000001-0000000001", "1", "20-11-2026 08:00:00", cartelNm="viejo")
        b = _fila("2026LD-000001-0000000001", "1", "20-11-2026 09:00:00", cartelNm="nuevo")
        procesos = _extraer(_cfg(tmp_path), Falso([_pagina([a, b])]))
        assert len(procesos) == 1 and procesos[0].descripcion == "nuevo"

    @pytest.mark.parametrize("quitar", ["content", "totalElements"])
    def test_pagina_sin_campos_minimos(self, tmp_path, quitar):
        pagina = _pagina(_filas())
        del pagina[quitar]
        with pytest.raises(ContratoRoto) as e:
            _extraer(_cfg(tmp_path), Falso([pagina]))
        assert e.value.detalle == quitar

    @pytest.mark.parametrize("quitar", ["instCartelNo", "cartelNo", "regDt"])
    def test_fila_sin_campos_minimos(self, tmp_path, quitar):
        fila = dict(_filas()[0])
        del fila[quitar]
        with pytest.raises(ContratoRoto) as e:
            _extraer(_cfg(tmp_path), Falso([_pagina([fila])]))
        assert e.value.detalle == quitar

    def test_respuesta_que_no_es_objeto(self, tmp_path):
        with pytest.raises(ContratoRoto):
            _extraer(_cfg(tmp_path), Falso([["no", "page"]]))


class TestMapeo:
    def test_mapeo_completo_con_partidas(self, tmp_path):
        fila = _filas()[1]
        p, = _extraer(_cfg(tmp_path), Falso([_pagina([fila])]))
        assert p.portal_id == "sicop_cr"
        assert p.numero_referencia == "2026LD-000364-0015499999"
        assert p.id_externo == "20261102189"
        assert p.institucion == "MUNICIPALIDAD DEL CANTÓN CENTRAL DE SAN JOSÉ"
        assert p.descripcion.startswith("Soporte y mantenimiento integral")
        assert p.fecha_publicacion == datetime(2026, 11, 20, 19, 2, 18, tzinfo=ZONA)
        assert p.fecha_limite == datetime(2026, 11, 27, 11, 0, tzinfo=ZONA)
        assert p.modalidad_codigo == "LD"
        assert p.forma_contratacion == "LICITACIÓN REDUCIDA"
        assert p.tipo_actividad == ""
        assert p.url_directa == PORTAL.url_consulta == "https://www.sicop.go.cr/app/module/bid/public/tenders"
        assert p.datos_portal["instruccion_busqueda"].endswith("2026LD-000364-0015499999")
        assert p.datos_portal["cartel_seq"] == "00"
        assert p.datos_portal["estado"] == "En recepción de ofertas"
        assert p.datos_portal["fecha_apertura"] == "2026-11-27T11:01:00-06:00"
        # partidas de la fixture: CRC, UNSPSC y nombres de línea
        assert p.moneda == "CRC" and p.monto == pytest.approx(37081788.8 + 300000)
        assert p.montos_por_moneda == {}
        assert p.datos_portal["unspsc"] == ["43211503", "53121706"]
        assert p.detalle.startswith("COMPUTADORA PORTÁTIL") and " · BOLSO TIPO MALETIN" in p.detalle
        assert len(p.detalle) <= 600

    def test_fecha_iso_del_detalle(self):
        assert sicop.parsear_fecha("2026-11-20T19:49:29") == datetime(2026, 11, 20, 19, 49, 29, tzinfo=ZONA)
        assert sicop.parsear_fecha("2026-11-20T19:49:29.123") == datetime(2026, 11, 20, 19, 49, 29, tzinfo=ZONA)
        assert sicop.parsear_fecha("basura") is None and sicop.parsear_fecha(None) is None

    def test_partida_en_usd(self, tmp_path):
        partidas = _fx("epCartelProd_findByEpCartelId.json")
        for linea in partidas["content"]:
            linea["currencyType"] = "USD"
        p, = _extraer(_cfg(tmp_path), Falso([_pagina([_filas()[0]])], partidas=partidas))
        assert p.moneda == "USD" and p.monto == pytest.approx(37381788.8)

    def test_mezcla_crc_usd_no_se_suma(self, tmp_path):
        partidas = _fx("epCartelProd_findByEpCartelId.json")
        partidas["content"][1]["currencyType"] = "USD"
        p, = _extraer(_cfg(tmp_path), Falso([_pagina([_filas()[0]])], partidas=partidas))
        assert p.monto is None and p.moneda == ""
        assert p.montos_por_moneda == {"CRC": pytest.approx(37081788.8), "USD": pytest.approx(300000)}

    def test_sin_moneda_nunca_se_rotula_usd(self):
        r = sicop.resumir_partidas([{"budgetAmt": 100, "currencyType": None}])
        assert r["monto"] == 100 and r["moneda"] == ""

    def test_detalle_se_trunca_a_600(self):
        lineas = [{"prodNm": f"PRODUCTO {i} " + "X" * 80, "prodId": "4321150300000000"} for i in range(20)]
        r = sicop.resumir_partidas(lineas)
        assert 0 < len(r["detalle"]) <= 600 and r["unspsc"] == ["43211503"]

    def test_ningun_dato_personal(self, tmp_path):
        procesos = _extraer(_cfg(tmp_path), Falso([_pagina(_filas())]))
        assert procesos
        for p in procesos:
            volcado = json.dumps({**vars(p), "fecha_publicacion": None, "fecha_limite": None}, ensure_ascii=False)
            for marca in PII:
                assert marca not in volcado

    def test_formatos_de_referencia_disjuntos_con_comprasal(self):
        sicop_re = re.compile(r"^\d{4}[A-Z]{2}-\d{6}-\d{10}$")
        comprasal_re = re.compile(r"^\d+-\d{4}-[A-Z]\d+$")
        refs = [f["instCartelNo"] for f in _filas()] + ["2026XE-000010-0001234567"]
        for ref in refs:
            assert sicop_re.match(ref) and not comprasal_re.match(ref)
        for ref in ("96-2026-P0011", "1234-2026-L0001"):
            assert comprasal_re.match(ref) and not sicop_re.match(ref)


class TestPartidasYCortesia:
    def test_remate_y_confidencial_no_piden_partidas(self, tmp_path):
        filas = [
            _fila("2026RE-000001-0000000001", "11", "20-11-2026 10:00:00", proceType="RE"),
            _fila("2026LD-000002-0000000002", "12", "20-11-2026 10:00:00", cartelSecretYn="Y"),
            _fila("2026LD-000003-0000000003", "13", "20-11-2026 10:00:00"),
        ]
        t = Falso([_pagina(filas)])
        procesos = _extraer(_cfg(tmp_path), t)
        pedidas = [c[2]["formFilters"][0]["value"] for c in t.de(sicop.RUTA_PARTIDAS)]
        assert pedidas == ["13"]
        assert {p.id_externo: p.monto is None for p in procesos} == {"11": True, "12": True, "13": False}

    def test_tope_max_detalles(self, tmp_path, caplog):
        filas = [_fila(f"2026LD-00000{i}-000000000{i}", str(i), "20-11-2026 10:00:00") for i in range(5)]
        t = Falso([_pagina(filas)])
        with caplog.at_level("WARNING"):
            procesos = _extraer(_cfg(tmp_path, sicop_max_detalles=2), t)
        assert len(t.de(sicop.RUTA_PARTIDAS)) == 2
        assert sum(p.monto is not None for p in procesos) == 2
        assert "SICOP_MAX_DETALLES" in caplog.text

    def test_partidas_con_pagesize_100(self, tmp_path):
        t = Falso([_pagina([_filas()[0]])])
        _extraer(_cfg(tmp_path), t)
        cuerpo = t.de(sicop.RUTA_PARTIDAS)[0][2]
        assert cuerpo["pageSize"] == 100
        assert cuerpo["formFilters"] == [
            {"field": "cartelNo", "value": "20261102967"}, {"field": "cartelSeq", "value": "00"},
        ]

    def test_cache_segunda_corrida_sin_partidas(self, tmp_path):
        cfg = _cfg(tmp_path)
        _extraer(cfg, Falso([_pagina(_filas())]))
        ruta = tmp_path / "cache" / "sicop" / "20261102967-00.partidas.json"
        assert ruta.exists()
        t2 = Falso([_pagina(_filas())])
        procesos = _extraer(cfg, t2)
        assert t2.de(sicop.RUTA_PARTIDAS) == []
        assert all(p.moneda == "CRC" for p in procesos)

    def test_cache_vencida_se_purga_y_se_repide(self, tmp_path):
        cfg = _cfg(tmp_path, sicop_cache_dias=3)
        _extraer(cfg, Falso([_pagina([_filas()[0]])]))
        ruta = tmp_path / "cache" / "sicop" / "20261102967-00.partidas.json"
        viejo = ruta.stat().st_mtime - 4 * 86400
        os.utime(ruta, (viejo, viejo))
        t2 = Falso([_pagina([_filas()[0]])])
        _extraer(cfg, t2)
        assert len(t2.de(sicop.RUTA_PARTIDAS)) == 1

    def test_purgar_borra_solo_lo_vencido(self, tmp_path):
        cache = sicop.CacheSicop(_cfg(tmp_path), dias=3)
        cache.guardar("1", "00", "partidas", [])
        cache.guardar("2", "00", "partidas", [])
        viejo = os.path.getmtime(cache.ruta("1", "00", "partidas")) - 5 * 86400
        os.utime(cache.ruta("1", "00", "partidas"), (viejo, viejo))
        assert cache.purgar() == 1
        assert cache.leer("2", "00", "partidas") == [] and cache.leer("1", "00", "partidas") is None

    def test_nombre_de_cache_no_escapa_del_directorio(self, tmp_path):
        cache = sicop.CacheSicop(_cfg(tmp_path))
        assert os.path.dirname(cache.ruta("../../etc", "00", "partidas")) == cache.dir

    def test_partidas_paginadas(self, tmp_path):
        base = _fx("epCartelProd_findByEpCartelId.json")

        def partidas(cuerpo):
            pagina = copy.deepcopy(base)
            pagina["last"] = cuerpo["pageNumber"] == 1
            return pagina

        t = Falso([_pagina([_filas()[0]])], partidas=partidas)
        p, = _extraer(_cfg(tmp_path), t)
        assert len(t.de(sicop.RUTA_PARTIDAS)) == 2
        assert p.monto == pytest.approx(2 * (37081788.8 + 300000))

    def test_extraer_sin_transporte_usa_navegador_anonimo(self, tmp_path, monkeypatch):
        import src.scraper.navegador_anonimo as nav

        usados = []

        class NavFalso(Falso):
            def __init__(self, config, origen, **kw):
                super().__init__([_pagina([])])
                usados.append(origen)

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return None

        monkeypatch.setattr(nav, "NavegadorAnonimo", NavFalso)
        resultado = asyncio.run(sicop.extraer(_cfg(tmp_path), PORTAL, AHORA - timedelta(days=1)))
        assert resultado == [] and usados == [sicop.ORIGEN_SPA]
