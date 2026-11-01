"""P3 · contrato de marcas: la clasificación por marca (evidencia + veredicto) sobrevive a toda la persistencia.

Sin Oracle real: conexiones y cursores simulados. El SQL contra Oracle 23ai lo prueba
`tests/integration/test_oracle_local.py::test_p3_*`. Datos sintéticos, sin nada real.
"""
import contextlib
import dataclasses
import json
import types
from datetime import datetime
from unittest.mock import MagicMock

import oracledb
import pytest

from src import history
from src.memoria import backfill_v2, repo, service
from src.scraper.models import (
    MarcaRecomendada,
    MatrizRiesgos,
    Proceso,
    ProcesoClasificado,
    ResultadoAnalisis,
    RiesgoItem,
)

CID = "cliente_prueba"


def _marca_completa(**kw) -> MarcaRecomendada:
    base = dict(marca="Oracle", razon="base de datos", role="Recomendada", score=0.92,
                evidencia="licencias Oracle Database", evidencia_valida=True, origen="diccionario",
                veredicto="confirmada", por_que="término fuerte en el título")
    base.update(kw)
    return MarcaRecomendada(**base)


def _clasificado(ref="P3-001", marcas=None, ajenas=None, **kw) -> ProcesoClasificado:
    base = dict(
        institucion="INSTITUCION DE PRUEBA", descripcion="Renovación de licencias Oracle Database",
        fecha_publicacion=datetime(2026, 10, 30, 8, 0), url_directa="https://example.com/p3",
        numero_referencia=ref, forma_contratacion="Contratación Directa", tipo_actividad="Software",
        es_relevante=True, categoria_detectada="software", razon="encaja", match_tecnico=0.8,
        match_categoria=0.7, factibilidad=0.6, confianza=0.6, tags=["Bases de datos"],
        marcas_recomendadas=[_marca_completa()] if marcas is None else marcas,
        marcas_ajenas=[{"marca": "IBM", "evidencia": "Cognos"}] if ajenas is None else ajenas,
    )
    base.update(kw)
    return ProcesoClasificado(**base)


# ── models: MarcaRecomendada ──────────────────────────────────────────────────

class TestMarcaRecomendada:
    def test_fila_vieja_queda_con_los_defaults(self):
        m = MarcaRecomendada.desde_dict({"marca": "Oracle", "role": "Alternativa"})
        assert m == MarcaRecomendada(marca="Oracle", role="Alternativa")
        assert (m.score, m.evidencia, m.evidencia_valida, m.origen, m.veredicto, m.por_que) == (
            None, "", None, "", "", "")

    def test_sin_role_es_alternativa(self):
        assert MarcaRecomendada.desde_dict({"marca": "HPE", "razon": "r"}).role == "Alternativa"

    def test_claves_desconocidas_se_ignoran(self):
        m = MarcaRecomendada.desde_dict({"marca": "Oracle", "campo_del_futuro": 1, "veredicto": "por_confirmar"})
        assert m.marca == "Oracle" and m.veredicto == "por_confirmar"

    def test_tipos_raros_no_rompen(self):
        m = MarcaRecomendada.desde_dict({"marca": None, "score": "no-numero", "evidencia_valida": "sí"})
        assert m.marca == "" and m.score is None and m.evidencia_valida is None
        assert MarcaRecomendada.desde_dict({"score": True}).score is None  # un bool no es un score
        assert MarcaRecomendada.desde_dict({"score": "0.5"}).score == 0.5
        assert MarcaRecomendada.desde_dict("Oracle").marca == "Oracle"
        assert MarcaRecomendada.desde_dict(None) == MarcaRecomendada()

    def test_ida_y_vuelta_con_asdict(self):
        m = _marca_completa()
        assert MarcaRecomendada.desde_dict(dataclasses.asdict(m)) == m

    def test_proceso_clasificado_trae_marcas_ajenas_vacias_por_default(self):
        p = ProcesoClasificado.from_proceso(Proceso("I", "D", datetime(2026, 1, 1), "u"))
        assert p.marcas_ajenas == [] and p.marcas_recomendadas == []


