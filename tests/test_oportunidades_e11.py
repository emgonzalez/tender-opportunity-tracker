"""E11: asignar responsable y recuperar un descartado de AMALIA — servicio (repo simulado) y SQL (cursor falso)."""
from __future__ import annotations

from dataclasses import replace
from unittest.mock import MagicMock

import pytest

from src.identidad.alcance import Alcance
from src.oportunidades import repo, service

ASIGNABLES = [{"usuario_id": "u2", "nombre": "Beto"}, {"usuario_id": "u3", "nombre": "Carla"}]


class _Repo:
    """Repo e identidad simulados: registran cada llamada en orden."""

    def __init__(self, existente=None, visible=True, crea=True, fija=True, descartado=True,
                 asignables=ASIGNABLES):
        self.existente, self.visible, self.crea, self.fija = existente, visible, crea, fija
        self.descartado, self.asignables = descartado, asignables
        self.llamadas, self.escrituras, self.eventos, self.auditoria = [], [], [], []

    def install(self, mp):
        r = self

        def leer(nombre, valor):
            def f(*a, **k):
                r.llamadas.append(nombre)
                return valor() if callable(valor) else valor
            return f

        mp.setattr(repo, "proceso_visible", leer("visible", lambda: r.visible))
        mp.setattr(repo, "obtener", leer("obtener", lambda: r.existente))
        mp.setattr(repo, "es_descartado_por_amalia", leer("descartado", lambda: r.descartado))
        mp.setattr(service.identidad, "usuarios_asignables", leer("asignables", lambda: r.asignables))
        mp.setattr(repo, "crear_estado", lambda c, cl, g, e, u, m, mo, co: r.escrituras.append(("crear", e, u, m, mo, co)) or r.crea)
        mp.setattr(repo, "fijar_responsable", lambda c, cl, g, resp, t: r.escrituras.append(("fijar", resp, t)) or r.fija)
        mp.setattr(repo, "actualizar_estado", lambda *a: pytest.fail("no debe cambiar el estado"))
        mp.setattr(repo, "insertar_evento", lambda c, cl, g, a, n, u, nota: r.eventos.append((a, n, u, nota)))
        mp.setattr(service.identidad, "registrar_auditoria",
                   lambda c, org, accion, **kw: r.auditoria.append((org, accion, kw)))

    @property
    def nada_escrito(self):
        return self.escrituras == self.eventos == self.auditoria == []


def _alcance(cambia=("cli",), clientes=("cli",), simulado=None):
    return Alcance(usuario_id="u1", org_id="org", roles=frozenset({"gerente_pais"}), clientes=frozenset(clientes),
                   cambia_estado=frozenset(cambia), ve_operativo=False, simulado=simulado)


@pytest.fixture
def usar(monkeypatch):
    def _u(**kw):
        r = _Repo(**kw)
        r.install(monkeypatch)
        return r
    return _u


class _Intocable:
    def cursor(self):
        raise AssertionError("bajo simulación no se toca la base")


# ── asignar_responsable ────────────────────────────────────────────────────

