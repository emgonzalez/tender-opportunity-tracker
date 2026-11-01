"""Entrega 2 de identidad: códigos, sesiones, alcance y casos de uso. Sin Oracle (repo simulado)."""
from __future__ import annotations

import types

import pytest

from src.identidad import alcance, codigos, politica, repo, service, sesiones


class TestCodigos:
    def test_alfabeto_de_32_sin_ambiguos(self):
        assert len(codigos.ALFABETO) == 32 == len(set(codigos.ALFABETO))
        assert not set("01IO") & set(codigos.ALFABETO)

    def test_largo_y_alfabeto(self):
        for _ in range(200):
            c = codigos.generar_codigo()
            assert len(c) == 8 and set(c) <= set(codigos.ALFABETO)

    def test_normaliza_minusculas_espacios_y_guiones(self):
        assert codigos.normalizar_codigo(" k7m2-qx9p ") == "K7M2QX9P"

    def test_hash_depende_del_usuario_y_del_secreto(self):
        h = codigos.hashear_codigo("s", "u1", "K7M2QX9P")
        assert len(h) == 64
        assert h != codigos.hashear_codigo("s", "u2", "K7M2QX9P")
        assert h != codigos.hashear_codigo("otro", "u1", "K7M2QX9P")

    def test_verificar(self):
        h = codigos.hashear_codigo("s", "u1", "K7M2QX9P")
        assert codigos.verificar_codigo("s", "u1", "k7m2 qx9p", h)
        assert not codigos.verificar_codigo("s", "u1", "AAAAAAAA", h)
        assert not codigos.verificar_codigo("s", "u2", "K7M2QX9P", h)  # código de otro usuario
        assert not codigos.verificar_codigo("", "u1", "K7M2QX9P", h)

    def test_sin_secreto_no_emite(self):
        with pytest.raises(ValueError):
            codigos.hashear_codigo("", "u1", "K7M2QX9P")


def test_token_de_sesion_es_opaco_y_su_hash_cabe_en_la_columna():
    t1, t2 = sesiones.nuevo_token(), sesiones.nuevo_token()
    assert t1 != t2 and len(t1) >= 43
    h = sesiones.hash_token(t1)
    assert len(h) == 64 and h != t1 and h == sesiones.hash_token(t1)


@pytest.fixture
def mundo(monkeypatch):
    """Una org con tres países y un repo en memoria para `resolver_alcance`."""
    datos = {"usuario": {"activo": True}, "roles_org": [], "membresias": []}
    monkeypatch.setattr(repo, "obtener_usuario", lambda c, u: (
        {"usuario_id": u, "org_id": "org", "email": "x@y.z", "activo": datos["usuario"]["activo"]}
        if u == "u1" else None))
    monkeypatch.setattr(repo, "roles_org", lambda c, u: datos["roles_org"])
    monkeypatch.setattr(repo, "membresias_pais", lambda c, u: datos["membresias"])
    monkeypatch.setattr(repo, "clientes_activos", lambda c, o: ["sv", "gt", "hn"])
    return datos


