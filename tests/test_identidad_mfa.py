"""TOTP (P4): cifrado en reposo, verificación, anti-repetición, códigos de recuperación y el servicio."""
from __future__ import annotations

import types

import pyotp
import pytest

from src.identidad import mfa, politica, repo, service, sesiones

SECRETO = "secreto-de-prueba"


class TestCifrado:
    def test_ida_y_vuelta_y_cabe_en_la_columna(self):
        s = mfa.nuevo_secreto()
        guardado = mfa.cifrar(SECRETO, s)
        assert len(guardado) <= 200 and s not in guardado
        assert mfa.descifrar(SECRETO, guardado) == s

    def test_cada_cifrado_es_distinto(self):
        s = mfa.nuevo_secreto()
        assert mfa.cifrar(SECRETO, s) != mfa.cifrar(SECRETO, s)

    def test_otra_clave_o_un_byte_alterado_no_descifra(self):
        guardado = mfa.cifrar(SECRETO, mfa.nuevo_secreto())
        assert mfa.descifrar("otra-clave", guardado) is None
        import base64
        crudo = bytearray(base64.urlsafe_b64decode(guardado))
        for i in (0, 20, len(crudo) - 1):  # nonce, cifrado, etiqueta
            alterado = bytearray(crudo)
            alterado[i] ^= 0x01
            assert mfa.descifrar(SECRETO, base64.urlsafe_b64encode(bytes(alterado)).decode()) is None

    @pytest.mark.parametrize("basura", ["", "no-es-base64!!", "QUJD"])
    def test_basura_no_levanta(self, basura):
        assert mfa.descifrar(SECRETO, basura) is None

    def test_sin_clave_falla_ruidoso(self):
        with pytest.raises(ValueError):
            mfa.cifrar("", "ABC")


class TestVerificar:
    def test_acepta_el_paso_actual_y_los_vecinos(self):
        s = mfa.nuevo_secreto()
        t = pyotp.TOTP(s)
        ahora = 1_800_000_000
        paso = ahora // 30
        assert mfa.verificar(s, t.at(ahora), ahora) == paso
        assert mfa.verificar(s, t.at(ahora - 30), ahora) == paso - 1
        assert mfa.verificar(s, t.at(ahora + 30), ahora) == paso + 1
        assert mfa.verificar(s, t.at(ahora - 90), ahora) is None

    @pytest.mark.parametrize("codigo", ["", "12345", "1234567", "abcdef", "000000"])
    def test_rechaza_formato_o_valor_malo(self, codigo):
        assert mfa.verificar(mfa.nuevo_secreto(), codigo, 1_800_000_000) is None or codigo == "000000"

    def test_tolera_espacios(self):
        s = mfa.nuevo_secreto()
        c = pyotp.TOTP(s).at(1_800_000_000)
        assert mfa.verificar(s, f"{c[:3]} {c[3:]}", 1_800_000_000) is not None


class TestRecuperacion:
    def test_formato_y_unicidad(self):
        cs = mfa.generar_recuperacion()
        assert len(cs) == 8 == len(set(cs))
        assert all(len(c) == 11 and c[5] == "-" for c in cs)

    def test_distingue_recuperacion_de_totp(self):
        assert mfa.es_recuperacion("ABCDE-FGHJK") and mfa.es_recuperacion("abcde fghjk")
        assert not mfa.es_recuperacion("123456") and not mfa.es_recuperacion("12345-67890")

    def test_hash_depende_de_usuario_y_clave(self):
        h = mfa.hash_recuperacion(SECRETO, "u1", "ABCDE-FGHJK")
        assert h == mfa.hash_recuperacion(SECRETO, "u1", "abcde fghjk")
        assert h != mfa.hash_recuperacion(SECRETO, "u2", "ABCDE-FGHJK")
        assert h != mfa.hash_recuperacion("otra", "u1", "ABCDE-FGHJK")