# ── models: ResultadoAnalisis ─────────────────────────────────────────────────

class TestResultadoAnalisisMarca:
    def _res(self):
        return ResultadoAnalisis(
            proceso_uuid="P3-001", estado="ANALIZADO", triaje="GO",
            matriz=MatrizRiesgos(items=[RiesgoItem("legal", "carta del fabricante", "alta", "4.2")]),
            competible=False, motivo_bloqueo="proveedor_unico",
            marca_exigida={"marca": "IBM", "cita": "licencias IBM Cognos", "pagina": 12},
            proveedor_unico=True, carta_fabricante=True, equivalente_admitido=False,
            marcas_involucradas=[{"marca": "IBM", "cita": "IBM Cognos Analytics", "pagina": 12,
                                  "fuente_texto": "capa_texto"}],
        )

    def test_ida_y_vuelta(self):
        r = self._res()
        assert ResultadoAnalisis.desde_dict(json.loads(json.dumps(dataclasses.asdict(r)))) == r

    def test_linea_vieja_queda_en_desconocido(self):
        r = ResultadoAnalisis.desde_dict({"proceso_uuid": "X", "estado": "ANALIZADO"})
        assert r.marca_exigida is None and r.proveedor_unico is None and r.carta_fabricante is None
        assert r.equivalente_admitido is None and r.marcas_involucradas == []

    def test_valores_ilegibles_se_descartan(self):
        r = ResultadoAnalisis.desde_dict({
            "proceso_uuid": "X", "estado": "ANALIZADO", "marca_exigida": {"cita": "sin marca"},
            "proveedor_unico": "sí", "marcas_involucradas": [{"marca": "IBM", "pagina": "doce", "extra": 1},
                                                             "basura", {"cita": "sin marca"}],
        })
        assert r.marca_exigida is None and r.proveedor_unico is None
        assert r.marcas_involucradas == [{"marca": "IBM", "cita": "", "pagina": None, "fuente_texto": ""}]

    def test_jsonl_de_analisis_conserva_los_campos(self, tmp_path, monkeypatch):
        monkeypatch.delenv("ESTADO_FUENTE", raising=False)
        r = self._res()
        history.save_analisis(CID, r, str(tmp_path))
        leido = history.load_analisis(CID, ["P3-001"], str(tmp_path))["P3-001"]
        assert leido.marca_exigida == r.marca_exigida
        assert leido.marcas_involucradas == r.marcas_involucradas
        assert (leido.proveedor_unico, leido.carta_fabricante, leido.equivalente_admitido) == (True, True, False)


# ── history: JSONL, manifiesto y lectura de la ADB ────────────────────────────