class TestAsignarResponsable:
    def test_simulado_no_toca_la_base(self, monkeypatch):
        for nombre in ("proceso_visible", "obtener", "crear_estado", "fijar_responsable", "insertar_evento"):
            monkeypatch.setattr(repo, nombre, lambda *a, **k: pytest.fail("tocó la base"))
        sim = replace(_alcance(), simulado="gerente_pais")
        assert service.asignar_responsable(_Intocable(), sim, "cli", "p:1", "u2", "", "ip") == service.SIMULACION

    @pytest.mark.parametrize("alcance,visible", [
        (_alcance(cambia=()), True),                               # solo lectura en ese país
        (_alcance(clientes=("otro",), cambia=("otro",)), True),   # país ajeno
        (_alcance(), False),                                       # proceso de otro cliente (IDOR)
    ])
    def test_ajeno_es_no_encontrado_antes_de_validar_responsable(self, usar, alcance, visible):
        r = usar(visible=visible, asignables=[])
        assert service.asignar_responsable(None, alcance, "cli", "p:1", "u2", "", None) == service.NO_ENCONTRADO
        assert "asignables" not in r.llamadas and r.nada_escrito

    @pytest.mark.parametrize("responsable", ["u9", "inactivo", "de_otra_org", "sin_acceso", "ceo_sin_permiso"])
    def test_responsable_fuera_de_asignables_es_invalido(self, usar, responsable):
        # `usuarios_asignables` ya excluye inactivos, otra org, sin acceso al cliente y roles sin permiso.
        r = usar(existente={"estado": "EVALUANDO", "token": "T", "responsable": None})
        assert service.asignar_responsable(None, _alcance(), "cli", "p:1", responsable, "T", None) == \
            service.RESPONSABLE_INVALIDO
        assert r.nada_escrito and "obtener" not in r.llamadas  # se valida antes del token

    def test_asignables_se_piden_con_la_org_del_alcance_y_el_cliente(self, usar, monkeypatch):
        usar()
        vistos = []
        monkeypatch.setattr(service.identidad, "usuarios_asignables",
                            lambda c, org, cl: vistos.append((org, cl)) or ASIGNABLES)
        service.asignar_responsable(None, _alcance(), "cli", "p:1", "u2", "", None)
        assert vistos == [("org", "cli")]

    def test_asigna_sobre_fila_existente(self, usar):
        r = usar(existente={"estado": "EVALUANDO", "token": "T", "responsable": None})
        assert service.asignar_responsable(None, _alcance(), "cli", "p:1", "u3", "T", "iph") == service.OK
        assert r.escrituras == [("fijar", "u3", "T")]
        assert r.eventos == [("EVALUANDO", "EVALUANDO", "u1", "Asignada a Carla")]
        org, accion, kw = r.auditoria[0]
        assert (org, accion) == ("org", "RESPONSABLE_ASIGNADO")
        assert kw["recurso"] == "cliente=cli;proceso=p:1;responsable=u3"
        assert kw["usuario_id"] == "u1" and kw["ip_hash"] == "iph" and kw["alcance"] == {"cliente_id": "cli"}

    def test_sin_fila_crea_nuevo_con_ese_responsable(self, usar):
        r = usar()
        assert service.asignar_responsable(None, _alcance(), "cli", "p:1", "u2", "", None) == service.OK
        assert r.escrituras == [("crear", "NUEVO", "u2", None, None, None)]
        assert r.eventos == [("NUEVO", "NUEVO", "u1", "Asignada a Beto")]

    @pytest.mark.parametrize("vacio", [None, "", "  "])
    def test_quitar_responsable(self, usar, vacio):
        r = usar(existente={"estado": "OFERTADO", "token": "T", "responsable": "u2"})
        assert service.asignar_responsable(None, _alcance(), "cli", "p:1", vacio, "T", None) == service.OK
        assert "asignables" not in r.llamadas
        assert r.escrituras == [("fijar", None, "T")]
        assert r.eventos == [("OFERTADO", "OFERTADO", "u1", "Responsable quitado")]
        assert r.auditoria[0][2]["recurso"] == "cliente=cli;proceso=p:1;responsable="

    def test_sin_cambio_no_escribe(self, usar):
        r = usar(existente={"estado": "NUEVO", "token": "T", "responsable": "u2"})
        assert service.asignar_responsable(None, _alcance(), "cli", "p:1", "u2", "T", None) == service.OK
        assert r.nada_escrito

    def test_quitar_sin_fila_no_crea_nada(self, usar):
        r = usar()
        assert service.asignar_responsable(None, _alcance(), "cli", "p:1", None, "", None) == service.OK
        assert r.nada_escrito

    def test_token_vencido_es_conflicto(self, usar):
        r = usar(existente={"estado": "NUEVO", "token": "NUEVO-T", "responsable": None})
        assert service.asignar_responsable(None, _alcance(), "cli", "p:1", "u2", "VIEJO-T", None) == service.CONFLICTO
        assert r.nada_escrito

    def test_sin_fila_pero_con_token_es_conflicto(self, usar):
        r = usar()
        assert service.asignar_responsable(None, _alcance(), "cli", "p:1", "u2", "fantasma", None) == service.CONFLICTO
        assert r.nada_escrito

    def test_update_condicionado_sin_filas_es_conflicto(self, usar):
        r = usar(existente={"estado": "NUEVO", "token": "T", "responsable": None}, fija=False)
        assert service.asignar_responsable(None, _alcance(), "cli", "p:1", "u2", "T", None) == service.CONFLICTO
        assert r.eventos == r.auditoria == []

    def test_alguien_creo_la_fila_antes_es_conflicto(self, usar):
        r = usar(crea=False)
        assert service.asignar_responsable(None, _alcance(), "cli", "p:1", "u2", "", None) == service.CONFLICTO
        assert r.eventos == r.auditoria == []

    def test_fallo_de_auditoria_sube_para_el_rollback(self, usar, monkeypatch):
        usar()
        conn = MagicMock()

        def boom(*a, **k):
            raise RuntimeError("auditoría no escrita")

        monkeypatch.setattr(service.identidad, "registrar_auditoria", boom)
        with pytest.raises(RuntimeError):
            service.asignar_responsable(conn, _alcance(), "cli", "p:1", "u2", "", None)
        conn.commit.assert_not_called()  # sin commit: quien llama hace rollback y no queda nada


