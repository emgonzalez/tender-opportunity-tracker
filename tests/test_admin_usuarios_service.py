"""Fase G1 — reglas de administración de usuarios (`service.admin_*`) y SQL de las funciones nuevas de `repo`.

El servicio corre contra un repo en memoria (como `test_identidad_e2.py`); el SQL, contra una conexión mock.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.identidad import alcance as alcance_mod
from src.identidad import politica, repo, service
from src.identidad.alcance import Alcance

ORG, OTRA = "org", "otra"


@pytest.fixture(autouse=True)
def _env_minimo(monkeypatch):
    for var in ("COMPRASAL_EMAIL", "COMPRASAL_PASSWORD", "SMTP_USER", "SMTP_PASSWORD"):
        monkeypatch.setenv(var, "x")


def _alc(uid, roles, org=ORG):
    return Alcance(usuario_id=uid, org_id=org, roles=frozenset(roles), clientes=frozenset(),
                   cambia_estado=frozenset(), ve_operativo=False)


PLATAFORMA = _alc("plat", ("admin_plataforma",))
ADMIN_ORG = _alc("adm", ("admin_organizacion",))


class Mundo:
    """Repo en memoria con la interfaz que usa `service`."""

    def __init__(self):
        self.usuarios, self.org_roles, self.paises = {}, {}, {}
        self.clientes = {(ORG, "sv"), (ORG, "cr"), (OTRA, "gt")}
        self.orgs = {ORG, OTRA}
        self.invitaciones = []
        self.sesiones_revocadas, self.auditoria, self.mfa_reiniciado = [], [], []
        self.mfa = {}  # uid -> {"confirmado", "secreto_cifrado"}
        self.sesion_ok = True

    def agregar(self, uid, email="x@example.com", org=ORG, activo=True, org_rol=None, paises=None):
        self.usuarios[uid] = {"usuario_id": uid, "org_id": org, "email": email, "nombre": "", "activo": activo,
                              "mfa": False, "creado_en": None}
        self.org_roles[uid] = [org_rol] if org_rol else []
        self.paises[uid] = dict(paises or {})
        return uid

    def install(self, mp):
        w = self
        mp.setattr(repo, "obtener_usuario_admin", lambda c, u: dict(w.usuarios[u]) if u in w.usuarios else None)
        mp.setattr(repo, "roles_org", lambda c, u: list(w.org_roles.get(u, [])))
        mp.setattr(repo, "membresias_pais_todas", lambda c, u: sorted(w.paises.get(u, {}).items()))
        mp.setattr(repo, "cliente_existe", lambda c, o, cl: (o, cl) in w.clientes)
        mp.setattr(repo, "organizacion_existe", lambda c, o: o in w.orgs)
        mp.setattr(repo, "obtener_usuario_por_email", lambda c, o, e: next(
            (dict(u) for u in w.usuarios.values() if u["org_id"] == o and u["email"] == e.lower()), None))
        mp.setattr(repo, "crear_usuario", lambda c, uid, o, e, n=None: w.agregar(uid, e.lower(), o) and w.usuarios[uid].update(nombre=n or ""))
        mp.setattr(repo, "insertar_membresia", lambda c, u, cl, rol: w.paises[u].__setitem__(cl, rol))
        mp.setattr(repo, "insertar_membresia_org", lambda c, u, o, rol: w.org_roles[u].append(rol))
        mp.setattr(repo, "cambiar_rol", lambda c, u, cl, rol: w.paises[u].__setitem__(cl, rol) or True)
        mp.setattr(repo, "cambiar_rol_org", lambda c, u, o, rol: w.org_roles.__setitem__(u, [rol]) or True)
        mp.setattr(repo, "baja_membresia", lambda c, u, cl: w.paises[u].pop(cl, None) is not None)
        mp.setattr(repo, "baja_membresia_org", lambda c, u, o: bool(w.org_roles.__setitem__(u, []) or True))
        mp.setattr(repo, "actualizar_nombre", lambda c, u, n: w.usuarios[u].update(nombre=n or "") or True)
        mp.setattr(repo, "desactivar_usuario", lambda c, u: w.usuarios[u].update(activo=False) or True)
        mp.setattr(repo, "reactivar_usuario", lambda c, u: w.usuarios[u].update(activo=True) or True)
        mp.setattr(repo, "revocar_sesiones_de", lambda c, u: w.sesiones_revocadas.append(u) or 1)
        mp.setattr(repo, "invalidar_codigos_previos", lambda c, u: 0)
        mp.setattr(repo, "revocar_sesion_es_de", lambda c, u, p: w.sesion_ok and len(p) == 16)
        mp.setattr(repo, "obtener_mfa", lambda c, u: w.mfa.get(u, {"confirmado": False, "secreto_cifrado": None,
                                                                   "ultimo_paso": None}))
        mp.setattr(repo, "reiniciar_mfa", lambda c, u: w.mfa_reiniciado.append(u) or True)
        mp.setattr(repo, "registrar_auditoria", lambda c, org, accion, **kw: w.auditoria.append((accion, kw)))
        # Invitaciones
        mp.setattr(repo, "crear_invitacion", lambda c, i, o, e, r, p, d: w.invitaciones.append(
            {"invitacion_id": i, "org_id": o, "email": e, "rol_propuesto": r, "estado": "PENDIENTE"}))
        mp.setattr(repo, "invitaciones_pendientes_por_email", lambda c, e: [
            dict(i) for i in w.invitaciones if i["email"] == e.lower() and i["estado"] == "PENDIENTE"])
        mp.setattr(repo, "obtener_invitacion", lambda c, i: next(
            (dict(x) for x in w.invitaciones if x["invitacion_id"] == i), None))

        def cambiar(c, i, desde, hacia):
            for x in w.invitaciones:
                if x["invitacion_id"] == i and x["estado"] == desde:
                    x["estado"] = hacia
                    return True
            return False
        mp.setattr(repo, "cambiar_estado_invitacion", cambiar)


@pytest.fixture
def m(monkeypatch):
    mundo = Mundo()
    mundo.install(monkeypatch)
    mundo.agregar("plat", "plat@example.com", org_rol="admin_plataforma")
    mundo.agregar("adm", "adm@example.com", org_rol="admin_organizacion")
    return mundo


C = None  # la conexión no se usa: todo pasa por el repo en memoria


# ── Alta ───────────────────────────────────────────────────────────────────────

class TestAlta:
    def test_crea_usuario_con_rol_de_organizacion(self, m):
        assert service.admin_crear_usuario(C, ADMIN_ORG, " Ana@Example.com ", "  Ana   Pérez ", "ceo") == service.OK
        u = next(x for x in m.usuarios.values() if x["email"] == "ana@example.com")
        assert u["nombre"] == "Ana Pérez" and u["org_id"] == ORG
        assert m.org_roles[u["usuario_id"]] == ["ceo"]
        assert [a for a, _ in m.auditoria] == ["MEMBRESIA_ALTA"] and m.auditoria[0][1]["usuario_id"] == "adm"

    def test_rol_de_pais_exige_pais_existente_de_la_org(self, m):
        crear = lambda rol, cl: service.admin_crear_usuario(C, ADMIN_ORG, "b@example.com", "", rol, cl)  # noqa: E731
        assert crear("gerente_pais", "") == service.INVALIDO
        assert crear("gerente_pais", "gt") == service.INVALIDO       # de otra organización
        assert crear("ceo", "sv") == service.INVALIDO                # rol de org + país
        assert crear("gerente_pais", "sv") == service.OK

    @pytest.mark.parametrize("email", ["", "sin-arroba", "a@b", "a b@example.com", "x@@example.com"])
    def test_correo_invalido(self, m, email):
        assert service.admin_crear_usuario(C, ADMIN_ORG, email, "", "ceo") == service.INVALIDO

    def test_rol_desconocido_y_nombre_largo(self, m):
        assert service.admin_crear_usuario(C, ADMIN_ORG, "c@example.com", "", "rey") == service.INVALIDO
        assert service.admin_crear_usuario(C, ADMIN_ORG, "c@example.com", "ñ" * 101, "ceo") == service.INVALIDO  # bytes

    def test_org_admin_no_otorga_admin_plataforma_pero_plataforma_si(self, m):
        assert service.admin_crear_usuario(C, ADMIN_ORG, "d@example.com", "", "admin_plataforma") == service.INVALIDO
        assert service.admin_crear_usuario(C, PLATAFORMA, "d@example.com", "", "admin_plataforma") == service.OK

    def test_correo_repetido_es_conflicto(self, m):
        assert service.admin_crear_usuario(C, ADMIN_ORG, "adm@example.com", "", "ceo") == service.CONFLICTO

    def test_solo_plataforma_elige_otra_organizacion(self, m):
        assert service.admin_crear_usuario(C, ADMIN_ORG, "e@example.com", "", "ceo", "", OTRA) == service.INVALIDO
        assert service.admin_crear_usuario(C, PLATAFORMA, "e@example.com", "", "ceo", "", OTRA) == service.OK
        assert service.admin_crear_usuario(C, PLATAFORMA, "f@example.com", "", "ceo", "", "no_existe") == service.INVALIDO


# ── Alcance por organización ───────────────────────────────────────────────────

class TestAlcance:
    def test_lo_de_otra_organizacion_es_no_encontrado_para_org_admin(self, m):
        m.agregar("ajeno", org=OTRA, org_rol="ceo")
        for f in (service.admin_baja_usuario, service.admin_reactivar_usuario, service.admin_quitar_rol_org,
                  service.admin_cerrar_sesiones):
            assert f(C, ADMIN_ORG, "ajeno") == service.NO_ENCONTRADO
        assert service.admin_cambiar_rol(C, ADMIN_ORG, "ajeno", "", "ceo") == service.NO_ENCONTRADO
        assert service.admin_editar_nombre(C, ADMIN_ORG, "ajeno", "x") == service.NO_ENCONTRADO
        assert service.admin_baja_usuario(C, ADMIN_ORG, "no-existe") == service.NO_ENCONTRADO

    def test_plataforma_opera_sobre_todas(self, m):
        m.agregar("ajeno", org=OTRA, org_rol="ceo")
        assert service.admin_baja_usuario(C, PLATAFORMA, "ajeno") == service.OK

    def test_org_admin_no_ve_ni_toca_a_un_admin_plataforma(self, m):
        assert service.admin_baja_usuario(C, ADMIN_ORG, "plat") == service.NO_ENCONTRADO
        assert service.admin_cambiar_rol(C, ADMIN_ORG, "plat", "", "ceo") == service.NO_ENCONTRADO
        assert service.usuario_visible(C, ADMIN_ORG, "plat") is None
        assert service.usuario_visible(C, PLATAFORMA, "adm")["email"] == "adm@example.com"


# ── Baja, reactivación y nombre ────────────────────────────────────────────────

class TestBajaYReactivacion:
    def test_baja_cierra_sesiones_y_reactivar_la_deshace(self, m):
        m.agregar("ana", org_rol="ceo")
        assert service.admin_baja_usuario(C, ADMIN_ORG, "ana") == service.OK
        assert m.usuarios["ana"]["activo"] is False and "ana" in m.sesiones_revocadas
        assert service.admin_baja_usuario(C, ADMIN_ORG, "ana") == service.CONFLICTO
        assert service.admin_reactivar_usuario(C, ADMIN_ORG, "ana") == service.OK
        assert m.usuarios["ana"]["activo"] is True
        assert service.admin_reactivar_usuario(C, ADMIN_ORG, "ana") == service.CONFLICTO

    def test_nadie_se_da_de_baja_a_si_mismo(self, m):
        assert service.admin_baja_usuario(C, ADMIN_ORG, "adm") == service.INVALIDO
        assert m.usuarios["adm"]["activo"] is True

    def test_editar_nombre(self, m):
        assert service.admin_editar_nombre(C, ADMIN_ORG, "adm", "  Eva   Gómez ") == service.OK
        assert m.usuarios["adm"]["nombre"] == "Eva Gómez"
        assert service.admin_editar_nombre(C, ADMIN_ORG, "adm", "") == service.OK and m.usuarios["adm"]["nombre"] == ""
        assert service.admin_editar_nombre(C, ADMIN_ORG, "adm", "x" * 201) == service.INVALIDO


# ── Roles y membresías ─────────────────────────────────────────────────────────

class TestRoles:
    def test_cambia_rol_de_organizacion_y_de_pais(self, m):
        m.agregar("ana", org_rol="ceo", paises={"sv": "equipo_ventas"})
        assert service.admin_cambiar_rol(C, ADMIN_ORG, "ana", "", "gerente_marca_oracle") == service.OK
        assert m.org_roles["ana"] == ["gerente_marca_oracle"]
        assert service.admin_cambiar_rol(C, ADMIN_ORG, "ana", "sv", "gerente_pais") == service.OK
        assert m.paises["ana"] == {"sv": "gerente_pais"}

    def test_validaciones_del_cambio(self, m):
        m.agregar("ana", org_rol="ceo", paises={"sv": "equipo_ventas"})
        assert service.admin_cambiar_rol(C, ADMIN_ORG, "ana", "", "ceo") == service.CONFLICTO
        assert service.admin_cambiar_rol(C, ADMIN_ORG, "ana", "", "gerente_pais") == service.INVALIDO   # de país en org
        assert service.admin_cambiar_rol(C, ADMIN_ORG, "ana", "sv", "ceo") == service.INVALIDO          # de org en país
        assert service.admin_cambiar_rol(C, ADMIN_ORG, "ana", "cr", "gerente_pais") == service.NO_ENCONTRADO
        assert service.admin_cambiar_rol(C, ADMIN_ORG, "ana", "", "admin_plataforma") == service.INVALIDO
        m.agregar("sin", paises={})
        assert service.admin_cambiar_rol(C, ADMIN_ORG, "sin", "", "ceo") == service.NO_ENCONTRADO

    def test_org_admin_no_otorga_admin_plataforma(self, m):
        m.agregar("ana")
        assert service.admin_agregar_rol_org(C, ADMIN_ORG, "ana", "admin_plataforma") == service.INVALIDO
        assert service.admin_agregar_rol_org(C, PLATAFORMA, "ana", "admin_plataforma") == service.OK

    def test_nadie_se_quita_el_rol_admin_a_si_mismo(self, m):
        assert service.admin_cambiar_rol(C, ADMIN_ORG, "adm", "", "ceo") == service.INVALIDO
        assert service.admin_quitar_rol_org(C, ADMIN_ORG, "adm") == service.INVALIDO
        assert m.org_roles["adm"] == ["admin_organizacion"]
        # Cambiar a otro rol de administración sí es posible para plataforma sobre sí misma.
        assert service.admin_cambiar_rol(C, PLATAFORMA, "plat", "", "admin_organizacion") == service.OK

    def test_agregar_y_quitar_rol_de_organizacion(self, m):
        m.agregar("ana")
        assert service.admin_agregar_rol_org(C, ADMIN_ORG, "ana", "ceo") == service.OK
        assert service.admin_agregar_rol_org(C, ADMIN_ORG, "ana", "operador") == service.CONFLICTO  # uno por org
        assert service.admin_agregar_rol_org(C, ADMIN_ORG, "ana", "gerente_pais") == service.INVALIDO
        assert service.admin_quitar_rol_org(C, ADMIN_ORG, "ana") == service.OK
        assert service.admin_quitar_rol_org(C, ADMIN_ORG, "ana") == service.NO_ENCONTRADO

    def test_agregar_y_quitar_pais(self, m):
        m.agregar("ana")
        assert service.admin_agregar_pais(C, ADMIN_ORG, "ana", "sv", "equipo_ventas") == service.OK
        assert service.admin_agregar_pais(C, ADMIN_ORG, "ana", "sv", "gerente_pais") == service.CONFLICTO
        assert service.admin_agregar_pais(C, ADMIN_ORG, "ana", "gt", "equipo_ventas") == service.INVALIDO  # otra org
        assert service.admin_agregar_pais(C, ADMIN_ORG, "ana", "cr", "ceo") == service.INVALIDO
        assert service.admin_quitar_pais(C, ADMIN_ORG, "ana", "sv") == service.OK
        assert service.admin_quitar_pais(C, ADMIN_ORG, "ana", "sv") == service.NO_ENCONTRADO

    def test_un_rol_con_mfa_cierra_las_sesiones_de_quien_lo_recibe(self, m):
        m.agregar("ana", org_rol="ceo")
        service.admin_cambiar_rol(C, PLATAFORMA, "ana", "", "admin_organizacion")
        assert "ana" in m.sesiones_revocadas
        m.sesiones_revocadas.clear()
        service.admin_cambiar_rol(C, PLATAFORMA, "ana", "", "ceo")
        assert m.sesiones_revocadas == []

    def test_restriccion_oracle_se_conserva_con_rol_de_pais(self):
        """E14: rol Oracle de organización + rol de país sigue restringido a Oracle (lo decide `alcance`)."""
        a = alcance_mod._alcance_desde("u", ORG, {"gerente_marca_oracle"}, [("sv", "equipo_ventas")], {"sv", "cr"})
        assert a.solo_marcas == frozenset({"oracle"})


# ── Sesiones y MFA ─────────────────────────────────────────────────────────────

class TestSesionesYMfa:
    def test_revocar_una_sesion_y_todas(self, m):
        m.agregar("ana", org_rol="ceo")
        assert service.admin_revocar_sesion(C, ADMIN_ORG, "ana", "a" * 16) == service.OK
        assert service.admin_revocar_sesion(C, ADMIN_ORG, "ana", "corta") == service.CONFLICTO
        assert service.admin_revocar_sesion(C, ADMIN_ORG, "ajeno", "a" * 16) == service.NO_ENCONTRADO
        assert service.admin_cerrar_sesiones(C, ADMIN_ORG, "ana") == service.OK and "ana" in m.sesiones_revocadas

    def test_reinicio_de_mfa_lo_hace_otro_admin_plataforma(self, m):
        m.agregar("otro", "otro@example.com", org_rol="admin_plataforma")
        m.mfa["otro"] = {"confirmado": True, "secreto_cifrado": "c"}
        assert service.admin_reiniciar_mfa(C, PLATAFORMA, "otro") == service.OK
        assert m.mfa_reiniciado == ["otro"] and "otro" in m.sesiones_revocadas

    def test_nunca_a_si_mismo(self, m):
        m.mfa["plat"] = {"confirmado": True, "secreto_cifrado": "c"}
        assert service.admin_reiniciar_mfa(C, PLATAFORMA, "plat") == service.INVALIDO
        assert m.mfa_reiniciado == []

    def test_un_admin_organizacion_no_reinicia(self, m):
        m.agregar("otro", org_rol="admin_organizacion")
        m.mfa["otro"] = {"confirmado": True, "secreto_cifrado": "c"}
        assert service.admin_reiniciar_mfa(C, ADMIN_ORG, "otro") == service.NO_ENCONTRADO
        assert m.mfa_reiniciado == []

    def test_solo_a_quien_exige_mfa_y_esta_enrolado(self, m):
        m.agregar("ana", org_rol="ceo")
        m.mfa["ana"] = {"confirmado": True, "secreto_cifrado": "c"}
        assert service.admin_reiniciar_mfa(C, PLATAFORMA, "ana") == service.INVALIDO
        m.agregar("nuevo", org_rol="admin_organizacion")
        assert service.admin_reiniciar_mfa(C, PLATAFORMA, "nuevo") == service.CONFLICTO   # nada que reiniciar
        m.mfa["adm"] = {"confirmado": True, "secreto_cifrado": "c"}
        assert service.admin_reiniciar_mfa(C, PLATAFORMA, "adm") == service.OK


# ── Invitaciones ───────────────────────────────────────────────────────────────

class TestInvitaciones:
    def test_crear_revocar(self, m):
        assert service.admin_invitar(C, ADMIN_ORG, "Nuevo@Example.com", "equipo_ventas", "sv") == service.OK
        inv = m.invitaciones[0]
        assert inv["email"] == "nuevo@example.com" and inv["rol_propuesto"] == "equipo_ventas:sv"
        assert service.admin_invitar(C, ADMIN_ORG, "nuevo@example.com", "equipo_ventas", "sv") == service.CONFLICTO
        assert service.admin_revocar_invitacion(C, ADMIN_ORG, inv["invitacion_id"]) == service.OK
        assert service.admin_revocar_invitacion(C, ADMIN_ORG, inv["invitacion_id"]) == service.CONFLICTO
        assert service.admin_revocar_invitacion(C, ADMIN_ORG, "no-existe") == service.NO_ENCONTRADO

    def test_validaciones(self, m):
        assert service.admin_invitar(C, ADMIN_ORG, "adm@example.com", "ceo") == service.CONFLICTO   # ya tiene cuenta
        assert service.admin_invitar(C, ADMIN_ORG, "a@example.com", "admin_plataforma") == service.INVALIDO
        assert service.admin_invitar(C, ADMIN_ORG, "a@example.com", "gerente_pais", "") == service.INVALIDO
        assert service.admin_invitar(C, ADMIN_ORG, "mal", "ceo") == service.INVALIDO

    def test_el_rol_propuesto_cabe_en_40_bytes(self, m):
        m.clientes.add((ORG, "c" * 30))
        assert service.admin_invitar(C, ADMIN_ORG, "a@example.com", "equipo_ventas", "c" * 30) == service.INVALIDO

    def test_lo_de_otra_organizacion_es_404_para_org_admin(self, m):
        m.invitaciones.append({"invitacion_id": "i1", "org_id": OTRA, "email": "z@example.com",
                               "rol_propuesto": "ceo", "estado": "PENDIENTE"})
        assert service.admin_revocar_invitacion(C, ADMIN_ORG, "i1") == service.NO_ENCONTRADO
        assert service.admin_revocar_invitacion(C, PLATAFORMA, "i1") == service.OK

    def test_canje_crea_la_cuenta_con_el_rol_propuesto(self, m):
        service.admin_invitar(C, ADMIN_ORG, "nuevo@example.com", "equipo_ventas", "sv")
        service.admin_invitar(C, ADMIN_ORG, "nuevo@example.com", "director_comercial")
        r = service.canjear_invitacion(C, "NUEVO@example.com")
        assert r and r["org_id"] == ORG
        uid = r["usuario_id"]
        assert m.paises[uid] == {"sv": "equipo_ventas"} and m.org_roles[uid] == ["director_comercial"]
        assert {i["estado"] for i in m.invitaciones} == {"ACEPTADA"}
        acciones = [a for a, _ in m.auditoria]
        assert acciones.count("INVITACION_USADA") == 2 and "USUARIO_ALTA" in acciones
        assert service.canjear_invitacion(C, "nuevo@example.com") is None     # ya no hay pendientes

    def test_canje_no_resucita_a_quien_ya_tiene_cuenta(self, m):
        m.agregar("ana", "ana@example.com", activo=False)
        m.invitaciones.append({"invitacion_id": "i1", "org_id": ORG, "email": "ana@example.com",
                               "rol_propuesto": "ceo", "estado": "PENDIENTE"})
        assert service.canjear_invitacion(C, "ana@example.com") is None
        assert m.usuarios["ana"]["activo"] is False

    def test_canje_ambiguo_entre_organizaciones_no_hace_nada(self, m):
        for org in (ORG, OTRA):
            m.invitaciones.append({"invitacion_id": f"i-{org}", "org_id": org, "email": "z@example.com",
                                   "rol_propuesto": "ceo", "estado": "PENDIENTE"})
        assert service.canjear_invitacion(C, "z@example.com") is None

    def test_canje_deja_pendiente_lo_que_ya_no_es_asignable(self, m):
        m.invitaciones.append({"invitacion_id": "i1", "org_id": ORG, "email": "z@example.com",
                               "rol_propuesto": "equipo_ventas:borrado", "estado": "PENDIENTE"})
        assert service.canjear_invitacion(C, "z@example.com") is None
        assert m.invitaciones[0]["estado"] == "PENDIENTE"

    def test_emitir_codigo_canjea_la_invitacion_antes_de_emitir(self, m, monkeypatch):
        service.admin_invitar(C, ADMIN_ORG, "nuevo@example.com", "ceo")
        emitidos = []
        monkeypatch.setattr(repo, "buscar_usuarios_activos_por_email", lambda c, e: [
            {"usuario_id": u["usuario_id"], "org_id": u["org_id"]} for u in m.usuarios.values()
            if u["email"] == e and u["activo"]])
        monkeypatch.setattr(repo, "contar_codigos_recientes", lambda c, u, mi: 0)
        monkeypatch.setattr(repo, "insertar_codigo", lambda c, u, h, ttl: emitidos.append(u))
        cfg = MagicMock(identidad_secret="s", codigo_ttl_min=10)
        resultado = service.emitir_codigo(C, cfg, "nuevo@example.com")
        assert resultado and resultado[0] == "nuevo@example.com" and len(emitidos) == 1


# ── SQL de las funciones nuevas del repo ───────────────────────────────────────

@pytest.fixture
def cursor():
    return MagicMock()


@pytest.fixture
def conn(cursor):
    c = MagicMock()
    c.cursor.return_value = cursor
    return c


class TestSqlRepo:
    def test_cambiar_rol_actualiza_la_membresia(self, conn, cursor):
        cursor.rowcount = 1
        assert repo.cambiar_rol(conn, "u1", "sv", "gerente_pais") is True
        sql, binds = cursor.execute.call_args[0]
        assert sql.startswith("UPDATE membresia SET rol = :r") and "cliente_id = :c" in sql
        assert binds == {"r": "gerente_pais", "u": "u1", "c": "sv"}

    def test_cambiar_rol_org_y_bajas(self, conn, cursor):
        cursor.rowcount = 1
        assert repo.cambiar_rol_org(conn, "u1", "org", "ceo") is True
        assert "UPDATE membresia_org SET rol = :r" in cursor.execute.call_args[0][0]
        assert repo.baja_membresia(conn, "u1", "sv") is True
        assert cursor.execute.call_args[0][0].startswith("DELETE FROM membresia WHERE")
        assert repo.baja_membresia_org(conn, "u1", "org") is True
        assert cursor.execute.call_args[0][0].startswith("DELETE FROM membresia_org WHERE")
        cursor.rowcount = 0
        assert repo.baja_membresia(conn, "u1", "sv") is False

    def test_reactivar_solo_si_estaba_de_baja(self, conn, cursor):
        cursor.rowcount = 1
        assert repo.reactivar_usuario(conn, "u1") is True
        sql, _ = cursor.execute.call_args[0]
        assert "SET activo = 1" in sql and "activo = 0" in sql

    def test_reiniciar_mfa_borra_secreto_y_recuperacion(self, conn, cursor):
        cursor.rowcount = 1
        assert repo.reiniciar_mfa(conn, "u1") is True
        sqls = [c[0][0] for c in cursor.execute.call_args_list]
        assert "mfa_totp_secret = NULL" in sqls[0] and "mfa_confirmado_en = NULL" in sqls[0]
        assert "mfa_ultimo_paso = NULL" in sqls[0]
        assert sqls[1].startswith("DELETE FROM recuperacion_mfa") and len(sqls) == 2

    def test_revocar_sesion_por_prefijo_exige_uno_solo_y_hex(self, conn, cursor):
        assert repo.revocar_sesion_es_de(conn, "u1", "xyz") is False
        assert repo.revocar_sesion_es_de(conn, "u1", "G" * 16) is False
        cursor.execute.assert_not_called()
        cursor.fetchone.return_value = (2,)
        assert repo.revocar_sesion_es_de(conn, "u1", "a" * 16) is False        # ambiguo: no toca nada
        cursor.fetchone.return_value = (1,)
        cursor.rowcount = 1
        assert repo.revocar_sesion_es_de(conn, "u1", "a" * 16) is True
        sql, binds = cursor.execute.call_args[0]
        assert sql.startswith("UPDATE sesion SET revocada = 1") and "usuario_id = :u" in sql
        assert binds == {"u": "u1", "n": 16, "p": "a" * 16}

    def test_listar_sesiones_solo_vigentes_y_sin_el_hash_completo(self, conn, cursor):
        cursor.fetchall.return_value = [("a" * 16, None, None, 1, None)]
        out = repo.listar_sesiones_vigentes(conn, "u1")
        sql, binds = cursor.execute.call_args[0]
        assert "SUBSTR(sesion_id, 1, :n)" in sql and "revocada = 0" in sql and "expira_en > SYSTIMESTAMP" in sql
        assert binds == {"n": 16, "u": "u1"} and out[0]["id"] == "a" * 16 and out[0]["mfa_ok"] is True

    def test_invitacion_pendiente_vencida_se_lista_como_expirada(self, conn, cursor):
        cursor.fetchall.return_value = []
        repo.listar_invitaciones(conn, "org")
        sql, binds = cursor.execute.call_args[0]
        assert "THEN 'EXPIRADA'" in sql and "WHERE org_id = :o" in sql and binds == {"o": "org"}
        repo.listar_invitaciones(conn, None)
        assert "WHERE" not in cursor.execute.call_args[0][0]

    def test_crear_invitacion_con_vencimiento_en_la_base(self, conn, cursor):
        repo.crear_invitacion(conn, "i1", "org", "A@Example.com", "ceo", "u1", 7)
        sql, binds = cursor.execute.call_args[0]
        assert "SYSTIMESTAMP + NUMTODSINTERVAL(:d, 'DAY')" in sql
        assert binds == {"i": "i1", "o": "org", "e": "a@example.com", "r": "ceo", "p": "u1", "d": 7}

    def test_cambio_de_estado_es_atomico_y_aceptar_exige_vigencia(self, conn, cursor):
        cursor.rowcount = 1
        assert repo.cambiar_estado_invitacion(conn, "i1", "PENDIENTE", "ACEPTADA") is True
        sql, _ = cursor.execute.call_args[0]
        assert "AND estado = :d" in sql and "expira_en > SYSTIMESTAMP" in sql
        repo.cambiar_estado_invitacion(conn, "i1", "PENDIENTE", "REVOCADA")
        assert "expira_en" not in cursor.execute.call_args[0][0].split("WHERE")[1]

    def test_roles_de_usuarios_incluye_paises_dados_de_baja(self, conn, cursor):
        cursor.fetchall.side_effect = [[("u1", "ceo")], [("u1", "sv", "equipo_ventas"), ("u2", "cr", "gerente_pais")]]
        out = repo.roles_de_usuarios(conn, "org")
        assert out == {"u1": {"org": ["ceo"], "pais": [("sv", "equipo_ventas")]},
                       "u2": {"org": [], "pais": [("cr", "gerente_pais")]}}
        assert all("activo" not in c[0][0] for c in cursor.execute.call_args_list)

    def test_las_acciones_que_emite_g1_estan_auditadas(self):
        usadas = {"USUARIO_ALTA", "USUARIO_BAJA", "USUARIO_REACTIVADO", "ROL_CAMBIADO", "MEMBRESIA_ALTA",
                  "MEMBRESIA_BAJA", "INVITACION_CREADA", "INVITACION_REVOCADA", "INVITACION_USADA",
                  "SESION_REVOCADA_POR_ADMIN", "MFA_REINICIADO"}
        assert usadas <= politica.ACCIONES_AUDITADAS
