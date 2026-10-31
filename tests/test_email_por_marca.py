"""Correo por marca (E14): qué ve cada destinatario según su rol, y qué pasa si la ADB no responde.

Las direcciones son ficticias (`@example.com`). La ADB caída se simula con un DSN inválido, nunca con una
contraseña errónea (bloquearía la cuenta real, ORA-28000).
"""
import email
import email.header
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from src.memoria import db
from src.notifier import email_sender
from src.notifier.email_sender import send_report
from src.notifier.por_marca import es_de_marca_proceso, marcas_visibles
from src.scraper.models import MarcaRecomendada, ProcesoClasificado

REPO_TEMPLATES = Path(__file__).resolve().parent.parent / "templates"
ANA = "ana@example.com"          # perfil completo
LUIS = "luis@example.com"        # perfil completo
ORA = "oracle@example.com"       # rol Oracle
ORA2 = "oracle2@example.com"     # otro rol Oracle: mismo perfil
ADMIN = "plataforma@example.com"
MIO = "validacion@example.com"
ORACLE = frozenset({"oracle"})
REVISADOS = 71


def _cfg(override: str = "", feedback: bool = False, **extra):
    base = dict(
        notification_override_to=override,
        override_recipients=[a.strip() for a in override.split(",") if a.strip()],
        notification_from="AMALIA <bot@example.com>",
        smtp_host="smtp.test", smtp_port=587, smtp_user="bot@example.com", smtp_password="x",
        app_name="AMALIA", app_full_name="AMALIA Test",
        enable_feedback=feedback, feedback_base_url="https://feedback.example.com", feedback_hmac_secret="s",
        alert_telegram_bot_token="", alert_telegram_chat_id="", alert_webhook_url="",
    )
    base.update(extra)
    return SimpleNamespace(**base)


def _perfil():
    return SimpleNamespace(
        client_id="cliente_x", client_name="Cliente X", confidence_threshold=0.5,
        notification=SimpleNamespace(email_to=["no-se-usa@example.com"], subject_prefix="[TOT]", greeting="",
                                     report_title="", intro_message="", system_admins_email=[]),
    )


def _proceso(ref: str, titulo: str, marcas=(), match: float = 0.9) -> ProcesoClasificado:
    return ProcesoClasificado(
        institucion="Ministerio X", descripcion=titulo, fecha_publicacion=datetime.now(timezone.utc),
        url_directa=f"https://comprasal.example/{ref}", numero_referencia=ref, forma_contratacion="Licitación",
        fecha_limite=datetime.now(timezone.utc) + timedelta(days=5),
        es_relevante=True, categoria_detectada="infraestructura", razon="encaja",
        match_tecnico=match, match_categoria=match, factibilidad=match,
        marcas_recomendadas=[MarcaRecomendada(marca=m) for m in marcas],
    )


P_ORACLE = _proceso("P-ORA", "Bases de datos", ["Oracle NetSuite", "Dell"])   # marca + co-marca
P_TITULO = _proceso("P-TIT", "Licencias Oracle Database para el ministerio")     # solo por título
P_OTRA = _proceso("P-OTR", "Servidores rack", ["HPE"])                           # sin Oracle
P_FALSO = _proceso("P-FAL", "Equipos Oraclexyz", ["Oraclexyz"])                  # límite de palabra
PROCESOS = [P_ORACLE, P_TITULO, P_OTRA, P_FALSO]


class _Smtp:
    def __init__(self):
        self.enviados: list[tuple[list[str], str]] = []

    def __call__(self, *_a, **_k):
        server = MagicMock()
        server.__enter__.return_value = server
        server.__exit__.return_value = False
        server.sendmail.side_effect = lambda _f, dest, cuerpo: self.enviados.append((list(dest), cuerpo))
        return server

    def mensajes(self):
        return [email.message_from_string(c) for _, c in self.enviados]

    def de(self, destino: str):
        (msg,) = [m for (d, _), m in zip(self.enviados, self.mensajes(), strict=True) if d == [destino]]
        return msg


def _asunto(msg) -> str:
    return str(email.header.make_header(email.header.decode_header(msg["Subject"])))


def _parte(msg, tipo: str) -> str:
    for p in msg.walk():
        if p.get_content_type() == tipo:
            return p.get_payload(decode=True).decode("utf-8")
    raise AssertionError(tipo)


@pytest.fixture(autouse=True)
def plantillas_reales():
    with patch.object(email_sender, "TEMPLATES_DIR", REPO_TEMPLATES):
        yield