class TestAlcance:
    def test_gerente_pais_ve_solo_su_pais_y_cambia_estado(self, mundo):
        mundo["membresias"] = [("sv", "gerente_pais")]
        a = alcance.resolver_alcance(None, "u1")
        assert a.clientes == {"sv"} and a.cambia_estado == {"sv"} and not a.ve_operativo

    def test_ceo_ve_todos_los_paises_incluido_uno_nuevo_pero_solo_lee(self, mundo):
        mundo["roles_org"] = ["ceo"]
        a = alcance.resolver_alcance(None, "u1")
        assert a.clientes == {"sv", "gt", "hn"} and not a.cambia_estado

    def test_director_comercial_cambia_estado_en_todos(self, mundo):
        mundo["roles_org"] = ["director_comercial"]
        a = alcance.resolver_alcance(None, "u1")
        assert a.cambia_estado == a.clientes == {"sv", "gt", "hn"}
        assert a.puede_cambiar_estado("gt") and a.puede_ver("hn") and not a.puede_ver("zz")

    def test_equipo_ventas_cambia_solo_el_suyo(self, mundo):
        mundo["membresias"] = [("gt", "equipo_ventas")]
        a = alcance.resolver_alcance(None, "u1")
        assert a.clientes == a.cambia_estado == {"gt"}

    def test_tecnicos_no_ven_negocio_pero_si_lo_operativo(self, mundo):
        for rol in ("admin_plataforma", "operador"):
            mundo["roles_org"] = [rol]
            a = alcance.resolver_alcance(None, "u1")
            assert a.ve_operativo and not a.ve_negocio and not a.clientes

    def test_tecnico_con_membresia_de_negocio_ve_ese_pais(self, mundo):
        mundo["roles_org"] = ["operador"]
        mundo["membresias"] = [("sv", "equipo_ventas")]
        a = alcance.resolver_alcance(None, "u1")
        assert a.ve_operativo and a.clientes == {"sv"}

    def test_regional_que_ademas_es_gerente_de_pais_queda_como_regional(self, mundo):
        mundo["roles_org"] = ["director_estrategia"]
        mundo["membresias"] = [("sv", "gerente_pais")]
        a = alcance.resolver_alcance(None, "u1")
        assert a.clientes == {"sv", "gt", "hn"} and a.cambia_estado == {"sv"}

    def test_usuario_inexistente_o_de_baja(self, mundo):
        assert alcance.resolver_alcance(None, "nadie") is None
        mundo["usuario"]["activo"] = False
        assert alcance.resolver_alcance(None, "u1") is None


def test_los_roles_de_la_politica_son_los_11_del_ddl():
    ddl = open("db/schema_v2.sql", encoding="utf-8").read()
    assert len(politica.ROLES) == 11
    for rol in politica.ROLES:
        assert f"'{rol}'" in ddl
    assert politica.ROLES_ORG.isdisjoint(politica.ROLES_PAIS)


