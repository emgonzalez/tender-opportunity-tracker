"""Política de sesión (P5) y auditoría (P6) de `src.identidad`. SQL contra conexión mock."""
from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from src.config import Config
from src.identidad import politica, repo


@pytest.fixture(autouse=True)
def _env_minimo(monkeypatch):
    """Lo que `Config()` exige para construirse; el CI no trae un .env."""
    for var in ("COMPRASAL_EMAIL", "COMPRASAL_PASSWORD", "SMTP_USER", "SMTP_PASSWORD"):
        monkeypatch.setenv(var, "x")


@pytest.fixture
def cursor():
    return MagicMock()


@pytest.fixture
def conn(cursor):
    c = MagicMock()
    c.cursor.return_value = cursor
    return c


def test_los_valores_por_defecto_son_los_de_p5(monkeypatch):
    for var in ("CODIGO_TTL_MIN", "SESION_TTL_HORAS", "SESION_MAX_DIAS", "SESION_PURGA_DIAS"):
        monkeypatch.delenv(var, raising=False)
    cfg = Config()
    assert (cfg.codigo_ttl_min, cfg.sesion_ttl_horas, cfg.sesion_max_dias, cfg.sesion_purga_dias) == (10, 12, 7, 30)


def test_los_valores_se_leen_del_entorno(monkeypatch):
    monkeypatch.setenv("SESION_TTL_HORAS", "4")
    assert Config().sesion_ttl_horas == 4


class TestRenovarSesion:
    def test_desliza_con_tope_desde_el_login(self, conn, cursor):
        cursor.rowcount = 1
        assert repo.renovar_sesion(conn, "hash1", ttl_horas=12, max_dias=7) is True
        sql, binds = cursor.execute.call_args[0]
        assert "LEAST(SYSTIMESTAMP + NUMTODSINTERVAL(:ttl_horas, 'HOUR')" in sql
        assert "creado_en + NUMTODSINTERVAL(:max_dias, 'DAY')" in sql      # nunca pasa de 7 días desde el login
        assert binds == {"ttl_horas": 12, "max_dias": 7, "sesion_id": "hash1"}

    def test_no_resucita_una_sesion_revocada_ni_vencida(self, conn, cursor):
        cursor.rowcount = 0
        repo.renovar_sesion(conn, "hash1", 12, 7)
        sql, _ = cursor.execute.call_args[0]
        assert "revocada = 0" in sql and "expira_en > SYSTIMESTAMP" in sql

    def test_false_si_no_renovó_nada(self, conn, cursor):
        cursor.rowcount = 0
        assert repo.renovar_sesion(conn, "no-existe", 12, 7) is False


class TestPurgarSesiones:
    def test_borra_las_vencidas_hace_mas_de_n_dias(self, conn, cursor):
        cursor.rowcount = 5
        assert repo.purgar_sesiones_vencidas(conn, 30) == 5
        sql, binds = cursor.execute.call_args[0]
        assert sql.startswith("DELETE FROM sesion WHERE expira_en < SYSTIMESTAMP - NUMTODSINTERVAL(:dias, 'DAY')")
        assert binds == {"dias": 30}

    def test_dry_run_solo_cuenta(self, conn, cursor):
        cursor.fetchone.return_value = (7,)
        assert repo.purgar_sesiones_vencidas(conn, 30, dry_run=True) == 7
        sql, _ = cursor.execute.call_args[0]
        assert sql.startswith("SELECT COUNT(*)") and "DELETE" not in sql


class TestAuditoria:
    def test_inserta_con_binds_y_alcance_json(self, conn, cursor):
        repo.registrar_auditoria(conn, "mc", "LOGIN_OK", usuario_id="u1", recurso="/portal",
                                 alcance={"clientes": ["multicomputos"]}, ip_hash="abc")
        sql, binds = cursor.execute.call_args[0]
        assert "INSERT INTO auditoria" in sql and "UPDATE" not in sql        # append-only
        assert binds["accion"] == "LOGIN_OK" and binds["org_id"] == "mc" and binds["usuario_id"] == "u1"
        assert json.loads(binds["alcance"]) == {"clientes": ["multicomputos"]}

    def test_sin_alcance_ni_usuario(self, conn, cursor):
        repo.registrar_auditoria(conn, "mc", "LOGIN_FALLIDO")
        _, binds = cursor.execute.call_args[0]
        assert binds["alcance"] is None and binds["usuario_id"] is None

    def test_recorta_el_recurso_al_largo_de_la_columna(self, conn, cursor):
        repo.registrar_auditoria(conn, "mc", "ACCESO_FUERA_DE_ALCANCE", recurso="x" * 500)
        assert len(cursor.execute.call_args[0][1]["recurso"]) == 200

    def test_una_accion_fuera_de_la_politica_se_rechaza_sin_tocar_la_base(self, conn, cursor):
        with pytest.raises(politica.AccionNoAuditable):
            repo.registrar_auditoria(conn, "mc", "VISTA_CONSULTADA")           # P6: no se audita cada vista
        cursor.execute.assert_not_called()

    def test_la_politica_cubre_lo_decidido_en_p6(self):
        assert {"LOGIN_OK", "LOGIN_FALLIDO", "SESION_REVOCADA", "ESTADO_CAMBIADO", "ACCESO_FUERA_DE_ALCANCE",
                "USUARIO_ALTA", "USUARIO_BAJA", "MEMBRESIA_ALTA", "MEMBRESIA_BAJA", "ROL_CAMBIADO"} <= politica.ACCIONES_AUDITADAS
        assert not any("VISTA" in a for a in politica.ACCIONES_AUDITADAS)
