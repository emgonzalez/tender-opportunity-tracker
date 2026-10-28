"""Entrega 5: pipeline comercial — transiciones y servicio (repo simulado, sin Oracle)."""
from __future__ import annotations

from decimal import Decimal

import pytest

from src.oportunidades import politica, repo, service


class TestPolitica:
    def test_el_camino_feliz(self):
        assert politica.siguientes("NUEVO") == ["EVALUANDO", "DESCARTADO"]
        assert politica.siguientes("EVALUANDO") == ["OFERTADO", "DESCARTADO"]
        assert politica.siguientes("OFERTADO") == ["GANADO", "PERDIDO", "DESCARTADO"]

    def test_los_finales_se_corrigen_y_lo_descartado_se_reabre(self):
        assert politica.siguientes("GANADO") == ["PERDIDO", "DESCARTADO"]
        assert politica.siguientes("PERDIDO") == ["GANADO", "DESCARTADO"]
        assert politica.siguientes("DESCARTADO") == ["EVALUANDO"]

    @pytest.mark.parametrize("anterior,nuevo", [
        ("NUEVO", "OFERTADO"), ("NUEVO", "GANADO"), ("EVALUANDO", "GANADO"), ("OFERTADO", "EVALUANDO"),
        ("GANADO", "EVALUANDO"), ("NUEVO", "NUEVO"), ("DESCARTADO", "GANADO"), ("NUEVO", "INVENTADO"),
    ])
    def test_saltos_y_regresos_no_validos(self, anterior, nuevo):
        with pytest.raises(politica.TransicionInvalida):
            politica.validar(anterior, nuevo, "motivo")

    @pytest.mark.parametrize("anterior,nuevo,exige", [
        ("NUEVO", "EVALUANDO", False), ("OFERTADO", "GANADO", False),
        ("NUEVO", "DESCARTADO", True), ("OFERTADO", "DESCARTADO", True),
        ("GANADO", "PERDIDO", True), ("DESCARTADO", "EVALUANDO", True),
    ])
    def test_motivo_obligatorio_al_descartar_y_al_corregir(self, anterior, nuevo, exige):
        assert politica.exige_motivo(anterior, nuevo) is exige
        if exige:
            with pytest.raises(politica.TransicionInvalida, match="motivo"):
                politica.validar(anterior, nuevo, "  ")
        politica.validar(anterior, nuevo, "porque sí")

    def test_los_estados_son_los_del_ddl(self):
        ddl = open("db/schema_v2.sql", encoding="utf-8").read()
        for e in politica.ESTADOS:
            assert f"'{e}'" in ddl
        assert set(politica.TRANSICIONES) == set(politica.ESTADOS)


class _Repo:
    def __init__(self, existente=None, visible=True, crea=True, actualiza=True):
        self.existente, self.visible, self.crea, self.actualiza = existente, visible, crea, actualiza
        self.escrituras, self.eventos, self.auditoria = [], [], []

    def install(self, mp):
        r = self
        mp.setattr(repo, "proceso_visible", lambda c, cl, g, **k: r.visible)
        mp.setattr(repo, "obtener", lambda c, cl, g, **k: r.existente)
        mp.setattr(repo, "crear_estado", lambda c, cl, g, e, u, m, mo, co: r.escrituras.append(("crear", e, m, mo, co)) or r.crea)
        mp.setattr(repo, "actualizar_estado", lambda c, cl, g, t, e, u, m, mo, co: r.escrituras.append(("actualizar", t, e, m)) or r.actualiza)
        mp.setattr(repo, "insertar_evento", lambda c, cl, g, a, n, u, nota: r.eventos.append((a, n, u, nota)))
        mp.setattr(service.identidad, "registrar_auditoria", lambda c, org, accion, **kw: r.auditoria.append((accion, kw)))


def _alcance(cambia=("cli",), clientes=("cli",)):
    from src.identidad.alcance import Alcance
    return Alcance(usuario_id="u1", org_id="org", roles=frozenset({"gerente_pais"}), clientes=frozenset(clientes),
                   cambia_estado=frozenset(cambia), ve_operativo=False)


def _usar(monkeypatch, **kw):
    r = _Repo(**kw)
    r.install(monkeypatch)
    return r


