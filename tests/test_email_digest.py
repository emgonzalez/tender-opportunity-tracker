"""Correo diario tipo resumen (P5): grupos, orden, asunto, preheader y parte text/plain.

La regla que se prueba vive en `email_sender` (REGLA DE AGRUPAMIENTO):
  No competibles ← competible is False · Baja prioridad ← triaje NO-GO o match < 80 ·
  Para decidir ← el resto (competible None incluido), por fecha de cierre.
"""
import email
import email.header
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from src import history
from src.notifier import email_sender, resend
from src.notifier.email_sender import (
    GRUPO_BAJA,
    GRUPO_DECIDIR,
    GRUPO_NO_COMPETIBLE,
    agrupar,
    grupo_de,
    send_report,
)
from src.scraper.models import (
    MOTIVO_MARCA_ESPECIFICA,
    MOTIVO_PROVEEDOR_UNICO,
    MOTIVOS_BLOQUEO,
    MatrizRiesgos,
    ProcesoClasificado,
    ResultadoAnalisis,
    RiesgoItem,
)

REPO = Path(__file__).resolve().parent.parent
TZ = timezone(timedelta(hours=-6))
AHORA = datetime(2026, 10, 22, 7, 0, tzinfo=TZ)  # jueves


def _p(ref, match=0.9, dias=None, inst="Ministerio X (MINX)", relevante=True, razon="encaja", monto=None):
    return ProcesoClasificado(
        institucion=inst, descripcion=f"Objeto {ref}", fecha_publicacion=AHORA,
        url_directa=f"https://comprasal.example/{ref}", numero_referencia=ref,
        forma_contratacion="Subasta Inversa", monto=monto,
        fecha_limite=None if dias is None else AHORA + timedelta(days=dias, hours=5),
        es_relevante=relevante, categoria_detectada="servicios_ti", razon=razon,
        match_tecnico=match, match_categoria=match, factibilidad=match,
    )


def _ra(ref, estado="ANALIZADO", competible=True, motivo="", triaje="GO"):
    return ResultadoAnalisis(
        proceso_uuid=ref, estado=estado, triaje=triaje, competible=competible, motivo_bloqueo=motivo,
        matriz=MatrizRiesgos(items=[RiesgoItem("legal", "riesgo-x", "alta", "cita-x")],
                             resumen="Resumen del pliego. Segunda oración."),
    )


def _cfg(**extra):
    base = dict(
        notification_override_to="", override_recipients=[],
        notification_from="AMALIA <bot@gmail.com>",
        smtp_host="smtp.test", smtp_port=587, smtp_user="bot@gmail.com", smtp_password="x",
        app_name="AMALIA", app_full_name="AMALIA Test", enable_feedback=False,
        feedback_base_url="", feedback_hmac_secret="",
    )
    base.update(extra)
    return SimpleNamespace(**base)


def _perfil():
    return SimpleNamespace(
        client_id="multicomputos", client_name="Multicómputos", confidence_threshold=0.5,
        catalog=SimpleNamespace(categories=[SimpleNamespace(id="servicios_ti", label="Servicios Profesionales TI", brands=[])]),
        notification=SimpleNamespace(
            email_to=["ana@cliente.com"], subject_prefix="AMALIA | roto]", greeting="",
            report_title="", intro_message="", system_admins_email=[],
        ),
    )


class _Reloj:
    @staticmethod
    def now(tz=None):
        return AHORA.astimezone(tz) if tz else AHORA


@pytest.fixture(autouse=True)
def _destinatarios_del_portal():
    """Los destinatarios salen de la ADB: aquí, un solo usuario con perfil completo."""
    with patch.object(email_sender, "_leer_destinatarios", return_value=[("ana@cliente.com", frozenset())]):
        yield


def _enviar(procesos, resultados=None, revisados=71, cfg=None):
    """Envía con SMTP simulado; devuelve (asunto, html, texto, msg)."""
    enviados = []
    server = MagicMock()
    server.__enter__.return_value = server
    server.sendmail.side_effect = lambda _f, _t, cuerpo: enviados.append(cuerpo)
    with patch.object(email_sender, "TEMPLATES_DIR", REPO / "templates"), \
         patch.object(email_sender.smtplib, "SMTP", return_value=server), \
         patch.object(email_sender, "datetime", _Reloj):
        send_report(cfg or _cfg(), _perfil(), procesos, revisados, AHORA, resultados)
    assert len(enviados) == 1
    msg = email.message_from_string(enviados[0])
    partes = {p.get_content_type(): p.get_payload(decode=True).decode("utf-8")
              for p in msg.walk() if not p.is_multipart()}
    asunto = str(email.header.make_header(email.header.decode_header(msg["Subject"])))
    return asunto, partes["text/html"], partes["text/plain"], msg


