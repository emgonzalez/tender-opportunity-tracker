"""Tests de src.analytics.tablero — SQL con cursor falso (sin Oracle) y la clasificación pura."""
import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest

from src.analytics import tablero

SV = ZoneInfo("America/El_Salvador")
HOY = datetime(2026, 10, 22, 9, 0, tzinfo=SV)  # jueves


@pytest.fixture
def cursor():
    c = MagicMock()
    c.fetchone = MagicMock(return_value=(0,))
    c.fetchall = MagicMock(return_value=[])
    return c


@pytest.fixture
def conn(cursor):
    cn = MagicMock()
    cn.cursor = MagicMock(return_value=cursor)
    return cn


def _sqls(cursor):
    return [c[0][0] for c in cursor.execute.call_args_list]


def _binds(cursor):
    return [c[0][1] for c in cursor.execute.call_args_list]


def _cruda(**kw):
    """Una fila cruda de `procesos_del_rango` en el orden de `_COLUMNAS`."""
    base = dict.fromkeys(tablero._COLUMNAS)
    base.update(proceso_gid="comprasal_sv:P1", numero_referencia="P1", titulo="Servidores",
                institucion="MINSAL", url="https://x", match_tecnico=Decimal("0.9"),
                match_categoria=Decimal("0.9"), factibilidad=Decimal("0.9"), estado_cp="ANALIZADO")
    base.update(kw)
    return tuple(base[c] for c in tablero._COLUMNAS)


def _fila(**kw):
    f = {
        "proceso_gid": "g1", "numero_referencia": "P1", "titulo": "t", "tz_portal": "America/El_Salvador",
        "fecha_limite_utc": None, "modalidad_raw": "Licitación Competitiva", "match": 90,
        "match_tecnico": 0.9, "match_categoria": 0.9, "factibilidad": 0.9, "triaje": None,
        "estado_cp": "ANALIZADO", "es_relevante": None, "matriz_riesgos": None, "op_estado": None,
        "op_actualizado_en": None,
    }
    f.update(kw)
    return f


# ── privacidad y binds en todas las consultas ───────────────────────────────

CLI = "CLIENTE-XYZ"
LLAMADAS = [
    lambda cn: tablero.procesos_del_rango(cn, CLI, 30),
    lambda cn: tablero.votos_resumen(cn, CLI, 30),
    lambda cn: tablero.votos_por_proceso(cn, CLI, ["g1", "g2"]),
    lambda cn: tablero.tendencia_semanal(cn, CLI),
    lambda cn: tablero.marcas_top(cn, CLI, 30),
    lambda cn: tablero.pipeline_en_juego(cn, CLI),
    lambda cn: tablero.estancadas(cn, CLI),
]


@pytest.mark.parametrize("llamada", LLAMADAS)
def test_cliente_id_siempre_ligado_y_sin_destinatario(llamada, conn, cursor):
    cursor.fetchone.return_value = (0, 0, None)
    llamada(conn)
    assert cursor.execute.call_count >= 1
    for sql, binds in zip(_sqls(cursor), _binds(cursor)):
        assert binds["cliente_id"] == CLI
        assert ":cliente_id" in sql
        assert "destinatario" not in sql.lower()
        assert CLI not in sql  # el valor viaja como bind, nunca interpolado
    cursor.close.assert_called()


# ── procesos_del_rango ──────────────────────────────────────────────────────

