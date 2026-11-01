"""SQL y binds de `src.aprendizaje.repo` contra una conexión mock (el dialecto real lo valida
tests/integration/test_oracle_local.py en el job de Oracle 23ai Free)."""
from __future__ import annotations

import json
import re
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.aprendizaje import repo
from src.identidad import repo as identidad_repo
from src.identidad.politica import AccionNoAuditable

_RAIZ = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _acciones(monkeypatch):
    """Hasta que se integre la SOLICITUD sobre `politica.py`, las acciones del módulo se agregan acá."""
    monkeypatch.setattr(identidad_repo, "ACCIONES_AUDITADAS", identidad_repo.ACCIONES_AUDITADAS | repo.ACCIONES)


@pytest.fixture
def cursor():
    c = MagicMock()
    c.var.return_value.getvalue.return_value = [41]
    return c


@pytest.fixture
def conn(cursor):
    c = MagicMock()
    c.cursor.return_value = cursor
    return c


def _sqls(cursor) -> list[str]:
    return [" ".join(c.args[0].split()) for c in cursor.execute.call_args_list]


def _auditorias(cursor) -> list[dict]:
    return [c.args[1] for c in cursor.execute.call_args_list if "INSERT INTO auditoria" in c.args[0]]


# ── DDL ────────────────────────────────────────────────────────────────────

class TestDDL:
    ddl = (_RAIZ / "db" / "schema_v2.sql").read_text(encoding="utf-8")

    @pytest.mark.parametrize("tabla", repo.TABLAS)
    def test_cada_tabla_es_idempotente(self, tabla):
        bloque = re.search(rf"CREATE TABLE {tabla} \((.*?)\n    \)\n  \]';\nEXCEPTION WHEN OTHERS THEN\n"
                           r"  IF SQLCODE != -955 THEN RAISE; END IF;", self.ddl, re.S)
        assert bloque, f"{tabla}: falta o no traga ORA-00955"

    def test_los_check_coinciden_con_las_constantes_del_repo(self):
        for valores in (repo.ETIQUETAS, repo.FUENTES, repo.SPLITS, repo.TIPOS_TERMINO, repo.VARIANTES):
            for v in valores:
                assert f"'{v}'" in self.ddl

    def test_sin_indices_vectoriales(self):
        seccion = self.ddl.split("7) Memoria de aprendizaje")[1].split("Fin del DDL")[0]
        assert "VECTOR INDEX" not in seccion.upper()

    def test_una_sola_calibracion_activa_por_cliente_y_marca(self):
        assert "CREATE UNIQUE INDEX ux_marca_calibracion_activa" in self.ddl


# ── ejemplo_marca ──────────────────────────────────────────────────────────

class TestAgregarEjemplo:
    def test_calcula_el_embedding_dentro_de_la_base(self, conn, cursor):
        r = repo.agregar_ejemplo(conn, "cli", "oracle", "positivo", "humana", "  Licencias Oracle Database  ")
        assert r == {"ejemplo_id": 41, "nuevo": True, "embedding": True}
        sql, binds = cursor.execute.call_args.args
        assert "VECTOR_EMBEDDING(MULTILINGUAL_E5_SMALL USING :titulo AS data)" in sql
        assert binds["titulo"] == "Licencias Oracle Database" and binds["proceso_gid"] is None
        assert "RETURNING ejemplo_id INTO :ejemplo_id" in sql

    def test_sin_modelo_onnx_inserta_con_embedding_null(self, conn, cursor, caplog):
        cursor.execute.side_effect = [RuntimeError("ORA-40284: model does not exist"), None]
        r = repo.agregar_ejemplo(conn, "cli", "oracle", "negativo", "voto_usuario", "Cognos Analytics")
        assert r["embedding"] is False and r["nuevo"] is True
        segunda = _sqls(cursor)[1]
        assert "VECTOR_EMBEDDING" not in segunda and ":titulo, NULL, :split" in segunda
        assert "embedding NULL" in caplog.text

    def test_sin_calcular_embedding_no_toca_vector_embedding(self, conn, cursor):
        repo.agregar_ejemplo(conn, "cli", "oracle", "ajena", "semilla", "Power BI", calcular_embedding=False)
        assert "VECTOR_EMBEDDING" not in _sqls(cursor)[0]

    def test_idempotente_con_proceso(self, conn, cursor):
        cursor.fetchone.return_value = (7,)
        r = repo.agregar_ejemplo(conn, "cli", "oracle", "positivo", "pliego", "Exadata", proceso_gid="sv:1")
        assert r == {"ejemplo_id": 7, "nuevo": False, "embedding": None}
        assert len(_sqls(cursor)) == 1 and _sqls(cursor)[0].startswith("SELECT ejemplo_id FROM ejemplo_marca")

    def test_recorta_por_bytes_no_por_caracteres(self, conn, cursor):
        repo.agregar_ejemplo(conn, "cli", "oracle", "positivo", "humana", "ñ" * 900, evidencia="á" * 600)
        binds = cursor.execute.call_args.args[1]
        assert len(binds["titulo"].encode("utf-8")) <= 1000 and len(binds["evidencia"].encode("utf-8")) <= 1000

    @pytest.mark.parametrize("campo,valor", [("etiqueta", "quizas"), ("fuente", "rumor"), ("split", "x")])
    def test_rechaza_valores_fuera_del_check(self, conn, campo, valor):
        args = {"etiqueta": "positivo", "fuente": "humana", "split": "entrenamiento", campo: valor}
        with pytest.raises(ValueError):
            repo.agregar_ejemplo(conn, "cli", "oracle", args["etiqueta"], args["fuente"], "t", split=args["split"])

    def test_nombre_de_modelo_inyectado_se_rechaza(self, conn, cursor):
        with pytest.raises(ValueError):
            repo.agregar_ejemplo(conn, "cli", "oracle", "positivo", "humana", "t", modelo="X; DROP TABLE y--")
        cursor.execute.assert_not_called()


