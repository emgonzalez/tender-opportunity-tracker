"""`multicomputos` pasó a `multicomputos_sv`: el id viejo sigue resolviendo (tokens de correos ya enviados, CLI,
historial sin renombrar) y el YAML viejo ya no existe."""
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from src import history
from src.classifier import alias_clientes
from src.classifier.client_profile import load_all_active_profiles, load_client_profile
from src.feedback import service, tokens
from src.portales import portal_de_cliente
from src.web.app import crear_app

CLIENTS = str(Path(__file__).parent.parent / "clients")


def test_id_canonico():
    assert alias_clientes.id_canonico("multicomputos") == "multicomputos_sv"
    assert alias_clientes.id_canonico("multicomputos_sv") == "multicomputos_sv"
    assert alias_clientes.id_canonico("multicomputos_cr") == "multicomputos_cr"
    assert alias_clientes.id_canonico("otro") == "otro"


def test_no_queda_un_yaml_con_el_id_viejo():
    """Un `clients/multicomputos.yaml` duplicaría el cliente: `load_all_active_profiles` escanea el directorio."""
    assert not (Path(CLIENTS) / "multicomputos.yaml").exists()
    ids = [p.client_id for p in load_all_active_profiles(CLIENTS)]
    assert "multicomputos" not in ids and ids.count("multicomputos_sv") == 1


def test_el_id_viejo_carga_el_perfil_nuevo():
    viejo, nuevo = load_client_profile("multicomputos", CLIENTS), load_client_profile("multicomputos_sv", CLIENTS)
    assert viejo.client_id == nuevo.client_id == "multicomputos_sv"


def test_portal_de_cliente_acepta_el_id_viejo():
    assert portal_de_cliente(CLIENTS, "multicomputos") == portal_de_cliente(CLIENTS, "multicomputos_sv")


# ── historial sin renombrar ───────────────────────────────────────────────────

def test_ruta_con_legado_prefiere_la_nueva(tmp_path):
    (tmp_path / "multicomputos.jsonl").write_text("viejo")
    (tmp_path / "multicomputos_sv.jsonl").write_text("nuevo")
    ruta = str(tmp_path / "multicomputos_sv.jsonl")
    assert alias_clientes.ruta_con_legado(ruta, "multicomputos_sv") == ruta


def test_ruta_con_legado_cae_al_archivo_viejo_si_falta_el_nuevo(tmp_path):
    (tmp_path / "multicomputos.analisis.jsonl").write_text("viejo")
    nueva = str(tmp_path / "multicomputos_sv.analisis.jsonl")
    assert alias_clientes.ruta_con_legado(nueva, "multicomputos_sv") == str(tmp_path / "multicomputos.analisis.jsonl")


def test_ruta_con_legado_sin_ninguno_devuelve_la_nueva(tmp_path):
    nueva = str(tmp_path / "multicomputos_sv.jsonl")
    assert alias_clientes.ruta_con_legado(nueva, "multicomputos_sv") == nueva
    assert alias_clientes.ruta_con_legado(str(tmp_path / "x.jsonl"), "x") == str(tmp_path / "x.jsonl")


def test_historial_viejo_se_sigue_leyendo_y_escribiendo_en_el_mismo_archivo(tmp_path):
    """Sin esto, entre el deploy y el renombre el historial «desaparece» y se reabre lo ya notificado."""
    (tmp_path / "history").mkdir()
    (tmp_path / "history" / "multicomputos.jsonl").write_text("")
    assert history._history_path("multicomputos_sv", str(tmp_path)).endswith("history/multicomputos.jsonl")
    assert history._history_path("multicomputos", str(tmp_path)).endswith("history/multicomputos.jsonl")
    (tmp_path / "history" / "multicomputos.jsonl").unlink()
    assert history._history_path("multicomputos", str(tmp_path)).endswith("history/multicomputos_sv.jsonl")


# ── enlaces de correos ya enviados ────────────────────────────────────────────

SECRETO = "secreto-de-prueba"


def _cfg():
    return SimpleNamespace(
        enable_feedback=True, feedback_base_url="https://feedback.example.com", feedback_hmac_secret=SECRETO,
        clients_dir=CLIENTS, data_dir="/no/existe", override_recipients=[],
    )


@pytest.mark.parametrize("cliente_en_el_token", ["multicomputos", "multicomputos_sv"])
def test_token_viejo_y_nuevo_votan_con_el_id_canonico(monkeypatch, cliente_en_el_token):
    votos = []
    monkeypatch.setattr(service, "guardar_voto", lambda cfg, **kw: votos.append(kw) or True)
    monkeypatch.setattr(service, "leer_reportada", lambda *a, **k: None)
    monkeypatch.setattr(service, "leer_voto", lambda *a, **k: None)
    perfil = load_client_profile("multicomputos_sv", CLIENTS)
    email = perfil.notification.email_to[0]
    token = tokens.firmar(SECRETO, "96-2026-P0011", cliente_en_el_token, tokens.destinatario_id(email))
    r = TestClient(crear_app(_cfg())).post(f"/f/{token}/relevante")
    assert r.status_code == 200
    assert len(votos) == 1 and votos[0]["client_id"] == "multicomputos_sv"
