"""E10 «ver como»: alcance simulado, opciones, validación y transacción auditada. Sin Oracle (repo falso)."""
from __future__ import annotations

import dataclasses

import pytest

from src.identidad import alcance, politica, repo, ver_como

ACTIVOS = ["sv", "gt", "hn"]
NOMBRES = {"sv": "El Salvador", "gt": "Guatemala", "hn": "Honduras"}
ADMIN = alcance.Alcance(usuario_id="admin1", org_id="org", roles=frozenset({"admin_plataforma"}),
                        clientes=frozenset(), cambia_estado=frozenset(), ve_operativo=True)


@pytest.fixture
def mundo(monkeypatch):
    """Repo en memoria: un usuario «u1» configurable, países activos y la sesión del admin."""
    datos = {"roles_org": [], "membresias": [], "activos": list(ACTIVOS), "sesion": None,
             "auditoria": [], "fallar_auditoria": False, "fijar_ok": True}
    monkeypatch.setattr(repo, "obtener_usuario", lambda c, u: (
        {"usuario_id": u, "org_id": "org", "email": "x@y.z", "activo": True} if u == "u1" else None))
    monkeypatch.setattr(repo, "roles_org", lambda c, u: datos["roles_org"])
    monkeypatch.setattr(repo, "membresias_pais", lambda c, u: datos["membresias"])
    monkeypatch.setattr(repo, "clientes_activos", lambda c, o: datos["activos"] if o == "org" else [])
    monkeypatch.setattr(ver_como, "nombres_clientes", lambda c, ids: {i: NOMBRES[i] for i in ids})

    def fijar(conn, token_hash, rol, cliente):
        if not datos["fijar_ok"]:
            return False
        datos["sesion"] = (rol, cliente)
        return True

    def limpiar(conn, token_hash):
        hubo, datos["sesion"] = datos["sesion"] is not None, None
        return hubo

    def auditar(conn, org_id, accion, usuario_id=None, recurso=None, alcance=None, ip_hash=None):
        if datos["fallar_auditoria"]:
            raise RuntimeError("auditoría caída")
        assert accion in politica.ACCIONES_AUDITADAS
        datos["auditoria"].append((org_id, accion, usuario_id, recurso, ip_hash))

    monkeypatch.setattr(repo, "fijar_ver_como", fijar, raising=False)
    monkeypatch.setattr(repo, "limpiar_ver_como", limpiar, raising=False)
    monkeypatch.setattr(repo, "registrar_auditoria", auditar)
    return datos


class Conn:
    """Conexión falsa que solo cuenta commits y rollbacks."""

    def __init__(self):
        self.commits = self.rollbacks = 0

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def _sin_identidad(a):
    return dataclasses.replace(a, usuario_id="-", simulado=None)


# ── politica ───────────────────────────────────────────────────────────────

def test_constantes_de_politica():
    assert politica.ROLES_VER_COMO == {"admin_plataforma"}
    assert "admin_plataforma" not in politica.ROLES_SIMULABLES
    assert politica.ROLES_SIMULABLES == politica.ROLES - {"admin_plataforma"}
    assert {"operador", "admin_organizacion", "gerente_pais", "equipo_ventas"} <= politica.ROLES_SIMULABLES
    assert {"VER_COMO_INICIO", "VER_COMO_FIN"} <= politica.ACCIONES_AUDITADAS
    assert politica.TTL_VER_COMO_MIN == 60


# ── alcance_simulado ───────────────────────────────────────────────────────

class TestEquivalencia:
    @pytest.mark.parametrize("rol", sorted(politica.ROLES_SIMULABLES))
    def test_simulado_es_identico_al_real_del_mismo_rol(self, mundo, rol):
        cliente = "gt" if rol in politica.ROLES_PAIS else None
        if cliente:
            mundo["membresias"] = [(cliente, rol)]
        else:
            mundo["roles_org"] = [rol]
        real = alcance.resolver_alcance(None, "u1")
        sim = alcance.alcance_simulado(None, ADMIN, rol, cliente)
        assert sim is not None and sim.simulado == rol and real.simulado is None
        assert _sin_identidad(sim) == _sin_identidad(real)

    def test_conserva_usuario_y_org_del_admin(self, mundo):
        sim = alcance.alcance_simulado(None, ADMIN, "gerente_pais", "sv")
        assert (sim.usuario_id, sim.org_id) == ("admin1", "org")

    def test_la_simulacion_reemplaza_todo_el_alcance_del_admin(self, mundo):
        admin_y_mas = dataclasses.replace(ADMIN, roles=frozenset({"admin_plataforma", "operador"}))
        sim = alcance.alcance_simulado(None, admin_y_mas, "ceo")
        assert sim.roles == {"ceo"} and not sim.ve_operativo


