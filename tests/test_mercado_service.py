"""Tests de src.mercado.service — orquestación, con ADB/red mockeadas.

Mismo patrón que tests/test_memoria_service.py: una conexión falsa como
context manager, y los submódulos (`api_publica`, `identidad`, `repo`)
parcheados para no tocar ni Oracle ni COMPRASAL.
"""
import asyncio
import json
import types
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.mercado import service, sicop_publica
from src.portales.contrato import BloqueoAcceso


class _ConexionFalsa:
    def __init__(self):
        self.commits = 0

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def commit(self):
        self.commits += 1


@pytest.fixture
def cfg():
    return types.SimpleNamespace()


class TestSincronizarMercado:
    def test_pagina_upsertea_cada_fila_y_comitea_por_pagina(self, monkeypatch, cfg):
        conexion = _ConexionFalsa()
        monkeypatch.setattr("src.memoria.db.connection", lambda c: conexion)

        paginas = [[{"id": 1}, {"id": 2}], [{"id": 3}]]
        monkeypatch.setattr(
            "src.mercado.api_publica.paginar_todo",
            lambda **kw: iter(paginas),
        )
        monkeypatch.setattr("src.mercado.identidad.es_nuestro", lambda item: item["id"] == 2)

        upserts = []
        monkeypatch.setattr(
            "src.mercado.repo.upsert_adjudicacion_item",
            lambda conn, item, es_multicomputos: upserts.append((item["id"], es_multicomputos)),
        )

        total = service.sincronizar_mercado(cfg)

        assert total == 3
        assert upserts == [(1, False), (2, True), (3, False)]
        assert conexion.commits == 2  # una vez por página, no una vez al final

    def test_reenvia_filtros_a_paginar_todo(self, monkeypatch, cfg):
        monkeypatch.setattr("src.memoria.db.connection", lambda c: _ConexionFalsa())
        capturado = {}

        def fake_paginar_todo(**kw):
            capturado.update(kw)
            return iter([])

        monkeypatch.setattr("src.mercado.api_publica.paginar_todo", fake_paginar_todo)

        service.sincronizar_mercado(cfg, id_institucion=25, modalidad=12, nombre="medicamentos")

        assert capturado == {"id_institucion": 25, "modalidad": 12, "nombre": "medicamentos"}

    def test_sin_filas_devuelve_cero(self, monkeypatch, cfg):
        monkeypatch.setattr("src.memoria.db.connection", lambda c: _ConexionFalsa())
        monkeypatch.setattr("src.mercado.api_publica.paginar_todo", lambda **kw: iter([]))
        assert service.sincronizar_mercado(cfg) == 0


class TestSincronizarParticipados:
    def test_delega_al_modulo_participacion(self, monkeypatch, cfg):
        async def fake_sync(cfg_, cliente_id, max_pages=0):
            assert cliente_id == "multicomputos"
            assert max_pages == 3
            return 7

        monkeypatch.setattr("src.mercado.participacion.sincronizar_participados", fake_sync)

        import asyncio
        resultado = asyncio.run(service.sincronizar_participados(cfg, "multicomputos", max_pages=3))
        assert resultado == 7


