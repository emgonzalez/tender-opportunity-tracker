"""Quién recibe el reporte (`repo.destinatarios_reporte`), los admin_plataforma (`usuarios_con_rol`) y el
voto de un usuario del portal en el servicio web de feedback. Sin ADB: el SQL se simula.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from src.feedback import tokens
from src.identidad import repo
from src.web import feedback as web_feedback
from src.web.app import crear_app

ORG = "org"
SV, CR = "cli_sv", "cli_cr"
ACTIVOS = [SV, CR]

# usuario_id -> (correo, activo, roles de organización, membresías de país)
USUARIOS = {
    "u_ceo": ("Ceo@Example.com", True, ["ceo"], []),
    "u_dir": ("dir@example.com", True, ["director_comercial"], []),
    "u_ventas": ("ventas@example.com", True, [], [(SV, "equipo_ventas")]),
    "u_ventas_cr": ("ventas.cr@example.com", True, [], [(CR, "equipo_ventas")]),
    "u_oracle": ("oracle@example.com", True, ["gerente_marca_oracle"], []),
    "u_oracle_pais": ("oracle.pais@example.com", True, ["director_servicios_oracle"], [(SV, "gerente_pais")]),
    "u_admin_plataforma": ("plataforma@example.com", True, ["admin_plataforma"], []),
    "u_operador": ("operador@example.com", True, ["operador"], []),
    "u_admin_org": ("adminorg@example.com", True, ["admin_organizacion"], []),
    "u_admin_org_ceo": ("adminorg.ceo@example.com", True, ["admin_organizacion", "ceo"], []),
    "u_baja": ("baja@example.com", False, ["ceo"], []),
    "u_sin_roles": ("sinroles@example.com", True, [], []),
    # mismo correo que u_ceo, otra mayúscula: sale una sola vez
    "u_ceo_bis": ("CEO@example.com", True, ["ceo"], []),
}


@pytest.fixture
def mundo(monkeypatch):
    monkeypatch.setattr(repo, "_todos", lambda c, sql, b: [(u, d[0]) for u, d in USUARIOS.items() if d[1]])
    monkeypatch.setattr(repo, "obtener_usuario", lambda c, u: {
        "usuario_id": u, "org_id": ORG, "email": USUARIOS[u][0], "activo": USUARIOS[u][1]})
    monkeypatch.setattr(repo, "roles_org", lambda c, u: USUARIOS[u][2])
    monkeypatch.setattr(repo, "membresias_pais", lambda c, u: USUARIOS[u][3])
    monkeypatch.setattr(repo, "clientes_activos", lambda c, o: ACTIVOS)


def _recibe(cliente: str) -> dict[str, frozenset[str]]:
    return dict(repo.destinatarios_reporte(None, ORG, cliente))


class TestDestinatariosReporte:
    def test_reciben_los_roles_de_negocio_con_acceso_al_pais(self, mundo):
        r = _recibe(SV)
        assert {"ceo@example.com", "dir@example.com", "ventas@example.com"} <= set(r)

    def test_el_rol_de_un_pais_no_recibe_el_reporte_de_otro(self, mundo):
        assert "ventas@example.com" in _recibe(SV)
        assert "ventas@example.com" not in _recibe(CR)
        assert "ventas.cr@example.com" in _recibe(CR) and "ventas.cr@example.com" not in _recibe(SV)

    @pytest.mark.parametrize("correo", ["plataforma@example.com", "operador@example.com", "adminorg@example.com",
                                        "baja@example.com", "sinroles@example.com"])
    def test_no_reciben_roles_tecnicos_ni_admin_org_solo_ni_bajas_ni_sin_roles(self, mundo, correo):
        assert correo not in _recibe(SV)

    def test_admin_organizacion_con_rol_comercial_si_recibe(self, mundo):
        assert _recibe(SV)["adminorg.ceo@example.com"] == frozenset()

    def test_perfil_completo_vacio_y_rol_oracle_restringido(self, mundo):
        r = _recibe(SV)
        assert r["ceo@example.com"] == frozenset()
        assert r["oracle@example.com"] == frozenset({"oracle"})

    def test_oracle_con_rol_de_pais_sigue_restringido_a_oracle(self, mundo):
        # E14: la membresía de país no levanta la restricción (lo decide resolver_alcance).
        assert _recibe(SV)["oracle.pais@example.com"] == frozenset({"oracle"})

    def test_correo_en_minusculas_y_sin_repetidos(self, mundo):
        correos = [c for c, _ in repo.destinatarios_reporte(None, ORG, SV)]
        assert len(correos) == len(set(correos))
        assert all(c == c.lower() for c in correos)
        assert correos.count("ceo@example.com") == 1


class TestUsuariosConRol:
    def test_lee_correos_activos_por_rol_con_binds(self):
        conn, cur = MagicMock(), MagicMock()
        conn.cursor.return_value = cur
        cur.fetchall.return_value = [("a@example.com",), ("b@example.com",), (None,)]
        assert repo.usuarios_con_rol(conn, None, "admin_plataforma") == ["a@example.com", "b@example.com"]
        sql, binds = cur.execute.call_args[0]
        assert "u.activo = 1" in sql and "mo.rol = :r" in sql and ":o" not in sql
        assert binds == {"r": "admin_plataforma"}

    def test_con_organizacion_filtra_por_ella(self):
        conn, cur = MagicMock(), MagicMock()
        conn.cursor.return_value = cur
        cur.fetchall.return_value = []
        repo.usuarios_con_rol(conn, ORG, "admin_plataforma")
        sql, binds = cur.execute.call_args[0]
        assert "u.org_id = :o" in sql and binds == {"r": "admin_plataforma", "o": ORG}


# ── El voto de quien recibe el reporte se atribuye ─────────────────────────

SECRETO = "secreto-de-prueba"
PORTAL = "analista@example.com"


def test_el_voto_de_un_usuario_del_portal_queda_atribuido():
    cfg = SimpleNamespace(
        enable_feedback=True, feedback_base_url="https://feedback.example.com", feedback_hmac_secret=SECRETO,
        clients_dir="/no/existe", data_dir="/no/existe", override_recipients=[],
    )
    perfil = SimpleNamespace(notification=SimpleNamespace(email_to=[], system_admins_email=[]), feedback_motivos=[])
    guardados = []
    token = tokens.firmar(SECRETO, "96-2026-P0011", "cliente_x", tokens.destinatario_id(PORTAL))
    with patch.object(web_feedback, "load_client_profile", return_value=perfil), \
         patch.object(web_feedback, "_destinatarios_portal", return_value=[PORTAL]), \
         patch("src.feedback.service.leer_reportada", return_value={}), \
         patch("src.feedback.service.leer_voto", return_value=None), \
         patch("src.feedback.service.guardar_voto",
               side_effect=lambda _c, **k: guardados.append(k) or True):
        r = TestClient(crear_app(cfg)).post(f"/f/{token}/relevant")
    assert r.status_code == 200
    assert guardados and guardados[0]["email"] == PORTAL


def test_destinatarios_portal_tolera_la_adb_caida():
    # Sin atributos de ADB en la config la conexión falla: el resultado es vacío, no una excepción.
    assert web_feedback._destinatarios_portal(SimpleNamespace(), "cliente_x") == []