class _Repo:
    """Repo en memoria con la misma interfaz que usa `service`."""

    def __init__(self):
        self.usuarios = {}      # usuario_id -> dict
        self.membresias, self.membresias_org = [], []
        self.codigos, self.sesiones, self.auditoria = [], {}, []
        self.clientes = {("org", "sv")}
        self.recientes = 0

    def install(self, mp):
        r = self
        mp.setattr(repo, "cliente_existe", lambda c, o, cl: (o, cl) in r.clientes)
        mp.setattr(repo, "obtener_usuario_por_email", lambda c, o, e: next(
            (u for u in r.usuarios.values() if u["org_id"] == o and u["email"] == e.lower()), None))
        mp.setattr(repo, "crear_usuario", lambda c, uid, o, e, n=None: r.usuarios.update(
            {uid: {"usuario_id": uid, "org_id": o, "email": e.lower(), "activo": True}}))
        mp.setattr(repo, "insertar_membresia", lambda c, u, cl, rol: r.membresias.append((u, cl, rol)))
        mp.setattr(repo, "insertar_membresia_org", lambda c, u, o, rol: r.membresias_org.append((u, o, rol)))
        mp.setattr(repo, "registrar_auditoria", lambda c, org, accion, **kw: r.auditoria.append(accion))
        mp.setattr(repo, "desactivar_usuario", lambda c, u: r.usuarios[u].update(activo=False) or True)
        mp.setattr(repo, "revocar_sesiones_de", lambda c, u: 0)
        mp.setattr(repo, "invalidar_codigos_previos", lambda c, u: [x.update(vigente=False) for x in r.codigos])
        mp.setattr(repo, "buscar_usuarios_activos_por_email", lambda c, e: [
            {"usuario_id": u["usuario_id"], "org_id": u["org_id"]}
            for u in r.usuarios.values() if u["email"] == e.lower() and u["activo"]])
        mp.setattr(repo, "invitaciones_pendientes_por_email", lambda c, e: [])  # G1: sin invitaciones
        mp.setattr(repo, "contar_codigos_recientes", lambda c, u, m: r.recientes)
        mp.setattr(repo, "insertar_codigo", lambda c, u, h, ttl: r.codigos.append(
            {"codigo_id": len(r.codigos) + 1, "usuario_id": u, "hash": h, "intentos": 0, "vigente": True}))
        mp.setattr(repo, "obtener_codigo_vigente", lambda c, u: next(
            ({"codigo_id": x["codigo_id"], "codigo_hash": x["hash"], "intentos": x["intentos"]}
             for x in reversed(r.codigos) if x["usuario_id"] == u and x["vigente"]), None))

        def fallo(c, cid, mx):
            x = r.codigos[cid - 1]
            x["intentos"] += 1
            if x["intentos"] >= mx:
                x["vigente"] = False
        mp.setattr(repo, "registrar_intento_fallido", fallo)

        def usado(c, cid):
            x = r.codigos[cid - 1]
            ok, x["vigente"] = x["vigente"], False
            return ok
        mp.setattr(repo, "marcar_codigo_usado", usado)
        mp.setattr(repo, "crear_sesion", lambda c, th, u, ttl, ip=None, ua=None, mfa_ok=True: r.sesiones.update({th: u}))
        mp.setattr(repo, "roles_org", lambda c, u: [rol for (uu, _o, rol) in r.membresias_org if uu == u])
        mp.setattr(repo, "membresias_pais", lambda c, u: [(cl, rol) for (uu, cl, rol) in r.membresias if uu == u])
        mp.setattr(repo, "obtener_usuario_de_sesion", lambda c, th: r.sesiones.get(th))
        mp.setattr(repo, "obtener_usuario", lambda c, u: r.usuarios.get(u))
        mp.setattr(repo, "revocar_sesion", lambda c, th: r.sesiones.pop(th, None) is not None)


@pytest.fixture
def r(monkeypatch):
    mundo = _Repo()
    mundo.install(monkeypatch)
    return mundo


CFG = types.SimpleNamespace(identidad_secret="secreto", codigo_ttl_min=10, sesion_ttl_horas=12)


class TestAlta:
    def test_rol_de_pais_va_a_membresia_y_audita(self, r):
        uid = service.alta_usuario(None, "org", "Ana@Test.com", "gerente_pais", "sv")
        assert r.membresias == [(uid, "sv", "gerente_pais")] and not r.membresias_org
        assert r.usuarios[uid]["email"] == "ana@test.com"
        assert r.auditoria == ["USUARIO_ALTA", "MEMBRESIA_ALTA"]

    def test_rol_de_organizacion_va_a_membresia_org(self, r):
        uid = service.alta_usuario(None, "org", "ceo@test.com", "ceo")
        assert r.membresias_org == [(uid, "org", "ceo")] and not r.membresias

    def test_misma_persona_suma_otro_rol_sin_duplicar_usuario(self, r):
        a = service.alta_usuario(None, "org", "ana@test.com", "gerente_pais", "sv")
        b = service.alta_usuario(None, "org", "ana@test.com", "operador")
        assert a == b and len(r.usuarios) == 1 and r.auditoria.count("USUARIO_ALTA") == 1

    @pytest.mark.parametrize("email,rol,cliente", [
        ("sin-arroba", "ceo", None),
        ("a@b.com", "GP", None),
        ("a@b.com", "gerente_pais", None),          # falta el país
        ("a@b.com", "gerente_pais", "inexistente"),
        ("a@b.com", "ceo", "sv"),                    # rol de org no lleva país
    ])
    def test_rechaza_datos_invalidos_sin_escribir(self, r, email, rol, cliente):
        with pytest.raises(service.IdentidadError):
            service.alta_usuario(None, "org", email, rol, cliente)
        assert not r.usuarios and not r.auditoria

    def test_baja(self, r):
        uid = service.alta_usuario(None, "org", "ana@test.com", "ceo")
        assert service.baja_usuario(None, "org", "ana@test.com") is True
        assert r.usuarios[uid]["activo"] is False and r.auditoria[-1] == "USUARIO_BAJA"
        assert service.baja_usuario(None, "org", "ana@test.com") is False
        assert service.baja_usuario(None, "org", "nadie@test.com") is False