class TestResolverResultados:
    def test_devuelve_ganados_perdidos_y_pendientes(self, monkeypatch, cfg):
        monkeypatch.setattr("src.memoria.db.connection", lambda c: _ConexionFalsa())
        monkeypatch.setattr(
            "src.mercado.repo.resolver_resultados",
            lambda conn, cliente_id: {"ganados": 2, "perdidos": 5},
        )
        monkeypatch.setattr("src.mercado.repo.contar_pendientes", lambda conn, cliente_id: 10)

        resultado = service.resolver_resultados(cfg, "multicomputos")

        assert resultado == {"ganados": 2, "perdidos": 5, "pendientes": 10}

    def test_v2_apagado_no_llama_al_prepoblado(self, monkeypatch, cfg):
        """`cfg` sin `enable_esquema_v2` (default False): no debe ni intentarse."""
        monkeypatch.setattr("src.memoria.db.connection", lambda c: _ConexionFalsa())
        monkeypatch.setattr("src.mercado.repo.resolver_resultados", lambda conn, cliente_id: {"ganados": 0, "perdidos": 0})
        monkeypatch.setattr("src.mercado.repo.contar_pendientes", lambda conn, cliente_id: 0)
        prepoblar = MagicMock()
        monkeypatch.setattr("src.mercado.repo.prepoblar_oportunidad_estado_v2", prepoblar)

        service.resolver_resultados(cfg, "multicomputos")

        prepoblar.assert_not_called()

    def test_v2_activado_llama_al_prepoblado_con_el_cliente(self, monkeypatch, cfg):
        cfg.enable_esquema_v2 = True
        monkeypatch.setattr("src.memoria.db.connection", lambda c: _ConexionFalsa())
        monkeypatch.setattr("src.mercado.repo.resolver_resultados", lambda conn, cliente_id: {"ganados": 1, "perdidos": 1})
        monkeypatch.setattr("src.mercado.repo.contar_pendientes", lambda conn, cliente_id: 0)
        prepoblar = MagicMock()
        monkeypatch.setattr("src.mercado.repo.prepoblar_oportunidad_estado_v2", prepoblar)
        monkeypatch.setattr("src.mercado.repo.listar_conflictos_oportunidad_v2", lambda conn, cliente_id: [])

        service.resolver_resultados(cfg, "multicomputos")

        prepoblar.assert_called_once()
        args = prepoblar.call_args.args
        assert args[1] == "multicomputos"

    def test_v2_avisa_de_los_conflictos_entre_lo_manual_y_el_mercado(self, monkeypatch, cfg, caplog):
        cfg.enable_esquema_v2 = True
        monkeypatch.setattr("src.memoria.db.connection", lambda c: _ConexionFalsa())
        monkeypatch.setattr("src.mercado.repo.resolver_resultados", lambda conn, cliente_id: {"ganados": 1, "perdidos": 0})
        monkeypatch.setattr("src.mercado.repo.contar_pendientes", lambda conn, cliente_id: 0)
        monkeypatch.setattr("src.mercado.repo.prepoblar_oportunidad_estado_v2", MagicMock())
        monkeypatch.setattr(
            "src.mercado.repo.listar_conflictos_oportunidad_v2",
            lambda conn, cliente_id: [{"proceso_gid": "comprasal_sv:X1"}],
        )

        with caplog.at_level("WARNING"):
            resultado = service.resolver_resultados(cfg, "multicomputos")

        assert "comprasal_sv:X1" in caplog.text and "no se pisan" in caplog.text
        assert resultado["ganados"] == 1          # el aviso no cambia lo que informa v1

    def test_v2_fallando_no_afecta_el_resultado_devuelto(self, monkeypatch, cfg):
        """Un fallo del prepoblado v2 no debe cambiar lo que informa
        `resolver_resultados` (ganados/perdidos/pendientes son de v1)."""
        cfg.enable_esquema_v2 = True
        monkeypatch.setattr("src.memoria.db.connection", lambda c: _ConexionFalsa())
        monkeypatch.setattr("src.mercado.repo.resolver_resultados", lambda conn, cliente_id: {"ganados": 2, "perdidos": 5})
        monkeypatch.setattr("src.mercado.repo.contar_pendientes", lambda conn, cliente_id: 10)
        monkeypatch.setattr(
            "src.mercado.repo.prepoblar_oportunidad_estado_v2",
            MagicMock(side_effect=RuntimeError("boom")),
        )

        resultado = service.resolver_resultados(cfg, "multicomputos")

        assert resultado == {"ganados": 2, "perdidos": 5, "pendientes": 10}


# ── SICOP (docs/arquitectura/portales.md §10.4, A8) ──────────────────────────

_FIX_SICOP = Path(__file__).resolve().parent / "fixtures" / "sicop"


def _fx(nombre):
    return json.loads((_FIX_SICOP / nombre).read_text(encoding="utf-8"))


class _TransporteSicop:
    """Responde por endpoint: lo adjudicado sale solo para la cédula 3102813498."""

    def __init__(self, firme=None):
        self.firme = list(firme or [])
        self.llamadas = []

    async def json(self, metodo, url, cuerpo=None):
        self.llamadas.append(url)
        filtros = {f["field"]: f["value"] for f in cuerpo["formFilters"]}
        if url == sicop_publica.URL_ADJUDICADO_PROVEEDOR:
            if filtros["supplierCd"] == "3102813498":
                return _fx("epAwardedProcedureSupplier_listPage.json")
            return {"content": [], "last": True, "totalPages": 0}
        r = self.firme.pop(0)
        if isinstance(r, BaseException):
            raise r
        return r

    async def binario(self, url, max_bytes):
        return None