class TestProcesosDelRango:
    def test_tope_300_y_truncado(self, conn, cursor):
        cursor.fetchone.return_value = (450,)
        cursor.fetchall.return_value = [_cruda(proceso_gid=f"g{i}") for i in range(300)]
        r = tablero.procesos_del_rango(conn, "cli", 90, limite=10_000)
        assert _binds(cursor)[1]["limite"] == 300
        assert r["total"] == 450 and r["truncado"] is True and len(r["filas"]) == 300

    def test_no_truncado(self, conn, cursor):
        cursor.fetchone.return_value = (1,)
        cursor.fetchall.return_value = [_cruda()]
        r = tablero.procesos_del_rango(conn, "cli", 30)
        assert r["truncado"] is False and r["total"] == 1

    def test_sql_joins_y_ventana(self, conn, cursor):
        tablero.procesos_del_rango(conn, "cli", 30)
        sql = _sqls(cursor)[1]
        for frag in ("LEFT JOIN analisis a", "LEFT JOIN oportunidad_estado e", "LEFT JOIN usuario u",
                     "p.fecha_pub_utc >= SYSDATE - :dias", "NULLS LAST", "FETCH FIRST :limite ROWS ONLY",
                     "e.estado IN ('NUEVO', 'EVALUANDO', 'OFERTADO')"):
            assert frag in sql
        # no filtra por relevancia en el WHERE: los descartados también vienen...
        assert "cp.es_relevante = 1" not in sql.split("ORDER BY")[0]
        # ...pero el tope de filas conserva primero lo relevante o con estado, luego lo que aún cierra
        orden = sql.split("ORDER BY")[1]
        assert orden.index("e.cliente_id IS NOT NULL") < orden.index("SYS_EXTRACT_UTC") < orden.index("NULLS LAST")

    def test_token_mismo_formato_que_oportunidades(self, conn, cursor):
        from src.oportunidades.repo import _TOKEN
        tablero.procesos_del_rango(conn, "cli", 30)
        assert _TOKEN in _sqls(cursor)[1]

    def test_parseo_de_fila(self, conn, cursor):
        marcas = json.dumps([{"marca": "Dell", "razon": "r", "role": "Recomendada"}, {"marca": ""}])
        matriz = json.dumps({"items": [], "resumen": "ok"})
        cursor.fetchone.return_value = (1,)
        cursor.fetchall.return_value = [_cruda(marcas=marcas, matriz_riesgos=matriz, monto_usd=Decimal("1500.50"),
                                               es_relevante=1, token="2026-10-01T00:00:00.000000",
                                               responsable_nombre="Ana")]
        f = tablero.procesos_del_rango(conn, "cli", 30)["filas"][0]
        assert f["marcas"] == ["Dell"]
        assert f["matriz_riesgos"] == {"items": [], "resumen": "ok"}
        assert f["monto_usd"] == 1500.5 and f["monto_estimado"] is None
        assert f["match"] == 90 and f["es_relevante"] is True
        assert f["token"] == "2026-10-01T00:00:00.000000" and f["responsable_nombre"] == "Ana"

    def test_json_malformado_no_rompe(self, conn, cursor):
        cursor.fetchone.return_value = (1,)
        cursor.fetchall.return_value = [_cruda(marcas="{no es json", matriz_riesgos="[1,")]
        f = tablero.procesos_del_rango(conn, "cli", 30)["filas"][0]
        assert f["marcas"] == [] and f["matriz_riesgos"] is None

    def test_json_nativo_y_lob(self, conn, cursor):
        lob = MagicMock()
        lob.read.return_value = '{"items": [], "resumen": "lob"}'
        cursor.fetchone.return_value = (1,)
        cursor.fetchall.return_value = [_cruda(marcas=[{"marca": "HPE"}], matriz_riesgos=lob)]
        f = tablero.procesos_del_rango(conn, "cli", 30)["filas"][0]
        assert f["marcas"] == ["HPE"] and f["matriz_riesgos"]["resumen"] == "lob"

    def test_fila_sin_analisis_ni_estado(self, conn, cursor):
        cursor.fetchone.return_value = (1,)
        cursor.fetchall.return_value = [_cruda()]
        f = tablero.procesos_del_rango(conn, "cli", 30)["filas"][0]
        assert f["matriz_riesgos"] is None and f["op_estado"] is None and f["token"] == ""


# ── clasificar (pura) ───────────────────────────────────────────────────────

MATRIZ_PROVEEDOR_UNICO = {
    "items": [{"categoria": "legal", "descripcion": "Contratación directa con proveedor único",
               "severidad": "alta", "clausula_ref": ""}],
    "resumen": "Contratación directa por proveedor único.",
}