class TestLogin:
    def _usuario(self, r):
        return service.alta_usuario(None, "org", "ana@test.com", "ceo")

    def test_flujo_completo_y_el_codigo_sirve_una_vez(self, r):
        uid = self._usuario(r)
        email, codigo = service.emitir_codigo(None, CFG, "Ana@test.com")
        assert email == "ana@test.com"
        token = service.verificar_codigo(None, CFG, "ana@test.com", codigo.lower())
        assert token and service.usuario_de_sesion(None, token) == uid
        assert r.auditoria[-1] == "LOGIN_OK"
        assert service.verificar_codigo(None, CFG, "ana@test.com", codigo) is None  # ya usado
        assert service.cerrar_sesion(None, token) is True
        assert service.usuario_de_sesion(None, token) is None
        assert r.auditoria[-1] == "SESION_REVOCADA"

    def test_correo_sin_cuenta_o_de_baja_no_emite_nada(self, r):
        assert service.emitir_codigo(None, CFG, "nadie@test.com") is None
        self._usuario(r)
        service.baja_usuario(None, "org", "ana@test.com")
        assert service.emitir_codigo(None, CFG, "ana@test.com") is None
        assert not r.codigos

    def test_limite_de_codigos_por_hora(self, r):
        self._usuario(r)
        r.recientes = politica.MAX_CODIGOS_POR_HORA
        assert service.emitir_codigo(None, CFG, "ana@test.com") is None

    def test_un_codigo_nuevo_invalida_el_anterior(self, r):
        self._usuario(r)
        _, viejo = service.emitir_codigo(None, CFG, "ana@test.com")
        _, nuevo = service.emitir_codigo(None, CFG, "ana@test.com")
        assert service.verificar_codigo(None, CFG, "ana@test.com", viejo) is None
        assert service.verificar_codigo(None, CFG, "ana@test.com", nuevo)

    def test_el_quinto_fallo_invalida_el_codigo_aunque_luego_acierte(self, r):
        self._usuario(r)
        _, codigo = service.emitir_codigo(None, CFG, "ana@test.com")
        for _ in range(politica.MAX_INTENTOS_CODIGO):
            assert service.verificar_codigo(None, CFG, "ana@test.com", "AAAAAAAA") is None
        assert service.verificar_codigo(None, CFG, "ana@test.com", codigo) is None
        assert r.auditoria.count("LOGIN_FALLIDO") == politica.MAX_INTENTOS_CODIGO
        assert r.auditoria[-1] == "CODIGO_VENCIDO"

    def test_codigo_de_otro_usuario_no_entra(self, r):
        self._usuario(r)
        service.alta_usuario(None, "org", "beto@test.com", "operador")
        _, de_ana = service.emitir_codigo(None, CFG, "ana@test.com")
        service.emitir_codigo(None, CFG, "beto@test.com")
        assert service.verificar_codigo(None, CFG, "beto@test.com", de_ana) is None

    def test_el_codigo_no_se_guarda_en_claro(self, r):
        self._usuario(r)
        _, codigo = service.emitir_codigo(None, CFG, "ana@test.com")
        assert codigo not in r.codigos[0]["hash"] and len(r.codigos[0]["hash"]) == 64

    def test_sin_secreto_configurado_falla_ruidoso(self, r):
        self._usuario(r)
        sin = types.SimpleNamespace(identidad_secret="", codigo_ttl_min=10, sesion_ttl_horas=12)
        with pytest.raises(ValueError):
            service.emitir_codigo(None, sin, "ana@test.com")