class TestHistorySerializacion:
    def test_ida_y_vuelta_conserva_todos_los_campos_de_marca(self):
        p = _clasificado(marcas=[_marca_completa(), _marca_completa(marca="Oracle Cloud", role="Alternativa",
                                                                   score=None, veredicto="por_confirmar")])
        d = history._serialize(p)
        assert d["marcas_recomendadas"][0]["role"] == "Recomendada"
        assert d["marcas_ajenas"] == [{"marca": "IBM", "evidencia": "Cognos"}]
        q = history._deserialize(json.loads(json.dumps(d)))
        assert q.marcas_recomendadas == p.marcas_recomendadas
        assert q.marcas_ajenas == p.marcas_ajenas

    def test_linea_vieja_sin_role_ni_ajenas(self):
        d = history._serialize(_clasificado())
        d["marcas_recomendadas"] = [{"marca": "Oracle", "razon": "r"}]
        d.pop("marcas_ajenas")
        q = history._deserialize(d)
        assert q.marcas_recomendadas == [MarcaRecomendada(marca="Oracle", razon="r")]
        assert q.marcas_ajenas == []

    def test_lineas_raras_no_rompen(self):
        d = history._serialize(_clasificado())
        d["marcas_recomendadas"] = [{"marca": "Oracle", "desconocido": [1, 2]}, 7, None, "HPE"]
        d["marcas_ajenas"] = "no-es-lista"
        q = history._deserialize(d)
        assert [m.marca for m in q.marcas_recomendadas] == ["Oracle", "HPE"]
        assert q.marcas_ajenas == []

    def test_jsonl_save_run_load_last_run(self, tmp_path, monkeypatch):
        monkeypatch.delenv("ESTADO_FUENTE", raising=False)
        p = _clasificado()
        history.save_run(CID, datetime(2026, 10, 30, 7, 0), [p], 10, str(tmp_path))
        run = history.load_last_run(CID, str(tmp_path))
        (q,) = run["procesos"]
        assert q.marcas_recomendadas == p.marcas_recomendadas and q.marcas_ajenas == p.marcas_ajenas

    def test_manifiesto_scrape_a_analyze(self):
        """Lo mismo que hacen `main._stage_scrape` (json.dump de `_serialize`) y `_stage_analyze` (`_deserialize`)."""
        clasificados = [_clasificado("P3-A"), _clasificado("P3-B", marcas=[], ajenas=[], es_relevante=False)]
        manifiesto = {"clientes": {CID: {"clasificados": [history._serialize(c) for c in clasificados]}}}
        leido = json.loads(json.dumps(manifiesto, ensure_ascii=False))
        vuelta = [history._deserialize(d) for d in leido["clientes"][CID]["clasificados"]]
        for original, q in zip(clasificados, vuelta):
            assert q.marcas_recomendadas == original.marcas_recomendadas
            assert q.marcas_ajenas == original.marcas_ajenas
        assert vuelta[1].marcas_recomendadas == []

    def test_leer_run_adb_conserva_todos_los_campos(self, monkeypatch):
        marcas = json.dumps([dataclasses.asdict(_marca_completa()), {"marca": "Oracle", "role": "Alternativa"}])
        fila = ("P3-001", "INST", "Titulo", "Directa", "Software", datetime(2026, 10, 30), "https://example.com/p",
                None, None, 1, "software", "r", 0.8, 0.7, 0.6, '["Bases de datos"]', marcas, datetime(2026, 10, 30))
        cursor = MagicMock()
        cursor.fetchall.return_value = [fila]
        cursor.fetchone.return_value = (10,)
        conn = MagicMock()
        conn.cursor.return_value = cursor
        monkeypatch.setattr(history, "_cfg", lambda: types.SimpleNamespace(adb_dsn="x", adb_password="y"))
        monkeypatch.setattr("src.memoria.db.connection", lambda cfg: contextlib.nullcontext(conn))
        run = history._leer_run_adb(CID)
        (p,) = run["procesos"]
        assert p.marcas_recomendadas[0] == _marca_completa()
        assert p.marcas_recomendadas[1] == MarcaRecomendada(marca="Oracle", role="Alternativa")


# ── repo.upsert_proceso_v2: «sin clasificación» (None) ≠ «clasificado sin marcas» ([]) ──

def _binds_cp(proceso, **kw):
    cursor = MagicMock()
    conn = MagicMock()
    conn.cursor.return_value = cursor
    repo.upsert_proceso_v2(conn, proceso, CID, **kw)
    sql, binds = cursor.execute.call_args_list[1][0]
    return sql, binds, cursor