class TestPermisosSimulados:
    def test_director_comercial_cambia_en_todos(self, mundo):
        sim = alcance.alcance_simulado(None, ADMIN, "director_comercial")
        assert sim.clientes == sim.cambia_estado == set(ACTIVOS)

    def test_ceo_ve_todos_y_solo_lee(self, mundo):
        sim = alcance.alcance_simulado(None, ADMIN, "ceo")
        assert sim.clientes == set(ACTIVOS) and not sim.cambia_estado and sim.es_regional

    def test_gerente_pais_solo_el_suyo(self, mundo):
        sim = alcance.alcance_simulado(None, ADMIN, "gerente_pais", "hn")
        assert sim.clientes == sim.cambia_estado == {"hn"} and not sim.es_regional

    def test_operador_solo_operativo(self, mundo):
        sim = alcance.alcance_simulado(None, ADMIN, "operador")
        assert sim.ve_operativo and not sim.clientes and not sim.cambia_estado


class TestRechazos:
    @pytest.mark.parametrize("real", [
        None,
        dataclasses.replace(ADMIN, roles=frozenset({"operador"})),
        dataclasses.replace(ADMIN, roles=frozenset({"admin_organizacion"})),
        dataclasses.replace(ADMIN, simulado="ceo"),  # un alcance ya simulado no encadena
    ])
    def test_quien_no_es_admin_real_no_simula(self, mundo, real):
        assert alcance.alcance_simulado(None, real, "ceo") is None
        assert ver_como.opciones(None, real) == []
        assert ver_como.validar(None, real, "ceo") is None

    @pytest.mark.parametrize("rol,cliente", [
        ("admin_plataforma", None),          # es «Mi vista», no un destino
        ("superusuario", None),
        ("CEO", None),
        ("gerente_pais", None),              # rol de país sin país
        ("equipo_ventas", ""),
        ("gerente_pais", "zz"),              # país ajeno o inexistente
        ("ceo", "sv"),                       # rol de org con país: se rechaza, no se ignora
        ("operador", ""),
    ])
    def test_combinaciones_invalidas(self, mundo, rol, cliente):
        assert alcance.alcance_simulado(None, ADMIN, rol, cliente) is None

    def test_pais_inactivo(self, mundo):
        mundo["activos"] = ["sv"]
        assert alcance.alcance_simulado(None, ADMIN, "gerente_pais", "gt") is None


# ── opciones / etiqueta / validar ──────────────────────────────────────────

def test_etiqueta():
    assert ver_como.etiqueta("gerente_pais", "El Salvador") == "Gerente pais · El Salvador"
    assert ver_como.etiqueta("director_comercial") == "Director comercial"


class TestOpciones:
    def test_roles_de_org_ordenados_y_pais_por_cada_activo(self, mundo):
        ops = ver_como.opciones(None, ADMIN)
        org = [o.valor for o in ops if o.grupo == ver_como.GRUPO_ORG]
        assert org == sorted(politica.ROLES_SIMULABLES - politica.ROLES_PAIS)
        pais = [o.valor for o in ops if o.grupo == ver_como.GRUPO_PAIS]
        assert pais == [f"{r}|{c}" for r in ("equipo_ventas", "gerente_pais") for c in ("sv", "gt", "hn")]
        assert "admin_plataforma" not in {o.valor for o in ops} and "" not in {o.valor for o in ops}
        assert ver_como.Opcion("gerente_pais|sv", "Gerente pais · El Salvador", ver_como.GRUPO_PAIS) in ops

    def test_sin_paises_activos_solo_operador(self, mundo):
        mundo["activos"] = []
        ops = ver_como.opciones(None, ADMIN)
        assert [o.valor for o in ops] == ["operador"]
        assert alcance.alcance_simulado(None, ADMIN, "ceo") is None
        assert alcance.alcance_simulado(None, ADMIN, "operador") is not None

    def test_cada_opcion_produce_un_alcance(self, mundo):
        for o in ver_como.opciones(None, ADMIN):
            rol, cliente = ver_como.validar(None, ADMIN, o.valor)
            assert alcance.alcance_simulado(None, ADMIN, rol, cliente) is not None