def _orden(html: str, refs: list[str]) -> list[str]:
    return sorted(refs, key=lambda r: html.index(f"Objeto {r}"))


def _seccion(html: str, titulo: str) -> str:
    """El trozo del HTML desde el encabezado de un grupo hasta el siguiente."""
    for parte in html.split('class="bd-')[1:]:  # banda de sección (y chips de la leyenda) llevan `bd-`
        if parte.split(">", 1)[1].lstrip("●⊘▽ ").startswith(titulo):
            return parte
    raise AssertionError(f"No hay sección «{titulo}» en el correo")


# ── Regla de agrupamiento ───────────────────────────────────────────────────

class TestGrupoDe:
    def test_no_competible_gana_a_todo(self):
        ra = _ra("X", competible=False, motivo=MOTIVO_PROVEEDOR_UNICO, triaje="NO-GO")
        assert grupo_de(95, ra) == GRUPO_NO_COMPETIBLE
        assert grupo_de(10, ra) == GRUPO_NO_COMPETIBLE

    def test_triaje_no_go_es_baja_aunque_el_match_sea_alto(self):
        assert grupo_de(95, ResultadoAnalisis("X", "DESCARTADO_TRIAJE")) == GRUPO_BAJA
        assert grupo_de(95, ResultadoAnalisis("X", "NOTIFICADO", triaje="NO-GO")) == GRUPO_BAJA

    def test_umbral_de_match(self):
        assert grupo_de(80, None) == GRUPO_DECIDIR
        assert grupo_de(79, None) == GRUPO_BAJA

    def test_competible_none_no_excluye(self):
        ra = _ra("X", competible=None, motivo=MOTIVO_MARCA_ESPECIFICA)
        assert grupo_de(85, ra) == GRUPO_DECIDIR
        assert grupo_de(85, None) == GRUPO_DECIDIR  # sin análisis (drill-down apagado)


class TestAgrupar:
    def test_tres_grupos_y_suman_el_total(self):
        procesos = [_p("D1", dias=3), _p("D2", dias=1), _p("N1", dias=2), _p("B1", 0.7), _p("B2", 0.95)]
        res = {"N1": _ra("N1", competible=False, motivo=MOTIVO_PROVEEDOR_UNICO),
               "B2": ResultadoAnalisis("B2", "DESCARTADO_TRIAJE", triaje="NO-GO")}
        g = agrupar(procesos, res, AHORA)
        assert [o["code"] for o in g[GRUPO_DECIDIR]] == ["D2", "D1"]
        assert [o["code"] for o in g[GRUPO_NO_COMPETIBLE]] == ["N1"]
        assert [o["code"] for o in g[GRUPO_BAJA]] == ["B2", "B1"]  # por match descendente
        assert sum(len(v) for v in g.values()) == len(procesos)

    def test_orden_por_cierre_sin_fecha_al_final_y_desempate_por_match(self):
        procesos = [_p("SIN-A", 0.85), _p("D10", 0.85, dias=10), _p("SIN-B", 0.95),
                    _p("D2-bajo", 0.82, dias=2), _p("D2-alto", 0.92, dias=2), _p("VENCIDA", 0.9, dias=-2)]
        g = agrupar(procesos, {}, AHORA)
        assert [o["code"] for o in g[GRUPO_DECIDIR]] == [
            "D2-alto", "D2-bajo", "D10", "VENCIDA", "SIN-B", "SIN-A"]

    def test_competible_none_con_motivo_se_muestra_como_alerta(self):
        res = {"X": _ra("X", competible=None, motivo=MOTIVO_MARCA_ESPECIFICA)}
        (o,) = agrupar([_p("X")], res, AHORA)[GRUPO_DECIDIR]
        assert o["alerta"] == MOTIVOS_BLOQUEO[MOTIVO_MARCA_ESPECIFICA]

    def test_no_competible_lleva_la_etiqueta_legible(self):
        res = {"X": _ra("X", competible=False, motivo=MOTIVO_PROVEEDOR_UNICO)}
        (o,) = agrupar([_p("X", dias=4)], res, AHORA)[GRUPO_NO_COMPETIBLE]
        assert o["motivo"] == MOTIVOS_BLOQUEO[MOTIVO_PROVEEDOR_UNICO]
        assert MOTIVOS_BLOQUEO[MOTIVO_PROVEEDOR_UNICO] in o["detalle"]

    def test_urgencia_unificada_con_el_contador(self):
        g = agrupar([_p("A", dias=7), _p("B", dias=8), _p("C", dias=14), _p("D", dias=15)], {}, AHORA)
        urg = {o["code"]: o["urgencia"] for o in g[GRUPO_DECIDIR]}
        assert urg == {"A": "rojo", "B": "ambar", "C": "ambar", "D": ""}

    def test_veredicto_sale_de_la_razon_y_si_falta_del_resumen(self):
        g = agrupar([_p("A", razon="x" * 300), _p("B", razon="")], {"B": _ra("B")}, AHORA)
        a, b = g[GRUPO_DECIDIR]
        assert len(a["veredicto"]) <= email_sender.LARGO_VEREDICTO
        assert b["veredicto"] == "Resumen del pliego"