def test_listar_ejemplos_filtra_con_binds(conn, cursor):
    cursor.fetchall.return_value = [(1, None, "oracle", "positivo", "humana", 1, None, None, "t", "prueba", 1,
                                     "cli:x", None, 0)]
    filas = repo.listar_ejemplos(conn, "cli", "oracle", split="prueba")
    sql, binds = cursor.execute.call_args.args
    assert "marca_id = :marca_id" in sql and "split = :split" in sql and "activo = 1" in sql
    assert binds == {"cliente_id": "cli", "marca_id": "oracle", "split": "prueba", "limite": 500}
    assert filas[0]["tiene_embedding"] is False and filas[0]["split"] == "prueba"


def test_desactivar_ejemplo_protege_lo_humano_si_se_pide(conn, cursor):
    cursor.rowcount = 0
    assert repo.desactivar_ejemplo(conn, 5, proteger_humanos=True) is False
    sql = _sqls(cursor)[0]
    assert "fuente != 'humana'" in sql and "split != 'critico'" in sql and sql.startswith("UPDATE")


# ── marca_termino ──────────────────────────────────────────────────────────

def test_listar_terminos_activos_solo_semilla_y_activo(conn, cursor):
    cursor.fetchall.return_value = [(1, "oracle", "Exadata", "fuerte", "semilla", 0, None, "semilla", 1, 1)]
    t = repo.listar_terminos_activos(conn, "cli", "oracle")
    sql, binds = cursor.execute.call_args.args
    assert "activo = 1" in sql and "estado IN ('semilla','activo')" in sql
    assert binds == {"cliente_id": "cli", "marca_id": "oracle"}
    assert t == [{"termino_id": 1, "marca_id": "oracle", "termino": "Exadata", "tipo": "fuerte",
                  "estado": "semilla", "soporte": 0, "precision_est": None, "origen": "semilla",
                  "version": 1, "activo": True}]


class TestSembrar:
    def test_merge_solo_inserta_y_audita_una_vez(self, conn, cursor):
        cursor.rowcount = 2
        cursor.fetchone.return_value = ("org1",)
        n = repo.sembrar_terminos(conn, "cli", [("oracle", " Exadata ", "fuerte"), ("ibm", "Cognos", "ajena")],
                                  creado_por="cli:yo")
        assert n == 2
        sql, lote = cursor.executemany.call_args.args
        assert "WHEN NOT MATCHED THEN" in sql and "WHEN MATCHED" not in sql.replace("WHEN NOT MATCHED", "")
        assert lote[0]["termino"] == "Exadata"
        (aud,) = _auditorias(cursor)
        assert aud["accion"] == "TERMINOS_SEMBRADOS" and aud["org_id"] == "org1" and aud["usuario_id"] == "cli:yo"

    def test_sin_inserciones_no_audita(self, conn, cursor):
        cursor.rowcount = 0
        assert repo.sembrar_terminos(conn, "cli", [("oracle", "Exadata", "fuerte")]) == 0
        assert _auditorias(cursor) == []

    def test_tipo_invalido(self, conn):
        with pytest.raises(ValueError):
            repo.sembrar_terminos(conn, "cli", [("oracle", "Exadata", "fortisimo")])

    def test_sin_accion_auditable_no_escribe_nada(self, conn, cursor, monkeypatch):
        monkeypatch.setattr(identidad_repo, "ACCIONES_AUDITADAS", frozenset())
        with pytest.raises(AccionNoAuditable):
            repo.sembrar_terminos(conn, "cli", [("oracle", "Exadata", "fuerte")])
        cursor.executemany.assert_not_called()
        cursor.execute.assert_not_called()


