"""Tests de src.memoria.fx con SQL simulado (sin Oracle)."""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from src.memoria import fx

HOY = date(2026, 11, 24)


class Cursor:
    def __init__(self, conn):
        self.conn = conn
        self._fila = None
        self.rowcount = 0

    def execute(self, sql, params=None):
        self.conn.sentencias.append((sql, params or {}))
        self._fila = None
        for clave, valor in self.conn.respuestas.items():
            if clave in sql:
                self._fila = valor
                break
        if sql.lstrip().upper().startswith(("UPDATE", "MERGE")):
            self.rowcount = 1

    def fetchone(self):
        return self._fila

    def fetchall(self):
        return []

    def close(self):
        pass


class Conn:
    def __init__(self, respuestas=None):
        self.sentencias = []
        self.respuestas = respuestas or {}
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return Cursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def escrituras(self):
        return [s for s, _ in self.sentencias if s.lstrip().upper().startswith(("UPDATE", "MERGE", "INSERT"))]


# ── validaciones ────────────────────────────────────────────────────────────

def test_tasa_es_inversa_a_seis_decimales():
    t = fx.construir_tasa("crc", "2026-11", "525.30", "BCCR", HOY)
    assert t.moneda == "CRC" and t.mes == date(2026, 11, 1)
    assert t.tasa_a_usd == Decimal("0.001904")


@pytest.mark.parametrize("moneda,mes,valor", [
    ("CRC", "2026-12", "500"),    # mes futuro
    ("CRC", "2026-13", "500"),    # mes inválido
    ("CRC", "nov-2026", "500"),
    ("CRC", "2026-11", "0"),
    ("CRC", "2026-11", "-5"),
    ("CRC", "2026-11", "abc"),
    ("CRC", "2026-11", "nan"),
    ("EUR", "2026-11", "0.9"),    # ningún portal la declara
    ("USD", "2026-11", "1"),      # usa tasa 1, no se guarda
])
def test_rechaza_entradas_invalidas(moneda, mes, valor):
    with pytest.raises(fx.ErrorFx):
        fx.construir_tasa(moneda, mes, valor, "", HOY)


def test_rechaza_redondeo_que_cambia_el_monto_mas_de_una_milesima():
    with pytest.raises(fx.ErrorFx, match="redondeo"):
        fx.construir_tasa("CRC", "2026-11", "1500000", "", HOY)


def test_mes_actual_es_valido():
    assert fx.parsear_mes("2026-11", HOY) == date(2026, 11, 1)


def test_csv_valida_todo_antes_de_devolver(tmp_path):
    ok = tmp_path / "ok.csv"
    ok.write_text("moneda,mes,valor_por_dolar,fuente\nCRC,2026-10,510.5,BCCR\nCRC,2026-11,525.3,BCCR\n")
    assert len(fx.leer_csv(str(ok), HOY)) == 2
    mal = tmp_path / "mal.csv"
    mal.write_text("moneda,mes,valor_por_dolar,fuente\nCRC,2026-10,510.5,x\nCRC,2027-01,5,x\nCRC,2026-10,0,x\n")
    with pytest.raises(fx.ErrorFx) as e:
        fx.leer_csv(str(mal), HOY)
    assert "línea 3" in str(e.value) and "línea 4" in str(e.value)
    sin_col = tmp_path / "c.csv"
    sin_col.write_text("moneda,mes\nCRC,2026-10\n")
    with pytest.raises(fx.ErrorFx, match="columnas"):
        fx.leer_csv(str(sin_col), HOY)


# ── cargar ──────────────────────────────────────────────────────────────────

def test_cargar_dry_run_no_escribe():
    conn = Conn()
    t = fx.construir_tasa("CRC", "2026-11", "525.30", "BCCR", HOY)
    assert fx.cargar_tasas(conn, [t], aplicar=False) == {"insertar": 1, "actualizar": 0, "igual": 0}
    assert conn.escrituras() == []


def test_cargar_aplicar_hace_un_merge_y_es_idempotente():
    t = fx.construir_tasa("CRC", "2026-11", "525.30", "BCCR", HOY)
    conn = Conn()
    fx.cargar_tasas(conn, [t], aplicar=True)
    assert len(conn.escrituras()) == 1 and "MERGE INTO fx_tasa" in conn.escrituras()[0]
    # segunda vez: ya existe con la misma tasa -> no escribe
    conn2 = Conn({"FROM fx_tasa WHERE": (Decimal("0.001904"),)})
    assert fx.cargar_tasas(conn2, [t], aplicar=True)["igual"] == 1
    assert conn2.escrituras() == []
    # otra tasa -> actualizar
    conn3 = Conn({"FROM fx_tasa WHERE": (Decimal("0.0019"),)})
    assert fx.cargar_tasas(conn3, [t], aplicar=True)["actualizar"] == 1