class TestServicio:
    def test_primera_captura_crea_estado_evento_y_auditoria(self, monkeypatch):
        r = _usar(monkeypatch)
        res = service.cambiar_estado(None, _alcance(), "cli", "p:1", "EVALUANDO", "", competidor=" ACME ")
        assert res == service.OK
        assert r.escrituras == [("crear", "EVALUANDO", None, None, "ACME")]
        assert r.eventos == [("NUEVO", "EVALUANDO", "u1", None)]  # el estado anterior implícito es NUEVO
        accion, kw = r.auditoria[0]
        assert accion == "ESTADO_CAMBIADO" and kw["recurso"] == "p:1:NUEVO>EVALUANDO"
        assert kw["alcance"] == {"cliente_id": "cli"} and kw["usuario_id"] == "u1"

    def test_el_evento_lleva_el_estado_anterior_real(self, monkeypatch):
        r = _usar(monkeypatch, existente={"estado": "OFERTADO", "token": "T1"})
        assert service.cambiar_estado(None, _alcance(), "cli", "p:1", "GANADO", "T1",
                                      monto=Decimal("10.5")) == service.OK
        assert r.escrituras[0][:3] == ("actualizar", "T1", "GANADO")
        assert r.eventos == [("OFERTADO", "GANADO", "u1", None)]

    def test_transicion_invalida_o_sin_motivo_no_escribe_nada(self, monkeypatch):
        r = _usar(monkeypatch, existente={"estado": "NUEVO", "token": "T"})
        assert service.cambiar_estado(None, _alcance(), "cli", "p:1", "GANADO", "T") == service.INVALIDO
        assert service.cambiar_estado(None, _alcance(), "cli", "p:1", "DESCARTADO", "T", motivo=" ") == service.INVALIDO
        assert r.escrituras == r.eventos == r.auditoria == []

    def test_descartar_con_motivo(self, monkeypatch):
        r = _usar(monkeypatch, existente={"estado": "EVALUANDO", "token": "T"})
        assert service.cambiar_estado(None, _alcance(), "cli", "p:1", "DESCARTADO", "T",
                                      motivo="fuera de portafolio") == service.OK
        assert r.eventos[0][3] == "fuera de portafolio"

    def test_conflicto_si_el_token_ya_no_coincide(self, monkeypatch):
        r = _usar(monkeypatch, existente={"estado": "EVALUANDO", "token": "NUEVO-TOKEN"})
        assert service.cambiar_estado(None, _alcance(), "cli", "p:1", "OFERTADO", "TOKEN-VIEJO") == service.CONFLICTO
        assert r.escrituras == [] and r.eventos == []

    def test_conflicto_si_alguien_creo_la_fila_antes(self, monkeypatch):
        r = _usar(monkeypatch, crea=False)
        assert service.cambiar_estado(None, _alcance(), "cli", "p:1", "EVALUANDO", "") == service.CONFLICTO
        assert r.eventos == []

    def test_conflicto_si_el_update_condicionado_no_toca_filas(self, monkeypatch):
        r = _usar(monkeypatch, existente={"estado": "NUEVO", "token": "T"}, actualiza=False)
        assert service.cambiar_estado(None, _alcance(), "cli", "p:1", "EVALUANDO", "T") == service.CONFLICTO
        assert r.eventos == [] and r.auditoria == []

    def test_sin_fila_pero_con_token_es_conflicto(self, monkeypatch):
        _usar(monkeypatch)
        assert service.cambiar_estado(None, _alcance(), "cli", "p:1", "EVALUANDO", "T-fantasma") == service.CONFLICTO

    @pytest.mark.parametrize("alcance,visible", [
        (_alcance(cambia=()), True),                 # puede verlo, no cambiarlo
        (_alcance(clientes=("otro",), cambia=("otro",)), True),   # país ajeno
        (_alcance(), False),                         # proceso que ese cliente no tiene (IDOR)
    ])
    def test_ajeno_solo_lectura_o_proceso_ajeno_es_no_encontrado(self, monkeypatch, alcance, visible):
        r = _usar(monkeypatch, visible=visible)
        assert service.cambiar_estado(None, alcance, "cli", "p:1", "EVALUANDO", "") == service.NO_ENCONTRADO
        assert r.escrituras == r.eventos == r.auditoria == []

    def test_si_falla_el_evento_la_excepcion_sube_para_el_rollback(self, monkeypatch):
        _usar(monkeypatch)

        def boom(*a, **k):
            raise RuntimeError("evento no escrito")

        monkeypatch.setattr(repo, "insertar_evento", boom)
        with pytest.raises(RuntimeError):
            service.cambiar_estado(None, _alcance(), "cli", "p:1", "EVALUANDO", "")