# ── recuperar_descartado ───────────────────────────────────────────────────

class TestRecuperarDescartado:
    def test_simulado_no_toca_la_base(self, monkeypatch):
        for nombre in ("proceso_visible", "es_descartado_por_amalia", "crear_estado", "insertar_evento"):
            monkeypatch.setattr(repo, nombre, lambda *a, **k: pytest.fail("tocó la base"))
        sim = replace(_alcance(), simulado="equipo_ventas")
        assert service.recuperar_descartado(_Intocable(), sim, "cli", "p:1", None, None) == service.SIMULACION

    @pytest.mark.parametrize("alcance,visible", [
        (_alcance(cambia=()), True),
        (_alcance(clientes=("otro",), cambia=("otro",)), True),
        (_alcance(), False),
    ])
    def test_ajeno_es_no_encontrado(self, usar, alcance, visible):
        r = usar(visible=visible)
        assert service.recuperar_descartado(None, alcance, "cli", "p:1", None, None) == service.NO_ENCONTRADO
        assert "descartado" not in r.llamadas and r.nada_escrito

    def test_proceso_que_desaparece_entre_lecturas_es_no_encontrado(self, usar):
        r = usar(descartado=None)
        assert service.recuperar_descartado(None, _alcance(), "cli", "p:1", None, None) == service.NO_ENCONTRADO
        assert r.nada_escrito

    def test_recupera_crea_nuevo_sin_responsable_evento_y_auditoria(self, usar):
        r = usar()
        assert service.recuperar_descartado(None, _alcance(), "cli", "p:1", None, "iph") == service.OK
        assert r.escrituras == [("crear", "NUEVO", None, None, None, None)]
        assert r.eventos == [(None, "NUEVO", "u1", "Recuperada de descartados de AMALIA")]
        org, accion, kw = r.auditoria[0]
        assert (org, accion) == ("org", "DESCARTADO_RECUPERADO")
        assert kw["recurso"] == "cliente=cli;proceso=p:1" and kw["ip_hash"] == "iph"

    def test_ya_relevante_no_es_recuperable(self, usar):
        r = usar(descartado=False, existente=None)
        assert service.recuperar_descartado(None, _alcance(), "cli", "p:1", None, None) == service.NO_RECUPERABLE
        assert r.nada_escrito

    def test_ya_recuperado(self, usar):
        r = usar(descartado=False, existente={"estado": "NUEVO", "token": "T", "responsable": None})
        assert service.recuperar_descartado(None, _alcance(), "cli", "p:1", "T", None) == service.YA_RECUPERADO
        assert r.nada_escrito

    def test_token_de_una_fila_que_no_existe_es_conflicto(self, usar):
        r = usar()
        assert service.recuperar_descartado(None, _alcance(), "cli", "p:1", "fantasma", None) == service.CONFLICTO
        assert r.nada_escrito

    def test_recuperado_en_paralelo_es_conflicto(self, usar):
        r = usar(crea=False)
        assert service.recuperar_descartado(None, _alcance(), "cli", "p:1", "", None) == service.CONFLICTO
        assert r.eventos == r.auditoria == []

    def test_fallo_de_auditoria_sube_para_el_rollback(self, usar, monkeypatch):
        usar()
        conn = MagicMock()

        def boom(*a, **k):
            raise RuntimeError("auditoría no escrita")

        monkeypatch.setattr(service.identidad, "registrar_auditoria", boom)
        with pytest.raises(RuntimeError):
            service.recuperar_descartado(conn, _alcance(), "cli", "p:1", None, None)
        conn.commit.assert_not_called()