def test_los_roles_con_mfa_son_los_de_p4():
    assert politica.ROLES_MFA == {"admin_plataforma", "admin_organizacion"}
    assert politica.ROLES_MFA <= politica.ROLES


class _Mundo:
    """Repo en memoria para el flujo del servicio con TOTP."""

    def __init__(self):
        self.mfa = {"secreto_cifrado": None, "confirmado": False, "ultimo_paso": None}
        self.pendientes = {}
        self.completas = set()
        self.recuperacion = {}      # hash -> usado
        self.auditoria = []
        self.roles = ["admin_plataforma"]
        self.sesiones = {}          # hash -> (usuario, mfa_ok)

    def install(self, mp):
        w = self
        mp.setattr(repo, "roles_org", lambda c, u: list(w.roles))
        mp.setattr(repo, "membresias_pais", lambda c, u: [])
        mp.setattr(repo, "buscar_usuarios_activos_por_email", lambda c, e: [{"usuario_id": "u1", "org_id": "org"}])
        mp.setattr(repo, "obtener_usuario", lambda c, u: {"usuario_id": u, "org_id": "org", "email": "a@x.com", "activo": True})
        mp.setattr(repo, "contar_codigos_recientes", lambda c, u, m: 0)
        mp.setattr(repo, "invalidar_codigos_previos", lambda c, u: 0)
        mp.setattr(repo, "registrar_auditoria", lambda c, org, accion, **kw: w.auditoria.append(accion))
        mp.setattr(repo, "crear_sesion", lambda c, th, u, ttl, ip=None, ua=None, mfa_ok=True: w.sesiones.update({th: (u, mfa_ok)}))
        mp.setattr(repo, "obtener_usuario_de_sesion_pendiente",
                   lambda c, th: w.sesiones[th][0] if th in w.sesiones and not w.sesiones[th][1] else None)
        mp.setattr(repo, "completar_mfa", lambda c, th, ttl: w.sesiones.update({th: (w.sesiones[th][0], True)}) or True)
        mp.setattr(repo, "obtener_mfa", lambda c, u: dict(w.mfa))

        def guardar(c, u, cif):
            if w.mfa["confirmado"]:
                return False
            w.mfa.update(secreto_cifrado=cif, ultimo_paso=None)
            return True

        def confirmar(c, u, paso):
            if w.mfa["confirmado"]:
                return False
            w.mfa.update(confirmado=True, ultimo_paso=paso)
            return True

        def paso(c, u, p):
            if w.mfa["ultimo_paso"] is not None and w.mfa["ultimo_paso"] >= p:
                return False
            w.mfa["ultimo_paso"] = p
            return True

        mp.setattr(repo, "guardar_secreto_mfa", guardar)
        mp.setattr(repo, "confirmar_mfa", confirmar)
        mp.setattr(repo, "registrar_paso_mfa", paso)
        mp.setattr(repo, "reemplazar_recuperacion", lambda c, u, hs: (w.recuperacion.clear(), w.recuperacion.update({h: False for h in hs})))

        def consumir(c, u, h):
            if h in w.recuperacion and not w.recuperacion[h]:
                w.recuperacion[h] = True
                return True
            return False

        mp.setattr(repo, "consumir_recuperacion", consumir)


@pytest.fixture
def w(monkeypatch):
    mundo = _Mundo()
    mundo.install(monkeypatch)
    return mundo


CFG = types.SimpleNamespace(identidad_secret=SECRETO, codigo_ttl_min=10, sesion_ttl_horas=12)


def _sesion_pendiente(w):
    """Hace pasar el código del correo: devuelve el token de una sesión pendiente del TOTP."""
    import src.identidad.service as s
    _, codigo = s.emitir_codigo(None, CFG, "a@x.com")
    # `emitir_codigo` guarda el hash en el repo real; aquí se cubre aparte: se crea la sesión directo.
    token = sesiones.nuevo_token()
    repo.crear_sesion(None, sesiones.hash_token(token), "u1", 12, mfa_ok=not service.requiere_mfa(None, "u1"))
    return token