# ── rellenar-moneda ─────────────────────────────────────────────────────────

def test_rellenar_moneda_solo_portales_de_una_moneda():
    conn = Conn({"COUNT(*) FROM proceso WHERE portal_id": (5,)})
    res = fx.rellenar_moneda(conn, aplicar=True)
    assert res == {"comprasal_sv": 5}
    upd = [(s, p) for s, p in conn.sentencias if s.startswith("UPDATE")]
    assert len(upd) == 1
    sql, params = upd[0]
    assert params == {"m": "USD", "p": "comprasal_sv"}
    assert "moneda IS NULL" in sql and "actualizado_en" not in sql


def test_rellenar_moneda_dry_run_no_escribe_e_idempotente():
    conn = Conn({"COUNT(*) FROM proceso WHERE portal_id": (5,)})
    fx.rellenar_moneda(conn, aplicar=False)
    assert conn.escrituras() == []
    vacio = Conn({"COUNT(*) FROM proceso WHERE portal_id": (0,)})
    fx.rellenar_moneda(vacio, aplicar=True)
    assert vacio.escrituras() == []


# ── recalcular ──────────────────────────────────────────────────────────────

def test_recalcular_dry_run_cuenta_sin_escribir(capsys):
    conn = Conn({"COUNT(*)": (7,)})
    out = fx.recalcular(conn, aplicar=False)
    assert out["cambiadas"] == 7 and out["sin_tasa_CRC"] == 7
    assert conn.escrituras() == []
    assert "SIN tasa" in capsys.readouterr().out


def test_recalcular_aplicar_usa_el_repo(monkeypatch):
    from src.memoria import repo

    llamadas = []
    monkeypatch.setattr(repo, "recalcular_monto_usd", lambda c: llamadas.append(c) or 3)
    conn = Conn({"COUNT(*)": (2,)})
    out = fx.recalcular(conn, aplicar=True)
    assert llamadas == [conn] and out["cambiadas"] == 3 and out["sin_tasa_CRC"] == 2


# ── conexión y CLI ──────────────────────────────────────────────────────────

class Pool:
    def __init__(self, conn):
        self.conn = conn
        self.liberada = False

    def acquire(self):
        return self.conn

    def release(self, conn):
        self.liberada = True


def _parchar(monkeypatch, conn):
    from src.memoria import db

    pool = Pool(conn)
    monkeypatch.setattr(db, "get_pool", lambda cfg: pool)
    monkeypatch.setattr("src.config.get_config", lambda: object())
    return pool


def test_abrir_lectura_es_read_only_y_hace_rollback(monkeypatch):
    conn = Conn()
    pool = _parchar(monkeypatch, conn)
    with fx._abrir(object(), escribir=False):
        pass
    assert conn.sentencias[0][0] == "SET TRANSACTION READ ONLY"
    assert conn.commits == 0 and conn.rollbacks == 1 and pool.liberada


def test_abrir_escritura_commit_una_vez_y_rollback_si_falla(monkeypatch):
    conn = Conn()
    _parchar(monkeypatch, conn)
    with fx._abrir(object(), escribir=True):
        pass
    assert conn.commits == 1 and conn.rollbacks == 0
    conn2 = Conn()
    _parchar(monkeypatch, conn2)
    with pytest.raises(RuntimeError), fx._abrir(object(), escribir=True):
        raise RuntimeError("boom")
    assert conn2.commits == 0 and conn2.rollbacks == 1


def test_cli_dry_run_no_commitea_y_aplicar_commitea_una_vez(monkeypatch):
    conn = Conn()
    _parchar(monkeypatch, conn)
    args = ["cargar", "--moneda", "CRC", "--mes", datetime.now(timezone.utc).strftime("%Y-%m"), "--colones-por-dolar", "525.3"]
    assert fx.main(args) == 0
    assert conn.commits == 0 and conn.escrituras() == []
    assert fx.main(args + ["--aplicar"]) == 0
    assert conn.commits == 1 and len(conn.escrituras()) == 1


def test_cli_rechaza_entrada_invalida_sin_conectar(monkeypatch):
    from src.memoria import db

    monkeypatch.setattr(db, "get_pool", lambda cfg: pytest.fail("no debe conectar"))
    assert fx.main(["cargar", "--moneda", "CRC", "--mes", "2999-01", "--colones-por-dolar", "5"]) == 2
    assert fx.main(["cargar"]) == 2


