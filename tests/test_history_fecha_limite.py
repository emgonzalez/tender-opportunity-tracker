"""P1 (D14): el historial conserva fecha_limite y resend la usa en el reporte."""
import json
import os
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from src import history
from src.classifier.client_profile import load_client_profile
from src.notifier import resend
from src.scraper.models import MatrizRiesgos, ProcesoClasificado, ResultadoAnalisis, RiesgoItem

TZ = timezone(timedelta(hours=-6))
RUN = datetime(2026, 10, 20, 7, 0, tzinfo=TZ)


def _p(ref="R-1", limite=None, relevante=True):
    return ProcesoClasificado(
        numero_referencia=ref, institucion="MINSAL", descripcion="Servidores",
        fecha_publicacion=RUN, url_directa="http://x", fecha_limite=limite,
        es_relevante=relevante, match_tecnico=0.9, match_categoria=0.9,
        factibilidad=0.9, confianza=0.9, categoria_detectada="infra",
    )


def test_roundtrip_con_y_sin_fecha_limite():
    lim = datetime(2026, 10, 25, 15, 30, tzinfo=TZ)
    assert history._deserialize(history._serialize(_p(limite=lim))).fecha_limite == lim
    assert history._deserialize(history._serialize(_p(limite=None))).fecha_limite is None


def test_roundtrip_fecha_limite_naive():
    lim = datetime(2026, 10, 25, 15, 30)
    assert history._deserialize(history._serialize(_p(limite=lim))).fecha_limite == lim


def test_linea_antigua_sin_campo_se_lee(tmp_path):
    d = history._serialize(_p())
    d.pop("fecha_limite")
    assert history._deserialize(d).fecha_limite is None
    d["fecha_limite"] = "basura"
    assert history._deserialize(d).fecha_limite is None
    d.pop("fecha_limite")
    os.makedirs(tmp_path / "history")
    entry = {"run_timestamp": RUN.isoformat(), "total_revisados": 1,
             "total_relevantes": 1, "procesos": [d]}
    (tmp_path / "history" / "c.jsonl").write_text(json.dumps(entry) + "\n")
    assert history.load_last_run("c", str(tmp_path))["procesos"][0].fecha_limite is None


def test_triaje_se_guarda_solo_si_existe():
    p = _p()
    assert "triaje" not in history._serialize(p)
    p.triaje = "GO"
    assert history._serialize(p)["triaje"] == "GO"
    history._deserialize(history._serialize(p))  # no rompe


def test_load_analisis_toma_la_linea_con_matriz(tmp_path):
    m = MatrizRiesgos(items=[RiesgoItem("legal", "multa", "alta", "cl. 5")], resumen="r")
    history.save_analisis("c", ResultadoAnalisis("R-1", "ANALIZADO", matriz=m), str(tmp_path))
    history.save_analisis("c", ResultadoAnalisis("R-1", "NOTIFICADO"), str(tmp_path))
    out = history.load_analisis("c", ["R-1", "R-2"], str(tmp_path))
    assert set(out) == {"R-1"}
    assert out["R-1"].estado == "ANALIZADO"
    assert out["R-1"].matriz.items[0].severidad == "alta"
    assert history.load_analisis("otro", ["R-1"], str(tmp_path)) == {}


def _resend(tmp_path, procesos):
    history.save_run("multicomputos_sv", RUN, procesos, 5, str(tmp_path))
    profile = load_client_profile("multicomputos_sv", "clients")
    config = types.SimpleNamespace(data_dir=str(tmp_path), app_name="AMALIA", app_full_name="AMALIA",
                                   override_recipients=[])
    with patch("src.notifier.email_sender._leer_destinatarios", return_value=[("ana@example.com", frozenset())]), \
         patch("src.notifier.email_sender._send_individual") as envio, \
         patch("src.notifier.email_sender.TEMPLATES_DIR", Path("templates")), \
         patch("src.notifier.email_sender.datetime") as dt:
        dt.now.return_value = RUN
        resend._resend_client(config, profile)
    subject, render = envio.call_args.args[4], envio.call_args.args[5]
    return subject, render("ana@example.com")[0]  # subject, html


def test_resend_conserva_fechas_y_contador(tmp_path):
    procesos = [_p("R-1", limite=RUN + timedelta(days=3)),
                _p("R-2", limite=RUN + timedelta(days=30))]
    subject, _ = _resend(tmp_path, procesos)
    assert "1 cierra esta semana" in subject


def test_resend_sin_fechas_dice_cero(tmp_path):
    subject, _ = _resend(tmp_path, [_p("R-1")])
    assert "0 cierran esta semana" in subject