def test_proponer_termino_no_pisa_lo_activado_o_rechazado(conn, cursor):
    cursor.rowcount = 1
    cursor.fetchone.side_effect = [(9,), ("org1",)]
    assert repo.proponer_termino(conn, "cli", "oracle", "Autonomous", "fuerte", soporte=4, precision_est=0.9) == 9
    sql = _sqls(cursor)[0]
    assert "WHERE t.estado = 'propuesto'" in sql and "'propuesto', :origen, 0" in sql
    assert _auditorias(cursor)[0]["accion"] == "TERMINO_PROPUESTO"


def test_proponer_termino_sin_cambios_no_audita(conn, cursor):
    cursor.rowcount = 0
    assert repo.proponer_termino(conn, "cli", "oracle", "Autonomous", "fuerte") is None
    assert _auditorias(cursor) == []


class TestCambiarEstadoTermino:
    def test_activar_un_propuesto(self, conn, cursor):
        cursor.fetchone.side_effect = [("cli", "oracle", "propuesto"), ("org1",)]
        cursor.rowcount = 1
        assert repo.activar_termino(conn, 3, actor="u1") is True
        update = _sqls(cursor)[1]
        assert "SET estado = :estado, activo = :activo, version = version + 1" in update
        assert cursor.execute.call_args_list[1].args[1] == {"estado": "activo", "activo": 1, "termino_id": 3}
        assert _auditorias(cursor)[0]["accion"] == "TERMINO_ACTIVADO"

    def test_activar_lo_ya_activo_no_hace_nada(self, conn, cursor):
        cursor.fetchone.return_value = ("cli", "oracle", "activo")
        assert repo.activar_termino(conn, 3) is False
        assert len(_sqls(cursor)) == 1

    def test_rechazar_una_semilla(self, conn, cursor):
        cursor.fetchone.side_effect = [("cli", "oracle", "semilla"), ("org1",)]
        cursor.rowcount = 1
        assert repo.rechazar_termino(conn, 3) is True
        assert cursor.execute.call_args_list[1].args[1]["activo"] == 0
        assert _auditorias(cursor)[0]["accion"] == "TERMINO_RECHAZADO"


# ── marca_calibracion ──────────────────────────────────────────────────────

def test_calibracion_activa_prefiere_la_de_la_marca_y_cae_a_la_global(conn, cursor):
    cursor.fetchone.return_value = (3, "cli", None, 0.9, 0.5, 5, '{"humana": 3}', None, "{}", "activa", None, None)
    cal = repo.calibracion_activa(conn, "cli", "oracle")
    sql, binds = cursor.execute.call_args.args
    assert "(marca_id = :marca_id OR marca_id IS NULL)" in sql and "CASE WHEN marca_id IS NULL THEN 1" in sql
    assert binds == {"cliente_id": "cli", "marca_id": "oracle"}
    assert cal["version"] == 3 and cal["pesos_fuentes"] == {"humana": 3} and cal["umbral_confirmada"] == 0.9


def test_calibracion_activa_sin_filas(conn, cursor):
    cursor.fetchone.return_value = None
    assert repo.calibracion_activa(conn, "cli") is None


def test_registrar_candidata_json_por_bind(conn, cursor):
    cursor.fetchone.return_value = ("org1",)
    v = repo.registrar_candidata(conn, "cli", "oracle", umbral_confirmada=0.9, k=5,
                                 pesos_fuentes={"humana": 3.0}, metricas={"precision": 0.97})
    assert v == 41
    sql, binds = cursor.execute.call_args_list[0].args
    assert "JSON(:pesos_fuentes)" in sql and "'candidata'" in sql
    assert json.loads(binds["pesos_fuentes"]) == {"humana": 3.0} and binds["parametros"] is None
    assert _auditorias(cursor)[0]["accion"] == "CALIBRACION_REGISTRADA"