class TestClasificar:
    def test_todos_los_grupos(self):
        filas = [
            _fila(proceso_gid="decidir"),
            _fila(proceso_gid="nc", modalidad_raw="Contratación Directa", matriz_riesgos=MATRIZ_PROVEEDOR_UNICO),
            _fila(proceso_gid="baja_match", match=79),
            _fila(proceso_gid="baja_triaje", triaje="NO-GO", es_relevante=True),
            _fila(proceso_gid="desc", estado_cp="DESCARTADO_REGEX", es_relevante=False),
        ]
        r = tablero.clasificar(filas, ["Dell"], HOY)
        por_gid = {f["proceso_gid"]: f for f in r["filas"]}
        assert por_gid["decidir"]["grupo"] == "decidir"
        assert por_gid["nc"]["grupo"] == "no_competible"
        assert por_gid["nc"]["competible"] is False and por_gid["nc"]["motivo_bloqueo"] == "proveedor_unico"
        assert por_gid["nc"]["motivo_bloqueo_txt"]
        assert por_gid["baja_match"]["grupo"] == "baja"
        assert por_gid["baja_triaje"]["grupo"] == "baja"
        assert por_gid["desc"]["grupo"] == "descartado"
        assert por_gid["desc"]["motivo_descarte"] == "filtro de texto"
        assert r["conteos"] == {"decidir": 1, "no_competible": 1, "baja": 2, "descartado": 1, "pendiente": 0}

    @pytest.mark.parametrize("estado_cp", ["DESCUBIERTO", "APROBADO", "VECTORIZADO", "REQUIERE_REVISION_MANUAL", "ERROR_LLM"])
    def test_lo_que_aun_esta_en_curso_o_fallo_no_es_un_descarte(self, estado_cp):
        f = _fila(proceso_gid="x", estado_cp=estado_cp, es_relevante=None, triaje=None)
        r = tablero.clasificar([f], [], HOY)
        assert r["filas"][0]["grupo"] == "pendiente" and r["filas"][0]["motivo_descarte"] is None
        assert r["conteos"]["descartado"] == 0

    def test_el_portal_lee_estado_cierre_y_monto_de_la_fila_real(self):
        f = _fila(proceso_gid="x", op_estado="OFERTADO", monto_usd=10.0, monto_estimado=25.0,
                  fecha_limite_utc=datetime(2026, 11, 5, 12, 0), op_actualizado_en=datetime(2026, 10, 20, 12, 0))
        a = tablero.clasificar([f], [], HOY)["filas"][0]
        assert a["estado"] == "OFERTADO" and a["monto_usd"] == 25.0  # el análisis manda sobre el monto del portal
        assert a["fecha_limite"] is not None and a["actualizado_en"] is not None

    def test_sin_analisis_competible_none_no_bloquea(self):
        f = tablero.clasificar([_fila()], None, HOY)["filas"][0]
        assert f["competible"] is None and f["grupo"] == "decidir"

    def test_recuperado_no_es_descartado(self):
        f = tablero.clasificar([_fila(estado_cp="DESCARTADO_REGEX", es_relevante=False, op_estado="NUEVO",
                                      match=85)], None, HOY)["filas"][0]
        assert f["grupo"] == "decidir" and f["recuperado"] is True and f["motivo_descarte"] is None

    @pytest.mark.parametrize("estado,triaje,motivo", [
        ("DESCARTADO_REGEX", None, "filtro de texto"),
        ("DESCARTADO_TAMANO", None, "pliego demasiado grande"),
        ("DESCARTADO_TRIAJE", None, "triaje NO-GO"),
        ("DESCUBIERTO", "NO-GO", "triaje NO-GO"),
        ("DESCUBIERTO", None, "fuera del portafolio"),
    ])
    def test_motivos_de_descarte(self, estado, triaje, motivo):
        f = tablero.clasificar([_fila(estado_cp=estado, triaje=triaje, es_relevante=0)], None, HOY)["filas"][0]
        assert f["grupo"] == "descartado" and f["motivo_descarte"] == motivo

    @pytest.mark.parametrize("campos", [
        {"es_relevante": 1}, {"triaje": "GO"}, {"estado_cp": "NOTIFICADO"}, {"estado_cp": "ANALIZADO"},
    ])
    def test_relevancia_igual_que_sql_es_relevante(self, campos):
        base = {"estado_cp": "DESCUBIERTO", "es_relevante": 0}
        base.update(campos)
        assert tablero.clasificar([_fila(**base)], None, HOY)["filas"][0]["grupo"] != "descartado"

    def test_dias_y_urgencia_en_zona_del_portal(self):
        # 23 oct 03:00 UTC = 22 oct 21:00 en El Salvador → cierra HOY, no mañana.
        limite = datetime(2026, 10, 23, 3, 0)
        f = tablero.clasificar([_fila(fecha_limite_utc=limite)], None, HOY)["filas"][0]
        assert f["dias_restantes"] == 0 and f["urgencia"] == "rojo"
        assert f["cierre_texto"].startswith("Cierra hoy")

    def test_tz_invalido_usa_defecto(self):
        limite = datetime(2026, 10, 23, 3, 0)
        f = tablero.clasificar([_fila(fecha_limite_utc=limite, tz_portal="Marte/Olympus")], None, HOY)["filas"][0]
        assert f["dias_restantes"] == 0

    def test_vencida_y_sin_fecha(self):
        r = tablero.clasificar([
            _fila(proceso_gid="v", fecha_limite_utc=datetime(2026, 10, 1, 12, 0)),
            _fila(proceso_gid="s"),
            _fila(proceso_gid="ok", fecha_limite_utc=datetime(2026, 11, 30, 12, 0)),
        ], None, HOY)
        por = {f["proceso_gid"]: f for f in r["filas"]}
        assert por["v"]["vencida"] is True and por["v"]["urgencia"] is None
        assert por["s"]["dias_restantes"] is None and por["s"]["urgencia"] is None
        assert por["ok"]["urgencia"] == "verde"
        assert [f["proceso_gid"] for f in r["grupos"]["decidir"]] == ["ok", "v", "s"]

    def test_matriz_rara_no_rompe(self):
        f = tablero.clasificar([_fila(matriz_riesgos={"items": "x"})], None, HOY)["filas"][0]
        assert f["grupo"] == "decidir"

    def test_dias_en_estado(self):
        f = tablero.clasificar([_fila(op_estado="EVALUANDO", op_actualizado_en=datetime(2026, 10, 12, 15, 0))],
                               None, HOY)["filas"][0]
        assert f["dias_en_estado"] == 10

    def test_no_muta_la_entrada(self):
        fila = _fila()
        tablero.clasificar([fila], None, HOY)
        assert "grupo" not in fila