class TestUpsertMarcas:
    def test_clasificado_sin_marcas_sobrescribe_a_vacio(self):
        sql, binds, _ = _binds_cp(_clasificado(marcas=[]))
        assert "CASE WHEN :conservar_marcas = 1" in sql
        assert binds["conservar_marcas"] == 0 and binds["marcas"] == "[]"

    def test_no_relevante_clasificado_sin_marcas_tambien_sobrescribe(self):
        _, binds, _ = _binds_cp(_clasificado(marcas=[], es_relevante=False, categoria_detectada="otro"))
        assert binds["conservar_marcas"] == 0

    def test_proceso_crudo_conserva(self):
        _, binds, _ = _binds_cp(Proceso("I", "D", datetime(2026, 1, 1), "u", numero_referencia="P3-X"))
        assert binds["conservar_marcas"] == 1

    def test_namespace_del_backfill_conserva(self):
        ns = types.SimpleNamespace(numero_referencia="P3-NS", institucion="I", descripcion="D", tipo_actividad="",
                                   forma_contratacion="", monto=None, fecha_publicacion=datetime(2026, 1, 1),
                                   fecha_limite=None, url_directa="u")
        _, binds, _ = _binds_cp(ns)
        assert binds["conservar_marcas"] == 1 and binds["marcas"] == "[]"

    def test_sin_clasificar_de_e111_conserva(self):
        crudo = ProcesoClasificado.from_proceso(Proceso("I", "D", datetime(2026, 1, 1), "u", numero_referencia="S"))
        _, binds, _ = _binds_cp(crudo)
        assert binds["conservar_marcas"] == 1

    def test_marcas_none_conserva(self):
        p = _clasificado()
        p.marcas_recomendadas = None
        _, binds, _ = _binds_cp(p)
        assert binds["conservar_marcas"] == 1 and binds["marcas"] == "[]"

    def test_forzar_conservar_o_sobrescribir(self):
        assert _binds_cp(_clasificado(marcas=[]), conservar_marcas=True)[1]["conservar_marcas"] == 1
        crudo = Proceso("I", "D", datetime(2026, 1, 1), "u", numero_referencia="F")
        assert _binds_cp(crudo, conservar_marcas=False)[1]["conservar_marcas"] == 0

    def test_lista_con_marcas_siempre_manda_y_lleva_todos_los_campos(self):
        _, binds, cursor = _binds_cp(_clasificado(), conservar_marcas=True)
        assert binds["conservar_marcas"] == 0
        (m,) = json.loads(binds["marcas"])
        assert m == dataclasses.asdict(_marca_completa())
        cursor.setinputsizes.assert_called_with(marcas=oracledb.DB_TYPE_CLOB)

    def test_sin_clasificar_de_service_usa_el_mismo_criterio(self):
        crudo = ProcesoClasificado.from_proceso(Proceso("I", "D", datetime(2026, 1, 1), "u"))
        assert service.sin_clasificar(crudo) is repo.proceso_sin_clasificar(crudo) is True
        assert service.sin_clasificar(_clasificado()) is False


# ── repo.actualizar_marcas_v2 ─────────────────────────────────────────────────

class TestActualizarMarcasV2:
    def _llamar(self, marcas, rowcount=1):
        cursor = MagicMock()
        cursor.rowcount = rowcount
        conn = MagicMock()
        conn.cursor.return_value = cursor
        n = repo.actualizar_marcas_v2(conn, CID, "comprasal_sv:P3-001", marcas)
        sql, binds = cursor.execute.call_args[0]
        return n, sql, binds, cursor

    def test_update_con_binds(self):
        n, sql, binds, cursor = self._llamar([_marca_completa()])
        assert n == 1
        assert "UPDATE cliente_proceso" in sql and "JSON(:marcas)" in sql
        assert "WHERE cliente_id = :cliente_id" in sql and "proceso_gid = :proceso_gid" in sql
        assert binds["cliente_id"] == CID and binds["proceso_gid"] == "comprasal_sv:P3-001"
        assert json.loads(binds["marcas"]) == [dataclasses.asdict(_marca_completa())]
        cursor.setinputsizes.assert_called_once_with(marcas=oracledb.DB_TYPE_CLOB)
        cursor.close.assert_called_once()

    def test_acepta_dicts_texto_json_y_lista_vacia(self):
        assert json.loads(self._llamar([{"marca": "Oracle", "veredicto": "fuera"}])[2]["marcas"]) == [
            {"marca": "Oracle", "veredicto": "fuera"}]
        assert json.loads(self._llamar('[{"marca": "HPE"}]')[2]["marcas"]) == [{"marca": "HPE"}]
        assert self._llamar([])[2]["marcas"] == "[]"
        assert self._llamar(None)[2]["marcas"] == "[]"

    def test_fila_inexistente_devuelve_cero(self):
        assert self._llamar([], rowcount=0)[0] == 0

    def test_texto_que_no_es_lista_se_rechaza(self):
        with pytest.raises(ValueError):
            self._llamar('{"marca": "Oracle"}')