@pytest.fixture
def smtp():
    fake = _Smtp()
    with patch("src.notifier.email_sender.smtplib.SMTP", fake):
        yield fake


@pytest.fixture
def destinatarios():
    with patch("src.notifier.email_sender._leer_destinatarios") as leer:
        leer.return_value = [(ANA, frozenset()), (ORA, ORACLE)]
        yield leer


@pytest.fixture
def alerta():
    with patch("src.notifier.email_sender.alerts.notify", return_value=["telegram"]) as notify, \
         patch("src.notifier.email_sender.alerts.admins_plataforma", return_value=[ADMIN]):
        yield notify


def _enviar(cfg=None, procesos=None):
    send_report(cfg or _cfg(), _perfil(), PROCESOS if procesos is None else procesos, REVISADOS,
                datetime.now(timezone.utc))


class TestEsDeMarcaProceso:
    def test_marca_recomendada_con_limite_de_palabra(self):
        assert es_de_marca_proceso(P_ORACLE, ORACLE)
        assert not es_de_marca_proceso(P_FALSO, ORACLE)

    def test_o_el_titulo_nombra_la_marca(self):
        assert es_de_marca_proceso(P_TITULO, ORACLE)
        assert not es_de_marca_proceso(P_OTRA, ORACLE)

    def test_sin_restriccion_todo_pasa(self):
        assert all(es_de_marca_proceso(p, frozenset()) for p in PROCESOS)

    def test_clave_no_restringible_no_lanza_ni_abre_de_mas(self):
        assert es_de_marca_proceso(P_ORACLE, frozenset({"desconocida"})) is False

    def test_marcas_visibles_quita_las_co_marcas(self):
        assert marcas_visibles(["Dell", "Oracle NetSuite"], ORACLE) == ["Oracle NetSuite"]
        assert marcas_visibles(["Dell", "Oracle NetSuite"], frozenset()) == ["Dell", "Oracle NetSuite"]


class TestCorreoPorPerfil:
    def test_perfil_completo_ve_todo_con_su_cifra_de_revisados(self, smtp, destinatarios):
        _enviar()
        msg = smtp.de(ANA)
        html = _parte(msg, "text/html")
        assert all(ref in html for ref in ("P-ORA", "P-TIT", "P-OTR", "P-FAL"))
        assert "Procesos revisados" in html and f">{REVISADOS}<" in html
        assert f"{REVISADOS} procesos revisados" in _parte(msg, "text/plain")
        assert "Portafolio: Oracle NetSuite, Dell" in html or "Portafolio: Oracle NetSuite, Dell" in _parte(msg, "text/plain")

    def test_rol_oracle_solo_ve_oportunidades_oracle(self, smtp, destinatarios):
        _enviar()
        html = _parte(smtp.de(ORA), "text/html")
        assert "P-ORA" in html and "P-TIT" in html
        assert "P-OTR" not in html and "P-FAL" not in html

    def test_asunto_y_contadores_son_solo_de_lo_suyo(self, smtp, destinatarios):
        _enviar()
        assert "2 para decidir" in _asunto(smtp.de(ORA))
        assert "4 para decidir" in _asunto(smtp.de(ANA))
        texto = _parte(smtp.de(ORA), "text/plain")
        assert "2 relevantes = 2 para decidir" in texto

    def test_sin_cifra_de_revisados(self, smtp, destinatarios):
        _enviar()
        msg = smtp.de(ORA)
        todo = _parte(msg, "text/html") + _parte(msg, "text/plain") + _asunto(msg)
        assert "revisado" not in todo.lower()
        assert str(REVISADOS) not in re.sub(r"https?://\S+", "", todo)

    def test_sin_co_marcas_en_el_portafolio(self, smtp, destinatarios):
        _enviar()
        msg = smtp.de(ORA)
        todo = _parte(msg, "text/html") + _parte(msg, "text/plain")
        assert "Oracle NetSuite" in todo
        assert "Dell" not in todo

    def test_restringido_sin_oportunidades_el_correo_sale_igual_con_cero(self, smtp, destinatarios):
        _enviar(procesos=[P_OTRA, P_FALSO])
        assert len(smtp.enviados) == 2
        msg = smtp.de(ORA)
        assert _asunto(msg) == "AMALIA · Sin oportunidades nuevas"
        assert "Sin oportunidades nuevas hoy." in _parte(msg, "text/html")
        assert "revisado" not in (_parte(msg, "text/html") + _parte(msg, "text/plain")).lower()

    def test_un_correo_por_persona_con_un_solo_to(self, smtp, destinatarios):
        destinatarios.return_value = [(ANA, frozenset()), (LUIS, frozenset()), (ORA, ORACLE), (ORA2, ORACLE)]
        _enviar()
        assert [d for d, _ in smtp.enviados] == [[ANA], [LUIS], [ORA], [ORA2]]
        assert [m["To"] for m in smtp.mensajes()] == [ANA, LUIS, ORA, ORA2]


