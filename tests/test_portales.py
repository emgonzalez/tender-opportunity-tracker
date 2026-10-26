"""src.portales — registro de portales y portal declarado por cada cliente."""
from __future__ import annotations

import pytest

from src import portales
from src.classifier.client_profile import load_client_profile


def test_comprasal_es_el_default_y_esta_integrado():
    assert portales.PORTAL_ID_DEFAULT == "comprasal_sv"
    p = portales.obtener_portal("comprasal_sv")
    assert p.integrado and p.pais == "SV" and p.moneda == "USD"


def test_sicop_esta_declarado_pero_sin_extractor():
    p = portales.obtener_portal("sicop_cr")
    assert not p.integrado and p.pais == "CR" and p.zona_horaria == "America/Costa_Rica" and p.moneda == "CRC"


def test_portal_desconocido_falla_con_mensaje():
    with pytest.raises(portales.PortalDesconocido, match="no_existe"):
        portales.obtener_portal("no_existe")


def _yaml(tmp_path, nombre, portal=None):
    linea = f"  portal: {portal}\n" if portal else ""
    (tmp_path / f"{nombre}.yaml").write_text(
        f"client:\n  id: {nombre}\n  name: X\n  active: true\n{linea}"
        "catalog:\n  categories: []\n", encoding="utf-8")


def test_portal_de_cliente_lee_el_yaml_y_cae_a_comprasal(tmp_path):
    _yaml(tmp_path, "sv")
    _yaml(tmp_path, "cr", "sicop_cr")
    assert portales.portal_de_cliente(str(tmp_path), "sv") == "comprasal_sv"
    assert portales.portal_de_cliente(str(tmp_path), "cr") == "sicop_cr"
    assert portales.portal_de_cliente(str(tmp_path), "no_hay") == "comprasal_sv"
    assert portales.portal_de_cliente("/no/existe", "cr") == "comprasal_sv"


def test_portal_de_cliente_ve_los_cambios_del_yaml(tmp_path):
    import os
    _yaml(tmp_path, "c")
    assert portales.portal_de_cliente(str(tmp_path), "c") == "comprasal_sv"
    _yaml(tmp_path, "c", "sicop_cr")
    os.utime(tmp_path / "c.yaml", ns=(1, 2_000_000_000))  # mtime distinto sin depender del reloj
    assert portales.portal_de_cliente(str(tmp_path), "c") == "sicop_cr"


def test_el_perfil_trae_portal_id(tmp_path):
    _yaml(tmp_path, "sv")
    _yaml(tmp_path, "cr", "sicop_cr")
    assert load_client_profile("sv", str(tmp_path)).portal_id == "comprasal_sv"
    assert load_client_profile("cr", str(tmp_path)).portal_id == "sicop_cr"


def test_el_yaml_de_multicomputos_no_declara_portal_y_sigue_en_comprasal():
    assert load_client_profile("multicomputos", "clients").portal_id == "comprasal_sv"


def test_las_constantes_duplicadas_apuntan_a_la_misma_fuente():
    from src.feedback import repo as f
    from src.memoria import rag, vectorizer
    from src.memoria import repo as m
    from src.mercado import repo as me
    assert {m.PORTAL_ID_DEFAULT, f._PORTAL_ID_DEFAULT_V2, me._PORTAL_ID_DEFAULT_V2,
            vectorizer._PORTAL_ID_DEFAULT_V2, rag._PORTAL_ID_DEFAULT_V2} == {portales.PORTAL_ID_DEFAULT}


# ── El proceso_gid de un cliente de otro portal no cae en comprasal_sv ─────────

def _cfg_cr(tmp_path):
    from types import SimpleNamespace
    _yaml(tmp_path, "cr", "sicop_cr")
    _yaml(tmp_path, "sv")
    return SimpleNamespace(
        clients_dir=str(tmp_path), data_dir=str(tmp_path), enable_memoria=True, enable_esquema_v2=True,
        enable_feedback=True, feedback_base_url="https://f.example.com", feedback_hmac_secret="s",
    )


def _conexion_falsa(monkeypatch):
    from contextlib import contextmanager

    from src.memoria import db

    @contextmanager
    def conexion(cfg):
        yield object()

    monkeypatch.setattr(db, "connection", conexion)