# ── Correo renderizado ──────────────────────────────────────────────────────

class TestCorreo:
    @pytest.fixture
    def correo(self):
        procesos = [_p("D-TARDE", 0.85, dias=12, inst="Universidad (UES)"),
                    _p("D-PRONTO", 0.86, dias=7, inst="Registro Nacional (RNPN)"),
                    _p("D-SIN", 0.9),
                    _p("N-UNICO", 0.85, dias=12, inst="Portuaria (CEPA)"),
                    _p("B-TRIAJE", 0.74), _p("B-MATCH", 0.69),
                    _p("NO-REL", 0.9, relevante=False)]
        res = {"N-UNICO": _ra("N-UNICO", competible=False, motivo=MOTIVO_PROVEEDOR_UNICO),
               "B-TRIAJE": ResultadoAnalisis("B-TRIAJE", "DESCARTADO_TRIAJE", triaje="NO-GO")}
        return _enviar(procesos, res)

    def test_asunto_accionable(self, correo):
        asunto, *_ = correo
        assert asunto == "AMALIA · 3 para decidir · 1.ª cierra jue 29 oct (RNPN) · 1 cierra esta semana"
        assert "roto" not in asunto  # no depende de subject_prefix del yaml

    def test_preheader_complementa_al_asunto_y_va_primero(self, correo):
        _, html, *_ = correo
        cuerpo = html.split("<body", 1)[1].split(">", 1)[1].lstrip()
        assert cuerpo.startswith('<div style="display:none;')
        pre = cuerpo.split(">", 1)[1].split("&#8199;", 1)[0]
        assert pre == "1 no competible · 2 de baja prioridad · 71 procesos revisados · todas en este correo."

    def test_veredicto_y_contadores_que_suman(self, correo):
        _, html, *_ = correo
        assert "3 oportunidades para decidir; la más urgente cierra el jueves 29 de octubre." in html
        assert "71 procesos revisados → 6 relevantes = 3 para decidir + 1 no competible + 2 de baja prioridad." in html
        assert "Este correo las muestra todas." in html

    def test_todas_las_relevantes_aparecen_y_solo_ellas(self, correo):
        _, html, texto, _ = correo
        for ref in ("D-TARDE", "D-PRONTO", "D-SIN", "N-UNICO", "B-TRIAJE", "B-MATCH"):
            assert f"Objeto {ref}" in html and f"Objeto {ref}" in texto
        assert "Objeto NO-REL" not in html

    def test_cada_oportunidad_en_su_grupo_y_en_orden(self, correo):
        _, html, *_ = correo
        decidir = _seccion(html, "Para decidir")
        assert _orden(decidir, ["D-SIN", "D-TARDE", "D-PRONTO"]) == ["D-PRONTO", "D-TARDE", "D-SIN"]
        nc = _seccion(html, "No competibles")
        assert "Objeto N-UNICO" in nc and "Contratación directa con proveedor único" in nc
        baja = _seccion(html, "Baja prioridad")
        assert "Objeto B-TRIAJE" in baja and "triaje NO-GO" in baja and "Objeto B-MATCH" in baja

    def test_cierre_en_texto_con_color_coherente(self, correo):
        _, html, *_ = correo
        # El texto lleva el significado y el chip lo refuerza: rojo ≤ 7 días, ámbar 8–14.
        assert re.search(r"color:#A33A2A;[^>]*>Cierra jue 29 oct · 7 días</span>", html)
        assert re.search(r"color:#8A6D1F;[^>]*>Cierra mar 3 nov · 12 días</span>", html)
        assert "Cierre: sin fecha registrada" in html
        assert "Esta semana" not in html and "Holgado" not in html

    def test_categoria_legible_y_sin_estimado_vacio(self, correo):
        _, html, *_ = correo
        assert "Servicios Profesionales TI" in html and "servicios_ti" not in html
        assert "Estimado" not in html and "—</p>" not in html

    def test_sin_enlaces_muertos_ni_dejar_de_recibir_falso(self, correo):
        _, html, *_ = correo
        assert 'href="#"' not in html
        assert "Dejar de recibir" not in html
        assert "Configurar filtros" not in html

    def test_compatibilidad_y_modo_oscuro(self, correo):
        _, html, *_ = correo
        assert "gradient" not in html and "@import" not in html
        assert 'name="color-scheme" content="light dark"' in html
        assert "prefers-color-scheme:dark" in html
        assert "max-width:640px" in html
        anchos = [int(w) for w in re.findall(r'width="(\d+)"', html)]
        assert anchos and max(anchos) <= 640

    def test_sin_matriz_en_el_correo(self, correo):
        _, html, *_ = correo
        assert "riesgo-x" not in html and "cita-x" not in html

    def test_parte_texto_con_el_contenido_esencial(self, correo):
        asunto, _, texto, msg = correo
        assert msg.get_content_type() == "multipart/alternative"
        tipos = [p.get_content_type() for p in msg.get_payload()]
        assert tipos == ["text/plain", "text/html"]  # el preferido (HTML) va al final
        assert "71 procesos revisados → 6 relevantes = 3 para decidir" in texto
        assert "PARA DECIDIR (3)" in texto and "NO COMPETIBLES (1)" in texto and "BAJA PRIORIDAD (2)" in texto
        assert texto.index("Objeto D-PRONTO") < texto.index("Objeto D-TARDE") < texto.index("Objeto D-SIN")
        assert "https://comprasal.example/D-PRONTO" in texto
        assert "<" not in texto.replace("< 80", "")