class TestPromover:
    def test_archiva_la_anterior_antes_de_activar(self, conn, cursor):
        cursor.fetchone.side_effect = [("cli", "oracle", "candidata"), (2,), ("org1",)]
        assert repo.promover(conn, 5, actor="ciclo") == 2
        sqls = _sqls(cursor)
        i_arch = next(i for i, s in enumerate(sqls) if "SET estado = 'archivada'" in s)
        i_act = next(i for i, s in enumerate(sqls) if "SET estado = 'activa'" in s)
        assert i_arch < i_act
        assert cursor.execute.call_args_list[i_arch].args[1] == {"version": 2}
        assert "NVL(marca_id, '*') = NVL(:marca_id, '*')" in sqls[1]
        aud = _auditorias(cursor)[0]
        assert aud["accion"] == "CALIBRACION_PROMOVIDA" and "archivada=2" in aud["recurso"]

    def test_sin_anterior(self, conn, cursor):
        cursor.fetchone.side_effect = [("cli", None, "archivada"), None, ("org1",)]
        assert repo.promover(conn, 5) is None
        assert not any("SET estado = 'archivada'" in s for s in _sqls(cursor))

    def test_ya_activa_o_inexistente(self, conn, cursor):
        cursor.fetchone.return_value = ("cli", None, "activa")
        with pytest.raises(ValueError):
            repo.promover(conn, 5)
        cursor.fetchone.return_value = None
        with pytest.raises(ValueError):
            repo.promover(conn, 6)


def test_archivar(conn, cursor):
    cursor.fetchone.side_effect = [("cli", None, "candidata"), ("org1",)]
    assert repo.archivar(conn, 4) is True
    assert _auditorias(cursor)[0]["accion"] == "CALIBRACION_ARCHIVADA"
    cursor.fetchone.side_effect = [("cli", None, "archivada")]
    assert repo.archivar(conn, 4) is False


# ── decision_capa ──────────────────────────────────────────────────────────

def test_guardar_decision(conn, cursor):
    assert repo.guardar_decision(conn, "cli", "sv:1", "triaje", version="v2", salida={"go": True},
                                 score=0.8, entrada_hash="h" * 64) == 41
    sql, binds = cursor.execute.call_args.args
    assert "JSON(:salida)" in sql and "RETURNING id INTO :id" in sql
    assert json.loads(binds["salida"]) == {"go": True} and binds["variante"] == "activa"


def test_guardar_decision_salida_enorme_se_reemplaza_por_marcador(conn, cursor):
    repo.guardar_decision(conn, "cli", "sv:1", "analisis", salida={"t": "x" * 40000})
    assert json.loads(cursor.execute.call_args.args[1]["salida"])["truncado"] is True


def test_guardar_decision_variante_invalida(conn):
    with pytest.raises(ValueError):
        repo.guardar_decision(conn, "cli", "sv:1", "triaje", variante="experimento")


def test_buscar_decision_por_hash(conn, cursor):
    cursor.fetchone.return_value = (1, "cli", "sv:1", "triaje", "v2", "activa", '{"go": true}', 0.8, None,
                                    0.001, 1200, "h", None)
    d = repo.buscar_decision_por_hash(conn, "h", "triaje", version="v2")
    sql, binds = cursor.execute.call_args.args
    assert "entrada_hash = :entrada_hash" in sql and "version = :version" in sql and "FETCH FIRST 1 ROWS ONLY" in sql
    assert binds == {"entrada_hash": "h", "capa": "triaje", "variante": "activa", "version": "v2"}
    assert d["salida"] == {"go": True} and d["latencia_ms"] == 1200


def test_buscar_decision_sin_cache(conn, cursor):
    cursor.fetchone.return_value = None
    assert repo.buscar_decision_por_hash(conn, "h", "triaje") is None


def test_contar_por_tabla_con_cliente(conn, cursor):
    cursor.fetchone.return_value = (3,)
    conteos = repo.contar_por_tabla(conn, "cli")
    assert set(conteos) == set(repo.TABLAS) and all(v == 3 for v in conteos.values())
    assert "SELECT COUNT(*) FROM pagina_pliego" in _sqls(cursor)


def test_recortar_bytes():
    assert repo.recortar_bytes("añb", 2) == "a" and repo.recortar_bytes(None, 5) is None
    assert repo.recortar_bytes("abc", 10) == "abc"
