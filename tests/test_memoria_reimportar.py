"""Tests de src.memoria.reimportar — recuperación ante desastre (JSONL → ADB).

Nunca llama al LLM: todo lo que reimporta ya estaba calculado. Estos tests
cubren, en orden de importancia:

1. Que la deserialización de `.analisis.jsonl` sea fiel al `dataclasses.asdict`
   real que escribe `history.save_analisis` (round-trip).
2. La deduplicación "última aparición gana" en ambos archivos.
3. Que `reimportar_cliente` llame a los mismos escritores que ya usa el
   pipeline en vivo (`memoria.repo`), no una copia paralela del SQL.
4. Que un huérfano (análisis sin clasificación) se salte con warning, no
   rompa la corrida — evita el `ORA-02291` de la FK real.
"""
import dataclasses
import types
from datetime import datetime, timezone
from unittest.mock import MagicMock

from src import history
from src.memoria import reimportar
from src.scraper.models import (
    MatrizRiesgos,
    Precedente,
    ProcesoClasificado,
    ResultadoAnalisis,
    RiesgoItem,
    UsoModelo,
)


def _proceso(numero_referencia="REF-001", **overrides):
    base = dict(
        institucion="Ministerio X",
        descripcion="Adquisición de servidores",
        fecha_publicacion=datetime.now(timezone.utc),
        url_directa="https://comprasal/pac/1",
        numero_referencia=numero_referencia,
        es_relevante=True,
        monto=45000.5,
    )
    base.update(overrides)
    return ProcesoClasificado(**base)


def _resultado(proceso_uuid="REF-001", **overrides):
    base = dict(
        proceso_uuid=proceso_uuid,
        estado="ANALIZADO",
        formato="NATIVO",
        triaje="GO",
        triaje_confianza=0.9,
        matriz=MatrizRiesgos(items=[RiesgoItem("financiero", "multa", "alta", "5.1")], resumen="ok"),
        modelo_analisis="test-model",
        monto_estimado=45000.5,
        uso_analisis=UsoModelo(modelo="test-model", input_tokens=100, output_tokens=20, llamadas=1),
        precedentes=[Precedente(proceso_id="REF-000", institucion="X", distancia=0.1)],
    )
    base.update(overrides)
    return ResultadoAnalisis(**base)


class TestDeserializarResultadoAnalisis:
    def test_round_trip_fiel_al_asdict_real(self):
        original = _resultado()
        crudo = dataclasses.asdict(original)
        reconstruido = reimportar._deserializar_resultado_analisis(crudo)

        assert reconstruido.proceso_uuid == original.proceso_uuid
        assert reconstruido.estado == original.estado
        assert reconstruido.monto_estimado == original.monto_estimado
        assert reconstruido.matriz.items[0].categoria == "financiero"
        assert reconstruido.matriz.resumen == "ok"
        assert reconstruido.uso_analisis.input_tokens == 100
        assert reconstruido.precedentes[0].proceso_id == "REF-000"

    def test_tolera_claves_faltantes_de_lineas_viejas(self):
        """`monto_estimado` no existía antes de la Fase 0; una línea vieja no lo trae."""
        crudo = dataclasses.asdict(_resultado())
        del crudo["monto_estimado"]
        reconstruido = reimportar._deserializar_resultado_analisis(crudo)
        assert reconstruido.monto_estimado is None

    def test_sin_matriz_ni_uso_no_revienta(self):
        crudo = dataclasses.asdict(_resultado(matriz=None, uso_analisis=None, uso_triaje=None))
        reconstruido = reimportar._deserializar_resultado_analisis(crudo)
        assert reconstruido.matriz is None
        assert reconstruido.uso_analisis is None


class TestLeerClasificadosYAnalisis:
    def test_deduplica_por_ultima_aparicion(self, tmp_path):
        data_dir = str(tmp_path)
        history.save_run("cli", datetime.now(timezone.utc), [_proceso(monto=100.0)], 1, data_dir)
        history.save_run("cli", datetime.now(timezone.utc), [_proceso(monto=200.0)], 1, data_dir)

        clasificados = reimportar.leer_clasificados("cli", data_dir)
        assert len(clasificados) == 1
        assert clasificados["REF-001"].monto == 200.0

    def test_analisis_deduplica_por_ultima_aparicion(self, tmp_path):
        data_dir = str(tmp_path)
        history.save_analisis("cli", _resultado(estado="REQUIERE_REVISION_MANUAL"), data_dir)
        history.save_analisis("cli", _resultado(estado="ANALIZADO"), data_dir)

        analisis = reimportar.leer_analisis("cli", data_dir)
        assert len(analisis) == 1
        assert analisis["REF-001"].estado == "ANALIZADO"

    def test_archivo_ausente_devuelve_vacio(self, tmp_path):
        assert reimportar.leer_clasificados("cli-sin-historial", str(tmp_path)) == {}
        assert reimportar.leer_analisis("cli-sin-historial", str(tmp_path)) == {}

    def test_linea_corrupta_se_salta_sin_romper_las_demas(self, tmp_path):
        data_dir = str(tmp_path)
        history.save_run("cli", datetime.now(timezone.utc), [_proceso()], 1, data_dir)
        path = history._history_path("cli", data_dir)
        with open(path, "a", encoding="utf-8") as f:
            f.write("esto no es JSON válido\n")

        clasificados = reimportar.leer_clasificados("cli", data_dir)
        assert len(clasificados) == 1