class TestCasosLimite:
    def test_cero_relevantes_siempre_sale_con_revisados(self):
        asunto, html, texto, _ = _enviar([_p("X", relevante=False)], revisados=42)
        assert asunto == "AMALIA · Sin oportunidades nuevas · 42 revisados"
        assert "42 procesos revisados" in html and "42 procesos revisados" in texto
        assert "Sin oportunidades relevantes en este período." in html

    def test_nada_para_decidir(self):
        asunto, html, *_ = _enviar([_p("B", 0.5)])
        assert asunto.startswith("AMALIA · Nada para decidir hoy")
        assert "Nada para decidir hoy" in html
        assert "Objeto B" in _seccion(html, "Baja prioridad")

    def test_para_decidir_sin_fechas(self):
        asunto, html, *_ = _enviar([_p("A"), _p("B")])
        assert asunto == "AMALIA · 2 para decidir · 0 cierran esta semana"
        assert "ninguna trae fecha de cierre" in html

    def test_monto_solo_si_existe(self):
        _, html, *_ = _enviar([_p("A", monto=2625.0), _p("B")])
        assert "USD 2,625" in html
        assert html.count("USD ") == 1

    def test_modo_validacion_conserva_el_prefijo(self):
        asunto, *_ = _enviar([_p("A", dias=3)], cfg=_cfg(override_recipients=["yo@x.com"]))
        assert asunto.startswith("[VALIDACIÓN] AMALIA · 1 para decidir")

    def test_tope_de_baja_prioridad_lo_dice_y_enlaza(self):
        n = email_sender.MAX_LINEAS_BAJA + 3
        procesos = [_p(f"B{i:03d}", 0.5) for i in range(n)]
        asunto, html, texto, _ = _enviar(procesos)
        assert f"Baja prioridad · {n}" in html
        assert "Y 3 más de baja prioridad que no caben en este correo" in html
        assert f'href="{email_sender.COMPRASAL_URL}"' in html
        assert "las 3 restantes se indican al final" in html
        assert "Y 3 más de baja prioridad" in texto

    def test_para_decidir_pasado_el_tope_sigue_visible_en_una_linea(self):
        n = email_sender.MAX_DETALLADAS + 4
        procesos = [_p(f"D{i:03d}", 0.9, dias=i + 1) for i in range(n)]
        _, html, *_ = _enviar(procesos)
        assert f"Para decidir · {n}" in html
        assert html.count('class="cd" bgcolor="#FFFFFF" style="padding:0;') == email_sender.MAX_DETALLADAS  # una por tarjeta
        for p in procesos:
            assert p.descripcion in html


# ── Datos reales (corrida del 2026-10-22, 16 relevantes) ────────────────────