class TestValidar:
    def test_validas(self, mundo):
        assert ver_como.validar(None, ADMIN, "ceo") == ("ceo", None)
        assert ver_como.validar(None, ADMIN, "gerente_pais|sv") == ("gerente_pais", "sv")

    @pytest.mark.parametrize("vista", [
        "", None, 7, "Ceo", "CEO", "gerente_pais|SV", " ceo", "ceo ", "gerente pais|sv",
        "gerente_pais|sv|x", "gerente_pais||sv", "gerente_pais|", "|sv", "ceo|sv",
        "gerente_pais|zz", "admin_plataforma", "ceo\x00", "ceo\n", "gerente_pais|s\tv",
        "a" * 101, "gerente_pais|" + "s" * 101, "x" * 5000,
    ])
    def test_invalidas(self, mundo, vista):
        assert ver_como.validar(None, ADMIN, vista) is None

    def test_pais_desactivado_deja_de_ser_valido(self, mundo):
        assert ver_como.validar(None, ADMIN, "equipo_ventas|gt")
        mundo["activos"] = ["sv"]
        assert ver_como.validar(None, ADMIN, "equipo_ventas|gt") is None


# ── iniciar / terminar ─────────────────────────────────────────────────────

class TestIniciar:
    def test_exito_fija_audita_y_confirma(self, mundo):
        conn = Conn()
        assert ver_como.iniciar(conn, ADMIN, "th", "gerente_pais|sv", "ip")
        assert mundo["sesion"] == ("gerente_pais", "sv")
        assert mundo["auditoria"] == [("org", "VER_COMO_INICIO", "admin1", "rol=gerente_pais;cliente=sv", "ip")]
        assert (conn.commits, conn.rollbacks) == (1, 0)

    def test_rol_de_org_audita_cliente_guion(self, mundo):
        assert ver_como.iniciar(Conn(), ADMIN, "th", "operador", None)
        assert mundo["auditoria"][0][3] == "rol=operador;cliente=-"

    def test_vista_invalida_no_toca_nada(self, mundo):
        conn = Conn()
        assert not ver_como.iniciar(conn, ADMIN, "th", "ceo|sv", "ip")
        assert mundo["sesion"] is None and not mundo["auditoria"] and conn.commits == 0

    def test_no_admin_no_inicia(self, mundo):
        conn = Conn()
        operador = dataclasses.replace(ADMIN, roles=frozenset({"operador"}))
        assert not ver_como.iniciar(conn, operador, "th", "ceo", "ip")
        assert mundo["sesion"] is None and conn.commits == 0

    def test_fallo_de_auditoria_hace_rollback(self, mundo):
        mundo["fallar_auditoria"] = True
        conn = Conn()
        assert not ver_como.iniciar(conn, ADMIN, "th", "ceo", "ip")
        assert (conn.commits, conn.rollbacks) == (0, 1)

    def test_sesion_que_no_acepta_hace_rollback_sin_auditar(self, mundo):
        mundo["fijar_ok"] = False
        conn = Conn()
        assert not ver_como.iniciar(conn, ADMIN, "th", "ceo", "ip")
        assert not mundo["auditoria"] and (conn.commits, conn.rollbacks) == (0, 1)


class TestTerminar:
    def test_limpia_audita_y_confirma(self, mundo):
        mundo["sesion"] = ("ceo", None)
        conn = Conn()
        assert ver_como.terminar(conn, ADMIN, "th", "ip")
        assert mundo["sesion"] is None
        assert mundo["auditoria"] == [("org", "VER_COMO_FIN", "admin1", None, "ip")]
        assert (conn.commits, conn.rollbacks) == (1, 0)

    def test_sin_simulacion_no_audita(self, mundo):
        conn = Conn()
        assert not ver_como.terminar(conn, ADMIN, "th", "ip")
        assert not mundo["auditoria"] and conn.commits == 0

    def test_fallo_de_auditoria_hace_rollback(self, mundo):
        mundo["sesion"] = ("ceo", None)
        mundo["fallar_auditoria"] = True
        conn = Conn()
        assert not ver_como.terminar(conn, ADMIN, "th", "ip")
        assert (conn.commits, conn.rollbacks) == (0, 1)

    def test_sin_alcance_no_hace_nada(self, mundo):
        mundo["sesion"] = ("ceo", None)
        assert not ver_como.terminar(Conn(), None, "th", "ip")
        assert mundo["sesion"] == ("ceo", None)