def _cliente_sicop(transporte):
    async def dormir(_s):
        return None
    return sicop_publica.ClienteSicopPublico(transporte, dormir=dormir, reloj=lambda: 0.0)


def _sync(cfg, transporte, **kw):
    return asyncio.run(service.sincronizar_sicop(
        cfg, transporte, desde=date(2025, 11, 23), hasta=date(2026, 11, 23),
        cliente_publico=_cliente_sicop(transporte), **kw,
    ))


class TestSincronizarSicop:
    def test_dry_run_no_abre_la_adb(self, monkeypatch, cfg):
        monkeypatch.setattr("src.memoria.db.connection", MagicMock(side_effect=AssertionError("no debe abrir")))
        t = _TransporteSicop()
        r = _sync(cfg, t)
        assert r["aplicado"] is False and r["propias"] == 1 and r["ganados"] == 0
        assert r["propias_sin_moneda"] == 1 and r["monto_propio_crc"] == 0.0   # currencyType null (D9)
        assert t.llamadas.count(sicop_publica.URL_ADJUDICADO_PROVEEDOR) == 2    # una por cédula
        assert sicop_publica.URL_ADJUDICADO_FIRME not in t.llamadas

    def test_aplicar_carga_partidas_y_registra_ganado(self, monkeypatch, cfg):
        conexion = _ConexionFalsa()
        monkeypatch.setattr("src.memoria.db.connection", lambda c: conexion)
        filas = []
        monkeypatch.setattr("src.mercado.repo.upsert_adjudicacion_partida", lambda conn, f: filas.append(f))
        ganados = MagicMock(return_value=1)
        monkeypatch.setattr("src.mercado.repo.registrar_ganados_portal", ganados)

        r = _sync(cfg, _TransporteSicop(), aplicar=True)

        assert r["ganados"] == 1 and r["propias"] == 1
        assert [f["proveedor_id"] for f in filas] == ["3102813498"] and filas[0]["es_propio"] is True
        ganados.assert_called_once_with(conexion, "multicomputos_cr", "sicop_cr")
        assert conexion.commits >= 2

    def test_mercado_en_firme_y_v2_con_nota_de_sicop(self, monkeypatch, cfg):
        cfg.enable_esquema_v2 = True
        monkeypatch.setattr("src.memoria.db.connection", lambda c: _ConexionFalsa())
        filas = []
        monkeypatch.setattr("src.mercado.repo.upsert_adjudicacion_partida", lambda conn, f: filas.append(f))
        monkeypatch.setattr("src.mercado.repo.registrar_ganados_portal", lambda conn, c, p: 0)
        prepoblar = MagicMock()
        monkeypatch.setattr("src.mercado.repo.prepoblar_oportunidad_estado_v2", prepoblar)
        firme = _fx("epAdjuFirmAwarded_listPage.json")
        firme["last"] = True

        r = _sync(cfg, _TransporteSicop(firme=[firme]), aplicar=True, incluir_mercado=True)

        assert r["mercado"] == 3 and r["mercado_crc"] == 244800 + 393000 + 122400
        assert sum(1 for f in filas if f["proveedor_id"] == sicop_publica.PROVEEDOR_DESCONOCIDO) == 3
        assert prepoblar.call_args.args[1:] == ("multicomputos_cr", "sicop_cr")
        assert prepoblar.call_args.kwargs["nota"] == "Resultado publicado en SICOP"

    def test_bloqueo_en_el_mercado_deja_registrado_el_ganado(self, monkeypatch, cfg):
        monkeypatch.setattr("src.memoria.db.connection", lambda c: _ConexionFalsa())
        monkeypatch.setattr("src.mercado.repo.upsert_adjudicacion_partida", lambda conn, f: None)
        ganados = MagicMock(return_value=1)
        monkeypatch.setattr("src.mercado.repo.registrar_ganados_portal", ganados)

        with pytest.raises(BloqueoAcceso):
            _sync(cfg, _TransporteSicop(firme=[BloqueoAcceso("sicop_cr", "waf")]), aplicar=True,
                  incluir_mercado=True)
        ganados.assert_called_once()

    def test_portal_ajeno_es_error(self, cfg):
        with pytest.raises(ValueError):
            asyncio.run(service.sincronizar_sicop(cfg, None, desde=date(2026, 1, 1), hasta=date(2026, 2, 1),
                                                  portal_id="comprasal_sv"))