# ── SQL (cursor falso) ─────────────────────────────────────────────────────

@pytest.fixture
def cursor():
    return MagicMock()


@pytest.fixture
def conn(cursor):
    c = MagicMock()
    c.cursor.return_value = cursor
    return c


class TestSQL:
    def test_listar_incluye_relevantes_o_con_fila(self, conn, cursor):
        cursor.fetchall.return_value = []
        repo.listar(conn, "cli", dias=30, limite=10)
        sql, binds = cursor.execute.call_args[0]
        assert f"({repo._SQL_ES_RELEVANTE} OR e.cliente_id IS NOT NULL)" in sql
        assert binds == {"c": "cli", "dias": 30, "limite": 10}

    def test_obtener_devuelve_el_responsable(self, conn, cursor):
        cursor.fetchone.return_value = ("NUEVO", "T", "u2")
        assert repo.obtener(conn, "cli", "p:1") == {"estado": "NUEVO", "token": "T", "responsable": "u2"}

    def test_fijar_responsable_solo_toca_el_responsable_y_exige_token(self, conn, cursor):
        cursor.rowcount = 1
        assert repo.fijar_responsable(conn, "cli", "p:1", None, "T") is True
        sql, binds = cursor.execute.call_args[0]
        assert "responsable_usuario_id = :r" in sql and "= :token" in sql
        for col in ("estado =", "motivo", "monto", "origen", "competidor"):
            assert col not in sql.split("WHERE")[0]
        assert binds == {"r": None, "c": "cli", "g": "p:1", "token": "T"}
        conn.commit.assert_not_called()

    def test_fijar_responsable_false_si_cambio_la_fila(self, conn, cursor):
        cursor.rowcount = 0
        assert repo.fijar_responsable(conn, "cli", "p:1", "u2", "T") is False

    @pytest.mark.parametrize("fila,esperado", [((1,), True), ((0,), False), (None, None)])
    def test_es_descartado_por_amalia(self, conn, cursor, fila, esperado):
        cursor.fetchone.return_value = fila
        assert repo.es_descartado_por_amalia(conn, "cli", "p:1") is esperado
        sql, binds = cursor.execute.call_args[0]
        assert repo._SQL_ES_RELEVANTE in sql and "e.cliente_id IS NOT NULL" in sql
        assert "cp.cliente_id = :c AND cp.proceso_gid = :g" in sql and binds == {"c": "cli", "g": "p:1"}

    def test_crear_estado_sin_responsable(self, conn, cursor):
        assert repo.crear_estado(conn, "cli", "p:1", "NUEVO", None, None, None, None) is True
        _, binds = cursor.execute.call_args[0]
        assert binds["u"] is None and binds["estado"] == "NUEVO"
