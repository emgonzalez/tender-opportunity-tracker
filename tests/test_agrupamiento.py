"""Tests de src.notifier.agrupamiento — la regla única que comparten correo y portal."""
from datetime import datetime, timedelta, timezone
from itertools import product

import pytest

from src.notifier import agrupamiento as ag
from src.notifier import email_sender
from src.scraper.models import ProcesoClasificado, ResultadoAnalisis

TZ = timezone(timedelta(hours=-6))
HOY = datetime(2026, 10, 22, 7, 0, tzinfo=TZ)  # jueves


def _proc(t=0.0, c=0.0, f=0.0) -> ProcesoClasificado:
    return ProcesoClasificado(
        institucion="X", descripcion="d", fecha_publicacion=HOY, url_directa="u",
        match_tecnico=t, match_categoria=c, factibilidad=f,
    )


class TestMatch:
    def test_ponderacion(self):
        assert ag.match_de(_proc(1.0, 0.0, 0.0)) == 40
        assert ag.match_de(_proc(0.0, 1.0, 0.0)) == 30
        assert ag.match_de(_proc(0.0, 0.0, 1.0)) == 30
        assert ag.match_de(_proc(0.9, 0.8, 0.7)) == round((0.36 + 0.24 + 0.21) * 100)

    def test_valores_nulos_o_decimal(self):
        from decimal import Decimal
        assert ag.match_de_valores(None, None, None) == 0
        assert ag.match_de_valores(Decimal("1"), Decimal("1"), Decimal("1")) == 100
        assert ag.match_de_valores("x", 1, 1) == 60


class TestGrupo:
    def test_no_competible_gana_a_todo(self):
        assert ag.grupo_de_valores(95, False, True) == ag.GRUPO_NO_COMPETIBLE
        assert ag.grupo_de_valores(10, False, False) == ag.GRUPO_NO_COMPETIBLE

    def test_triaje_no_go_es_baja(self):
        assert ag.grupo_de_valores(95, True, True) == ag.GRUPO_BAJA
        assert ag.grupo_de_valores(95, None, True) == ag.GRUPO_BAJA

    def test_borde_79_80(self):
        assert ag.grupo_de_valores(79, True, False) == ag.GRUPO_BAJA
        assert ag.grupo_de_valores(80, True, False) == ag.GRUPO_DECIDIR

    def test_competible_desconocido_no_excluye(self):
        assert ag.grupo_de_valores(85, None, False) == ag.GRUPO_DECIDIR

    def test_sin_analisis(self):
        assert ag.grupo_de(85, None) == ag.GRUPO_DECIDIR
        assert ag.grupo_de(79, None) == ag.GRUPO_BAJA

    @pytest.mark.parametrize("match,competible,estado,triaje", list(product(
        (0, 79, 80, 100),
        (True, False, None),
        ("ANALIZADO", "DESCARTADO_TRIAJE", ""),
        ("GO", "NO-GO", ""),
    )))
    def test_equivalencia_correo_portal(self, match, competible, estado, triaje):
        ra = ResultadoAnalisis(proceso_uuid="p", estado=estado, triaje=triaje, competible=competible)
        assert ag.grupo_de(match, ra) == ag.grupo_de_valores(match, competible, ra.descartado_por_triaje)

    @pytest.mark.parametrize("match", (0, 79, 80, 100))
    def test_equivalencia_sin_analisis(self, match):
        assert ag.grupo_de(match, None) == ag.grupo_de_valores(match, None, False)


class TestUrgencia:
    @pytest.mark.parametrize("dias,esperado", [
        (None, None), (-1, None), (0, "rojo"), (7, "rojo"), (8, "ambar"),
        (14, "ambar"), (15, "verde"), (90, "verde"),
    ])
    def test_umbrales(self, dias, esperado):
        assert ag.urgencia(dias) == esperado

    def test_dias_hasta_con_zona(self):
        limite = datetime(2026, 10, 23, 3, 0, tzinfo=timezone.utc)  # 22 oct 21:00 en SV
        assert ag.dias_hasta(limite, HOY) == 0
        assert ag.dias_hasta(None, HOY) is None
        assert ag.dias_hasta(HOY - timedelta(days=2), HOY) == -2

    def test_textos(self):
        lim = HOY + timedelta(days=1)
        assert ag.texto_cierre(None, None) == "Cierre: sin fecha registrada"
        assert ag.texto_cierre(HOY, 0).startswith("Cierra hoy")
        assert ag.texto_cierre(lim, 1).endswith("1 día")
        assert ag.texto_cierre(HOY, -3).startswith("Plazo vencido")

    @pytest.mark.parametrize("dias", (None, -1, 0, 1, 7, 8, 14, 15, 40))
    def test_cierre_del_correo_conserva_colores(self, dias):
        """El correo solo pinta rojo/ámbar; vencido, sin fecha o lejano quedan en ''."""
        limite = None if dias is None else HOY + timedelta(days=dias)
        texto, color = ag.cierre(limite, dias)
        esperado = ag.urgencia(dias) if ag.urgencia(dias) in ("rojo", "ambar") else ""
        assert color == esperado
        assert texto == ag.texto_cierre(limite, dias)


def test_email_sender_reexporta_los_mismos_objetos():
    assert email_sender.grupo_de is ag.grupo_de
    assert email_sender.match_de is ag.match_de
    assert email_sender.UMBRAL_MATCH_DECIDIR == 80
    assert email_sender.DIAS_ESTA_SEMANA == 7 and email_sender.DIAS_PRONTO == 14
    assert (email_sender.GRUPO_DECIDIR, email_sender.GRUPO_NO_COMPETIBLE, email_sender.GRUPO_BAJA) == (
        "decidir", "no_competible", "baja")


def test_agrupamiento_no_importa_smtp_ni_jinja():
    import ast
    import inspect
    arbol = ast.parse(inspect.getsource(ag))
    modulos = {n.module for n in ast.walk(arbol) if isinstance(n, ast.ImportFrom)}
    modulos |= {a.name for n in ast.walk(arbol) if isinstance(n, ast.Import) for a in n.names}
    assert not modulos & {"smtplib", "jinja2", "src.config"}