# ── votos ───────────────────────────────────────────────────────────────────

class TestVotos:
    def test_resumen(self, conn, cursor):
        cursor.fetchone.return_value = (3, 2)
        cursor.fetchall.side_effect = [[("precio", 2), ("marca", 1)], [("marca", 1), (None, 4)]]
        r = tablero.votos_resumen(conn, "cli", 30)
        assert r == {"relevante": 3, "irrelevante": 2, "motivos": [("marca", 2), ("precio", 2)]}
        assert "JSON_TABLE" in _sqls(cursor)[1]
        assert all(b["dias"] == 30 for b in _binds(cursor))

    def test_resumen_vacio(self, conn, cursor):
        cursor.fetchone.return_value = (None, None)
        r = tablero.votos_resumen(conn, "cli", 30)
        assert r == {"relevante": 0, "irrelevante": 0, "motivos": []}

    def test_por_proceso(self, conn, cursor):
        cursor.fetchall.return_value = [
            ("g1", "RELEVANTE", None, None),
            ("g1", "IRRELEVANTE", '["precio","marca"]', "precio"),
            ("g1", "IRRELEVANTE", "{malo", "plazo"),
            ("g2", "IRRELEVANTE", ["marca"], None),
        ]
        r = tablero.votos_por_proceso(conn, "cli", ["g1", "g2", "g1"])
        assert r["g1"] == {"relevante": 1, "irrelevante": 2, "motivos": ["precio", "marca", "plazo"]}
        assert r["g2"]["motivos"] == ["marca"]
        sql, binds = _sqls(cursor)[0], _binds(cursor)[0]
        assert ":g0, :g1" in sql and ":g2" not in sql
        assert binds == {"g0": "g1", "g1": "g2", "cliente_id": "cli"}

    def test_por_proceso_vacio_no_consulta(self, conn, cursor):
        assert tablero.votos_por_proceso(conn, "cli", []) == {}
        cursor.execute.assert_not_called()

    def test_por_proceso_tope_300(self, conn, cursor):
        tablero.votos_por_proceso(conn, "cli", [f"g{i}" for i in range(500)])
        binds = _binds(cursor)[0]
        assert len(binds) == 301 and "g299" in binds and "g300" not in binds

    def test_por_proceso_inyeccion_viaja_como_bind(self, conn, cursor):
        malo = "x') OR 1=1 --"
        tablero.votos_por_proceso(conn, "cli", [malo])
        assert malo not in _sqls(cursor)[0] and _binds(cursor)[0]["g0"] == malo