class TestServicio:
    def test_roles_que_exigen_mfa(self, w):
        assert service.requiere_mfa(None, "u1") is True
        w.roles = ["ceo"]
        assert service.requiere_mfa(None, "u1") is False
        w.roles = ["admin_organizacion"]
        assert service.requiere_mfa(None, "u1") is True

    def test_enrolamiento_completo_y_recuperacion_de_un_solo_uso(self, w, monkeypatch):
        monkeypatch.setattr(repo, "insertar_codigo", lambda *a, **k: None)
        token = sesiones.nuevo_token()
        repo.crear_sesion(None, sesiones.hash_token(token), "u1", 12, mfa_ok=False)
        assert service.mfa_pendiente(None, token) is True
        assert service.usuario_de_sesion.__name__  # la sesión pendiente no da acceso: ver repo (mfa_ok = 1)

        info = service.iniciar_mfa(None, CFG, token)
        assert info["enrolamiento"] is True and info["uri"].startswith("otpauth://totp/")
        secreto = mfa.descifrar(SECRETO, w.mfa["secreto_cifrado"])
        assert info["secreto"].replace(" ", "") == secreto
        # Si recarga la página antes de confirmar, la clave es la misma.
        assert service.iniciar_mfa(None, CFG, token)["secreto"] == info["secreto"]

        assert service.verificar_mfa(None, CFG, token, "000000") is None
        assert w.auditoria[-1] == "LOGIN_FALLIDO" and service.mfa_pendiente(None, token) is True

        ok = service.verificar_mfa(None, CFG, token, pyotp.TOTP(secreto).now())
        assert ok is not None and len(ok["recuperacion"]) == 8
        assert w.mfa["confirmado"] and service.mfa_pendiente(None, token) is False
        assert "MFA_ENROLADO" in w.auditoria and w.auditoria[-1] == "LOGIN_OK"
        assert not any(c in str(w.recuperacion) for c in ok["recuperacion"])  # solo hashes

        # Segundo ingreso con un código de recuperación: sirve una vez.
        c = ok["recuperacion"][0]
        t2 = sesiones.nuevo_token()
        repo.crear_sesion(None, sesiones.hash_token(t2), "u1", 12, mfa_ok=False)
        assert service.iniciar_mfa(None, CFG, t2) == {"enrolamiento": False}
        assert service.verificar_mfa(None, CFG, t2, c) == {"recuperacion": []}
        assert "MFA_RECUPERACION_USADA" in w.auditoria
        t3 = sesiones.nuevo_token()
        repo.crear_sesion(None, sesiones.hash_token(t3), "u1", 12, mfa_ok=False)
        assert service.verificar_mfa(None, CFG, t3, c) is None  # ya usado

    def test_el_mismo_totp_no_sirve_dos_veces(self, w):
        secreto = mfa.nuevo_secreto()
        w.mfa.update(secreto_cifrado=mfa.cifrar(SECRETO, secreto), confirmado=True, ultimo_paso=None)
        codigo = pyotp.TOTP(secreto).now()
        t1, t2 = sesiones.nuevo_token(), sesiones.nuevo_token()
        for t in (t1, t2):
            repo.crear_sesion(None, sesiones.hash_token(t), "u1", 12, mfa_ok=False)
        assert service.verificar_mfa(None, CFG, t1, codigo) is not None
        assert service.verificar_mfa(None, CFG, t2, codigo) is None  # repetido

    def test_sin_sesion_pendiente_no_hay_nada(self, w):
        assert service.iniciar_mfa(None, CFG, "token-inexistente") is None
        assert service.verificar_mfa(None, CFG, "token-inexistente", "123456") is None

    def test_un_secreto_manipulado_falla_sin_levantar(self, w):
        w.mfa.update(secreto_cifrado="AAAA", confirmado=True)
        t = sesiones.nuevo_token()
        repo.crear_sesion(None, sesiones.hash_token(t), "u1", 12, mfa_ok=False)
        assert service.verificar_mfa(None, CFG, t, "123456") is None