class TestRegistrarReportadas:
    def test_recibe_la_union_de_lo_que_ve_cada_perfil(self, smtp, destinatarios):
        # Solo destinatarios Oracle + el perfil completo: la unión es todo lo que alguien recibe.
        with patch("src.notifier.email_sender.feedback.registrar_reportadas") as reg:
            _enviar(_cfg(feedback=True))
        reg.assert_called_once()
        assert {p.numero_referencia for p in reg.call_args[0][2]} == {"P-ORA", "P-TIT", "P-OTR", "P-FAL"}

    def test_solo_restringidos_registra_solo_lo_de_la_marca(self, smtp, destinatarios):
        destinatarios.return_value = [(ORA, ORACLE)]
        with patch("src.notifier.email_sender.feedback.registrar_reportadas") as reg:
            _enviar(_cfg(feedback=True))
        assert {p.numero_referencia for p in reg.call_args[0][2]} == {"P-ORA", "P-TIT"}

    def test_con_feedback_apagado_no_toca_la_adb(self, smtp, destinatarios):
        with patch("src.notifier.email_sender.feedback.registrar_reportadas") as reg:
            _enviar(_cfg(feedback=False))
        reg.assert_not_called()


class TestModoValidacion:
    def test_un_correo_por_perfil_distinto_no_uno_por_usuario(self, smtp, destinatarios):
        destinatarios.return_value = [(ANA, frozenset()), (LUIS, frozenset()), (ORA, ORACLE), (ORA2, ORACLE)]
        _enviar(_cfg(override=MIO))
        assert [d for d, _ in smtp.enviados] == [[MIO], [MIO]]
        completo, oracle = smtp.mensajes()
        assert completo["X-Original-To"] == f"{ANA}, {LUIS}"
        assert oracle["X-Original-To"] == f"{ORA}, {ORA2}"
        assert _asunto(completo).startswith("[VALIDACIÓN] ") and _asunto(oracle).startswith("[VALIDACIÓN] ")
        # Ni en el sobre ni en las cabeceras hay un destinatario real.
        for dest, cuerpo in smtp.enviados:
            assert dest == [MIO]
        assert all(m["To"] == MIO for m in smtp.mensajes())

    def test_el_perfil_oracle_de_validacion_tampoco_trae_lo_ajeno(self, smtp, destinatarios):
        _enviar(_cfg(override=MIO))
        _, oracle = smtp.mensajes()
        assert "P-OTR" not in _parte(oracle, "text/html")


class TestSinDestinatarios:
    def test_adb_caida_no_envia_alerta_y_no_lanza(self, smtp, alerta):
        # DSN inválido: falla al conectar sin tocar ninguna cuenta (nunca una contraseña errónea).
        cfg = _cfg(adb_user="AMALIA", adb_password="no-se-usa", adb_dsn="dsn-invalido-sin-host")
        db.close_pool()
        try:
            _enviar(cfg)  # no debe lanzar
        finally:
            db.close_pool()
        assert smtp.enviados == []
        alerta.assert_called_once()
        asunto = alerta.call_args[0][1]
        assert "la ADB no responde" in asunto
        assert alerta.call_args.kwargs["email_recipients"] == [ADMIN]

    def test_adb_no_configurada_tambien_es_adb_caida(self, smtp, alerta):
        _enviar(_cfg())  # sin atributos adb_*
        assert smtp.enviados == []
        assert "la ADB no responde" in alerta.call_args[0][1]

    def test_cliente_activo_sin_destinatarios_alerta_distinta(self, smtp, destinatarios, alerta):
        destinatarios.return_value = []
        _enviar()
        assert smtp.enviados == []
        asunto = alerta.call_args[0][1]
        assert "sin destinatarios" in asunto and "la ADB no responde" not in asunto

    def test_sin_destinatarios_no_registra_reportadas(self, smtp, destinatarios, alerta):
        destinatarios.return_value = []
        with patch("src.notifier.email_sender.feedback.registrar_reportadas") as reg:
            _enviar(_cfg(feedback=True))
        reg.assert_not_called()