class TestReimportarCliente:
    def _cfg(self, tmp_path):
        return types.SimpleNamespace(data_dir=str(tmp_path))

    def test_dry_run_solo_cuenta(self, tmp_path, monkeypatch):
        data_dir = str(tmp_path)
        history.save_run("cli", datetime.now(timezone.utc), [_proceso()], 1, data_dir)
        history.save_analisis("cli", _resultado(), data_dir)

        llamado = []
        monkeypatch.setattr("src.memoria.db.connection", lambda c: llamado.append(1))

        conteos = reimportar.reimportar_cliente(self._cfg(tmp_path), "cli", dry_run=True)

        assert conteos == {"clasificados": 1, "analisis": 1, "huerfanos": 0}
        assert llamado == [], "dry-run no debe abrir conexión a la ADB"

    def test_upsertea_clasificados_y_analisis_via_memoria_repo(self, tmp_path, monkeypatch):
        """No una copia paralela del SQL: los mismos escritores que usa el pipeline."""
        data_dir = str(tmp_path)
        history.save_run("cli", datetime.now(timezone.utc), [_proceso()], 1, data_dir)
        history.save_analisis("cli", _resultado(), data_dir)

        class _Conexion:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        monkeypatch.setattr("src.memoria.db.connection", lambda c: _Conexion())

        llamadas_upsert = []
        llamadas_set_estado = []
        llamadas_analisis = []
        monkeypatch.setattr(
            "src.memoria.repo.upsert_proceso",
            lambda conn, proceso, client_id, estado="DESCUBIERTO":
                llamadas_upsert.append((proceso.numero_referencia, client_id, estado)),
        )
        monkeypatch.setattr(
            "src.memoria.repo.set_estado",
            lambda conn, proceso_id, estado, **kw: llamadas_set_estado.append((proceso_id, estado, kw)),
        )
        monkeypatch.setattr(
            "src.memoria.repo.save_analisis_db",
            lambda conn, resultado: llamadas_analisis.append(resultado.proceso_uuid),
        )

        conteos = reimportar.reimportar_cliente(self._cfg(tmp_path), "cli")

        assert conteos == {"clasificados": 1, "analisis": 1, "huerfanos": 0}
        assert llamadas_upsert == [("REF-001", "cli", "ANALIZADO")], (
            "el estado inicial del upsert debe salir del análisis, no quedar en DESCUBIERTO"
        )
        assert llamadas_set_estado[0][0] == "REF-001"
        assert llamadas_set_estado[0][2]["triaje"] == "GO"
        assert llamadas_analisis == ["REF-001"]

    def test_sin_analisis_el_estado_inicial_es_descubierto(self, tmp_path, monkeypatch):
        data_dir = str(tmp_path)
        history.save_run("cli", datetime.now(timezone.utc), [_proceso(numero_referencia="REF-SOLO")], 1, data_dir)

        class _Conexion:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        monkeypatch.setattr("src.memoria.db.connection", lambda c: _Conexion())
        llamadas = []
        monkeypatch.setattr(
            "src.memoria.repo.upsert_proceso",
            lambda conn, proceso, client_id, estado="DESCUBIERTO": llamadas.append(estado),
        )

        reimportar.reimportar_cliente(self._cfg(tmp_path), "cli")
        assert llamadas == ["DESCUBIERTO"]

    def test_huerfano_se_salta_sin_llamar_a_save_analisis_db(self, tmp_path, monkeypatch):
        """Un análisis sin clasificación correspondiente no debe intentar
        insertarse: violaría la FK real de licitaciones_analisis."""
        data_dir = str(tmp_path)
        history.save_analisis("cli", _resultado(proceso_uuid="REF-HUERFANO"), data_dir)

        class _Conexion:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        monkeypatch.setattr("src.memoria.db.connection", lambda c: _Conexion())
        llamado = []
        monkeypatch.setattr(
            "src.memoria.repo.save_analisis_db", lambda conn, r: llamado.append(r.proceso_uuid)
        )
        monkeypatch.setattr("src.memoria.repo.set_estado", MagicMock())

        conteos = reimportar.reimportar_cliente(self._cfg(tmp_path), "cli")

        assert conteos == {"clasificados": 0, "analisis": 0, "huerfanos": 1}
        assert llamado == []


class TestReimportarTodos:
    def test_un_cliente_roto_no_frena_a_los_demas(self, tmp_path, monkeypatch):
        cfg = types.SimpleNamespace(data_dir=str(tmp_path))

        def fake(cfg_, client_id, dry_run=False):
            if client_id == "roto":
                raise RuntimeError("boom")
            return {"clasificados": 1, "analisis": 1, "huerfanos": 0}

        monkeypatch.setattr(reimportar, "reimportar_cliente", fake)

        resultado = reimportar.reimportar_todos(cfg, ["roto", "sano"])

        assert "error" in resultado["roto"]
        assert resultado["sano"] == {"clasificados": 1, "analisis": 1, "huerfanos": 0}