@pytest.mark.parametrize("cliente,esperado", [("cr", "sicop_cr"), ("sv", "comprasal_sv")])
def test_memoria_persiste_con_el_portal_del_cliente(monkeypatch, tmp_path, cliente, esperado):
    from types import SimpleNamespace

    from src.memoria import repo, service

    cfg = _cfg_cr(tmp_path)
    _conexion_falsa(monkeypatch)
    vistos = {}
    monkeypatch.setattr(repo, "upsert_proceso", lambda *a, **kw: None)
    monkeypatch.setattr(repo, "save_analisis_db", lambda *a, **kw: None)
    monkeypatch.setattr(repo, "upsert_proceso_v2", lambda c, p, cl, estado, portal_id: vistos.update(proceso=portal_id))
    monkeypatch.setattr(repo, "save_analisis_v2", lambda c, r, cl, portal_id: vistos.update(analisis=portal_id))
    proceso = SimpleNamespace(numero_referencia="P-1")
    resultado = SimpleNamespace(estado="ANALIZADO")

    service.persistir_memoria(cfg, proceso, resultado, "", cliente)

    assert vistos == {"proceso": esperado, "analisis": esperado}


@pytest.mark.parametrize("cliente,esperado", [("cr", "sicop_cr"), ("sv", "comprasal_sv")])
def test_feedback_escribe_con_el_portal_del_cliente(monkeypatch, tmp_path, cliente, esperado):
    from types import SimpleNamespace

    from src.feedback import repo, service

    cfg = _cfg_cr(tmp_path)
    _conexion_falsa(monkeypatch)
    vistos = {}
    monkeypatch.setattr(repo, "upsert_reportada", lambda *a, **kw: None)
    monkeypatch.setattr(repo, "upsert_voto", lambda *a, **kw: None)
    monkeypatch.setattr(repo, "upsert_reportada_v2", lambda c, cl, o, portal_id: vistos.update(reportada=portal_id))
    monkeypatch.setattr(repo, "upsert_voto_v2", lambda c, **kw: vistos.update(voto=kw["portal_id"]))

    service.registrar_reportadas(cfg, cliente, [SimpleNamespace(numero_referencia="P-1")])
    service.guardar_voto(cfg, client_id=cliente, proceso_id="P-1", dest_id="d", email=None,
                         voto="RELEVANTE", motivos=[], comentario="")

    assert vistos == {"reportada": esperado, "voto": esperado}


def test_el_gid_se_arma_con_el_portal():
    from src.feedback import repo
    assert repo._proceso_gid_v2("P-1", "sicop_cr") == "sicop_cr:P-1"
    assert repo._proceso_gid_v2("P-1") == "comprasal_sv:P-1"


# ── Semilla de Costa Rica ───────────────────────────────────────────────────────

def test_semilla_con_costa_rica_siembra_portal_y_cliente_inactivo(monkeypatch, tmp_path):
    from contextlib import contextmanager
    from types import SimpleNamespace

    from scripts import semilla_esquema_v2 as semilla

    @contextmanager
    def conexion(cfg):
        yield object()

    llamadas = {"portales": [], "clientes": []}
    monkeypatch.setattr(semilla.db, "connection", conexion)
    monkeypatch.setattr(semilla, "_merge_organizacion", lambda c: None)
    monkeypatch.setattr(semilla, "_merge_portal", lambda c, portal=None: llamadas["portales"].append(portal))
    monkeypatch.setattr(semilla, "_merge_cliente",
                        lambda c, cliente_id, nombre, activo: llamadas["clientes"].append((cliente_id, activo)))
    cfg = SimpleNamespace(clients_dir=str(tmp_path))  # sin YAMLs: solo lo propio de Costa Rica

    assert semilla.semilla(cfg)["portal"] == 1
    assert llamadas == {"portales": [None], "clientes": []}  # sin la opción, nada de Costa Rica

    llamadas["portales"].clear()
    conteos = semilla.semilla(cfg, con_costa_rica=True)
    assert conteos["costa_rica"] == 1
    cr = llamadas["portales"][1]
    assert cr["portal_id"] == "sicop_cr" and cr["pais"] == "CR" and cr["moneda_default"] == "CRC"
    assert llamadas["clientes"] == [("multicomputos_cr", False)]


# ── clients/multicomputos_cr.yaml ───────────────────────────────────────────────

def test_multicomputos_cr_copia_el_catalogo_de_el_salvador_pero_queda_inactivo():
    from src.classifier.client_profile import load_all_active_profiles
    sv = load_client_profile("multicomputos", "clients")
    cr = load_client_profile("multicomputos_cr", "clients")
    assert cr.portal_id == "sicop_cr" and cr.active is False
    assert cr.catalog == sv.catalog
    # No hereda los destinatarios ni el perfil entrenado de El Salvador.
    assert cr.notification.email_to == []
    assert not set(cr.notification.email_to) & set(sv.notification.email_to)
    assert cr.intelligence.is_trained is False
    assert "multicomputos_cr" not in {p.client_id for p in load_all_active_profiles("clients")}