def test_estado_es_solo_lectura(monkeypatch, capsys):
    conn = Conn()
    _parchar(monkeypatch, conn)
    assert fx.main(["estado"]) == 0
    assert conn.commits == 0 and conn.escrituras() == []
    assert "Tasas cargadas" in capsys.readouterr().out



@pytest.mark.parametrize("aplicar", [False, True])
def test_descargar_ultimo_usa_una_transaccion(monkeypatch, capsys, aplicar):
    from src.memoria.fx_regla import TasaDia

    llamadas = []

    def obtener_ultimo(*, intentos):
        llamadas.append(intentos)
        return TasaDia("CRC", HOY, 445.2, 440.9, "Hacienda")

    monkeypatch.setattr(fx.fx_hacienda, "obtener_ultimo", obtener_ultimo)
    conn = Conn()
    pool = _parchar(monkeypatch, conn)
    assert fx.main(["descargar"] + (["--aplicar"] if aplicar else [])) == 0
    assert llamadas == [1]
    assert len(conn.escrituras()) == int(aplicar)
    assert conn.commits == int(aplicar) and conn.rollbacks == int(not aplicar)
    assert pool.liberada
    assert ("insertó/actualizó" if aplicar else "insertaría") in capsys.readouterr().out


def test_descargar_dry_run_reconoce_tasa_igual(monkeypatch, capsys):
    from src.memoria.fx_regla import TasaDia

    monkeypatch.setattr(fx.fx_hacienda, "obtener_ultimo",
                        lambda **kwargs: TasaDia("CRC", HOY, 445.2, None, "Hacienda"))
    conn = Conn({"FROM fx_tasa_dia WHERE": ("ya estaba",)})
    _parchar(monkeypatch, conn)
    assert fx.main(["descargar"]) == 0
    assert conn.escrituras() == []
    assert "ya estaba" in capsys.readouterr().out


@pytest.mark.parametrize("vacio", [False, True])
def test_descargar_historico_continua_y_commitea_tramos_exitosos(monkeypatch, capsys, vacio):
    from src.memoria.fx_regla import TasaDia

    tramos = []

    def obtener_rango(desde, hasta):
        tramos.append((desde.day, hasta.day))
        if desde.day == 15:
            if vacio:
                return []
            raise fx.fx_hacienda.ErrorFxHacienda("HTTP 503")
        return [TasaDia("CRC", desde, 445.2, None, "Hacienda")]

    monkeypatch.setattr(fx.fx_hacienda, "obtener_rango", obtener_rango)
    conn = Conn()
    _parchar(monkeypatch, conn)
    assert fx.main(["descargar", "--historico", "--desde", "2026-11-01",
                    "--hasta", "2026-11-30", "--aplicar"]) == 1
    assert tramos == [(1, 14), (15, 28), (29, 30)]
    assert len(conn.escrituras()) == 2 and conn.commits == 1
    mensaje = "SIN DATOS de Hacienda" if vacio else "HTTP 503"
    assert f"2026-11-15 a 2026-11-28: {mensaje}" in capsys.readouterr().err


@pytest.mark.parametrize(("hasta", "codigo"), [("2026-11-29", 0), ("2026-11-30", 1)])
def test_descargar_historico_vacio_depende_de_si_incluye_un_dia_habil(monkeypatch, capsys, hasta, codigo):
    monkeypatch.setattr(fx.fx_hacienda, "obtener_rango", lambda desde, hasta: [])
    conn = Conn()
    _parchar(monkeypatch, conn)
    assert fx.main(["descargar", "--historico", "--desde", "2026-11-28", "--hasta", hasta, "--aplicar"]) == codigo
    assert not conn.escrituras()
    assert ("SIN DATOS" in capsys.readouterr().err) is bool(codigo)


def test_estado_informa_antiguedad_y_monedas_sin_tasa(monkeypatch, capsys):
    monkeypatch.setattr(fx, "_hoy_cr", lambda: HOY)

    def todos(conn, sql, params=None):
        if "ROW_NUMBER()" in sql:
            return [("CRC", datetime(2026, 11, 22), 445.2, 1), ("CRC", date(2026, 11, 21), 444, 2)]
        if "GROUP BY p.moneda" in sql:
            assert fx.repo.SQL_FILAS_SIN_TASA in sql
            return [("EUR", 4)]
        return []

    monkeypatch.setattr(fx, "_todos", todos)
    fx.estado(Conn())
    salida = capsys.readouterr().out
    assert "CRC 2026-11-22: venta=445.2 (antigüedad: 2 días)" in salida
    assert "CRC 2026-11-21: venta=444" in salida
    assert "EUR: 4" in salida