# ── service.actualizar_marcas: la corrida nunca falla por la ADB ──────────────

def _cfg_v2(**kw):
    return types.SimpleNamespace(enable_memoria=True, enable_esquema_v2=True, clients_dir="", **kw)


class TestServiceActualizarMarcas:
    def test_flags_apagados_no_abren_conexion(self, monkeypatch):
        abrir = MagicMock()
        monkeypatch.setattr("src.memoria.db.connection", abrir)
        cfg = types.SimpleNamespace(enable_memoria=True, enable_esquema_v2=False)
        assert service.actualizar_marcas(cfg, CID, "P3-001", []) is False
        abrir.assert_not_called()

    def test_adb_caida_devuelve_false(self, monkeypatch):
        def _caida(cfg):
            raise oracledb.OperationalError("DPY-6005: cannot connect to database")
        monkeypatch.setattr("src.memoria.db.connection", _caida)
        monkeypatch.setattr(service, "_portal", lambda cfg, cid: "comprasal_sv")
        assert service.actualizar_marcas(_cfg_v2(), CID, "P3-001", [_marca_completa()]) is False

    def test_dsn_invalido_real_devuelve_false(self, monkeypatch):
        """El pool REAL de `src.memoria.db` contra un DSN que no escucha (nunca una contraseña mala)."""
        from src.memoria import db as memoria_db

        monkeypatch.setattr(memoria_db, "_pool", None)
        monkeypatch.setattr(service, "_portal", lambda cfg, cid: "comprasal_sv")
        cfg = _cfg_v2(adb_dsn="127.0.0.1:1/NOEXISTE", adb_user="nadie", adb_password="no-importa")
        try:
            assert service.actualizar_marcas(cfg, CID, "P3-001", []) is False
        finally:
            memoria_db.close_pool()

    def test_ok_y_fila_inexistente(self, monkeypatch):
        conn = MagicMock()
        monkeypatch.setattr("src.memoria.db.connection", lambda cfg: contextlib.nullcontext(conn))
        monkeypatch.setattr(service, "_portal", lambda cfg, cid: "comprasal_sv")
        llamadas = []

        def _repo(c, cid, gid, marcas):
            llamadas.append((cid, gid, marcas))
            return 1 if gid.endswith("P3-001") else 0
        monkeypatch.setattr(repo, "actualizar_marcas_v2", _repo)
        assert service.actualizar_marcas(_cfg_v2(), CID, "P3-001", []) is True
        assert service.actualizar_marcas(_cfg_v2(), CID, "P3-NO", []) is False
        assert llamadas[0] == (CID, "comprasal_sv:P3-001", [])


# ── llamadores: el backfill reaplica una clasificación VIEJA y no borra marcas ──

def test_backfill_clasificacion_conserva_marcas(monkeypatch, tmp_path):
    (tmp_path / "history").mkdir()
    linea = {"procesos": [history._serialize(_clasificado("P3-BF", marcas=[]))]}
    (tmp_path / "history" / f"{CID}.jsonl").write_text(json.dumps(linea) + "\n", encoding="utf-8")
    cursor = MagicMock()
    cursor.fetchall.return_value = [(CID, "P3-BF")]
    conn = MagicMock()
    conn.cursor.return_value = cursor
    upsert = MagicMock()
    monkeypatch.setattr(repo, "upsert_proceso_v2", upsert)
    assert backfill_v2.backfill_clasificacion(conn, str(tmp_path)) == 1
    assert upsert.call_args.kwargs == {"conservar_marcas": True}