# ── tendencia, marcas, pipeline, estancadas ─────────────────────────────────

def test_tendencia_semanal(conn, cursor):
    cursor.fetchall.return_value = [(datetime(2026, 10, 12), 10, 3), (datetime(2026, 10, 19), Decimal(0), None)]
    r = tablero.tendencia_semanal(conn, "cli", semanas=2)
    assert r == [{"semana": date(2026, 10, 12), "revisados": 10, "relevantes": 3},
                 {"semana": date(2026, 10, 19), "revisados": 0, "relevantes": 0}]
    sql = _sqls(cursor)[0]
    assert "TRUNC(p.fecha_pub_utc, 'IW')" in sql and "cp.estado IN ('ANALIZADO', 'NOTIFICADO')" in sql
    assert _binds(cursor)[0]["semanas"] == 2


def test_marcas_top(conn, cursor):
    cursor.fetchall.return_value = [("Dell", 4), ("HPE", Decimal(2)), (None, 9)]
    assert tablero.marcas_top(conn, "cli", 30, limite=5) == [{"marca": "Dell", "conteo": 4},
                                                              {"marca": "HPE", "conteo": 2}]
    sql = _sqls(cursor)[0]
    assert "JSON_TABLE" in sql and "'$.marca'" in sql and "cp.es_relevante = 1" in sql
    assert _binds(cursor)[0]["limite"] == 5


class TestPipeline:
    def test_monto_nulo_no_es_cero(self, conn, cursor):
        cursor.fetchone.return_value = (3, 0, None)
        assert tablero.pipeline_en_juego(conn, "cli") == {"monto_usd": None, "activas": 3, "con_monto": 0}

    def test_con_monto(self, conn, cursor):
        cursor.fetchone.return_value = (3, 2, Decimal("25000.00"))
        r = tablero.pipeline_en_juego(conn, "cli")
        assert r == {"monto_usd": 25000.0, "activas": 3, "con_monto": 2}
        sql = _sqls(cursor)[0]
        assert "COALESCE(a.monto_estimado, p.monto_usd)" in sql
        assert "('NUEVO', 'EVALUANDO', 'OFERTADO')" in sql

    def test_sin_oportunidades(self, conn, cursor):
        cursor.fetchone.return_value = (0, 0, None)
        assert tablero.pipeline_en_juego(conn, "cli")["activas"] == 0


def test_estancadas(conn, cursor):
    cursor.fetchone.return_value = (4,)
    assert tablero.estancadas(conn, "cli", dias_sin_mover=10) == 4
    assert _binds(cursor)[0] == {"cliente_id": "cli", "dias": 10}
    assert "'EVALUANDO'" in _sqls(cursor)[0]


def test_utc_naive_se_interpreta_como_utc():
    assert tablero._utc(datetime(2026, 1, 1)).tzinfo == timezone.utc
    assert tablero._utc(None) is None
    aware = datetime(2026, 1, 1, tzinfo=timezone(timedelta(hours=-6)))
    assert tablero._utc(aware) is aware