def test_las_16_reales_aparecen_todas(tmp_path):
    """Las 16 relevantes del historial real (copia anonimizada en forma, no en datos)."""
    reales = [
        ("8352-2026-P0114", 74, "MUNICIPIO DE SONSONATE CENTRO"), ("4114-2026-P0216", 85, "CNR (CNR)"),
        ("96-2026-P0187", 76, "BANDESAL (BANDESAL)"), ("1000018-2026-P0028", 55, "OPA (OPA)"),
        ("3110-2026-P0052", 69, "Instituto Crecer Juntos"), ("4303-2026-P0361", 85, "CEPA (CEPA)"),
        ("2300-2026-P0185", 76, "MIGOB (MIGOB)"), ("2404-2026-P0053", 86, "RNPN (RNPN)"),
        ("1500-2026-P0077", 69, "CNJ (CNJ)"), ("4404-2026-P0126", 85, "ASA (ASA)"),
        ("8652-2026-P0219", 74, "MUNICIPIO DE SAN SALVADOR CENTRO"), ("3101-2026-P0078", 85, "UES (UES)"),
        ("4303-2026-P0030", 74, "CEPA (CEPA)"), ("900-2026-P0176", 64, "Ministerio de Defensa Nacional"),
        ("99-2027-P0075", 55, "BCR (BCR)"), ("99-2027-P0072", 91, "BCR (BCR)"),
    ]
    procesos = [_p(ref, m / 100, inst=inst) for ref, m, inst in reales]
    res = {"8352-2026-P0114": ResultadoAnalisis("8352-2026-P0114", "DESCARTADO_TRIAJE", triaje="NO-GO"),
           "1500-2026-P0077": ResultadoAnalisis("1500-2026-P0077", "DESCARTADO_TRIAJE", triaje="NO-GO"),
           "4303-2026-P0361": _ra("4303-2026-P0361", competible=False, motivo=MOTIVO_PROVEEDOR_UNICO),
           "99-2027-P0072": _ra("99-2027-P0072", competible=False, motivo=MOTIVO_MARCA_ESPECIFICA)}
    _, html, texto, _ = _enviar(procesos, res)
    for ref, *_ in reales:
        assert f"Objeto {ref}" in html and f"Objeto {ref}" in texto
    assert "16 relevantes = 4 para decidir + 2 no competibles + 10 de baja prioridad" in html
    nc = _seccion(html, "No competibles")
    assert "Objeto 99-2027-P0072" in nc and "Objeto 4303-2026-P0361" in nc
    baja = _seccion(html, "Baja prioridad")
    assert "Objeto 8352-2026-P0114" in baja and "Objeto 1500-2026-P0077" in baja


# ── Reenvío: agrupa igual que el original ───────────────────────────────────

def test_resend_agrupa_igual_que_el_original(tmp_path):
    procesos = [_p("D", 0.9, dias=3), _p("N", 0.9, dias=4), _p("T", 0.9)]
    original = {"N": _ra("N", competible=False, motivo=MOTIVO_PROVEEDOR_UNICO),
                "T": ResultadoAnalisis("T", "DESCARTADO_TRIAJE", triaje="NO-GO")}
    _, html_original, *_ = _enviar(procesos, original)

    data = str(tmp_path)
    history.save_run("multicomputos", AHORA, procesos, 71, data)
    history.save_analisis("multicomputos", original["N"], data)
    history.save_analisis("multicomputos", ResultadoAnalisis("N", "NOTIFICADO", competible=False,
                                                             motivo_bloqueo=MOTIVO_PROVEEDOR_UNICO), data)
    history.save_analisis("multicomputos", original["T"], data)

    capturado = {}

    def send(**kw):
        capturado.update(kw)

    with patch.object(resend, "send_report", side_effect=send):
        assert resend._resend_client(SimpleNamespace(data_dir=data), _perfil())
    res = capturado["resultados_analisis"]
    assert res["N"].competible is False and res["N"].motivo_bloqueo == MOTIVO_PROVEEDOR_UNICO
    assert res["T"].descartado_por_triaje
    assert "D" not in res

    _, html_reenvio, *_ = _enviar(capturado["procesos_clasificados"], res)
    for titulo in ("Para decidir", "No competibles", "Baja prioridad"):
        assert re.findall(r"Objeto (\w+)", _seccion(html_reenvio, titulo)) == \
            re.findall(r"Objeto (\w+)", _seccion(html_original, titulo))


def test_resend_sin_analisis_no_inventa_nada(tmp_path):
    assert history.load_analisis("c", ["X"], str(tmp_path)) == {}
    (tmp_path / "history").mkdir()
    (tmp_path / "history" / "c.analisis.jsonl").write_text(
        json.dumps({"proceso_uuid": "X", "estado": "ERROR_DESCARGA"}) + "\n")
    assert history.load_analisis("c", ["X"], str(tmp_path)) == {}
