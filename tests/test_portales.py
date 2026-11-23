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
    monkeypatch.setattr(
        semilla, "_merge_cliente_portal",
        lambda c, cliente_id, portal_id, lookback_dias=1: llamadas.setdefault("cp", []).append(
            (cliente_id, portal_id, lookback_dias)))
    monkeypatch.setattr(semilla, "_merge_taxonomia_modalidad", lambda c, portal_id: 13)
    cfg = SimpleNamespace(clients_dir=str(tmp_path))  # sin YAMLs: solo lo propio de Costa Rica

    assert semilla.semilla(cfg)["portal"] == 1
    assert llamadas == {"portales": [None], "clientes": []}  # sin la opción, nada de Costa Rica

    llamadas["portales"].clear()
    conteos = semilla.semilla(cfg, con_costa_rica=True)
    assert conteos["costa_rica"] == 1
    cr = llamadas["portales"][1]
    assert cr["portal_id"] == "sicop_cr" and cr["pais"] == "CR" and cr["moneda_default"] == "CRC"
    assert llamadas["clientes"] == [("multicomputos_cr", False)]
    assert llamadas["cp"] == [("multicomputos_cr", "sicop_cr", 1)]


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


# ── Portal ampliado (docs/arquitectura/portales.md §1) ─────────────────────────

def test_portal_es_inmutable():
    import dataclasses
    p = portales.obtener_portal("comprasal_sv")
    with pytest.raises(dataclasses.FrozenInstanceError):
        p.moneda = "CRC"  # type: ignore[misc]


def test_valores_de_comprasal():
    p = portales.PORTALES["comprasal_sv"]
    assert p.requiere_login and p.prefijo_credenciales == "COMPRASAL"
    assert p.extractor == "src.scraper.extractor:extraer_comprasal"
    assert p.localizador == "src.drilldown.locator:LocalizadorComprasal"
    assert p.lookback_dias is None and p.monedas == () and p.moneda_unica
    assert (p.etiqueta_proceso, p.etiqueta_proceso_plural, p.etiqueta_documento) == ("proceso", "procesos", "pliego")
    assert p.palabras_documento == ()
    assert p.url_consulta == "https://www.comprasal.gob.sv/"
    assert p.url_detalle.format(id_externo="123") == "https://www.comprasal.gob.sv/pac-procesos/etapas/123"


def test_valores_de_sicop():
    p = portales.PORTALES["sicop_cr"]
    assert not p.integrado and not p.requiere_login and p.prefijo_credenciales == ""
    assert p.extractor == "src.scraper.sicop:extraer"
    assert p.localizador == "src.drilldown.localizador_sicop:LocalizadorSicop"
    assert p.lookback_dias is None
    assert p.moneda == "CRC" and p.monedas == ("CRC", "USD") and not p.moneda_unica
    assert (p.etiqueta_proceso, p.etiqueta_proceso_plural) == ("concurso", "concursos")
    assert p.etiqueta_documento == "cartel"  # decisión A-1 de Emilio
    assert "cartel" in p.palabras_documento
    assert p.url_consulta == "https://www.sicop.go.cr/app/module/bid/public/tenders"
    assert p.url_detalle == ""  # sin enlace profundo: nunca se inventa una URL


def test_portales_en_orden_empieza_por_comprasal():
    orden = portales.portales_en_orden()
    assert [p.portal_id for p in orden] == ["comprasal_sv", "sicop_cr"]


def test_los_modulos_declarados_de_comprasal_existen():
    import importlib.util
    for ruta in (portales.PORTALES["comprasal_sv"].extractor, portales.PORTALES["comprasal_sv"].localizador):
        modulo = ruta.partition(":")[0]
        assert importlib.util.find_spec(modulo) is not None, ruta


def _portal_con(**cambios):
    import dataclasses
    return dataclasses.replace(portales.PORTALES["comprasal_sv"], portal_id="falso", **cambios)


def test_cargar_extractor_y_localizador_importan_tarde(monkeypatch):
    import sys
    import types

    async def extraer(config, portal, desde):
        return []

    class Loc:
        pass

    modulo = types.ModuleType("modulo_portal_falso")
    modulo.extraer = extraer
    modulo.Loc = Loc
    modulo.no_invocable = 42
    monkeypatch.setitem(sys.modules, "modulo_portal_falso", modulo)

    p = _portal_con(extractor="modulo_portal_falso:extraer", localizador="modulo_portal_falso:Loc")
    assert portales.cargar_extractor(p) is extraer
    assert portales.cargar_localizador(p) is Loc
    assert portales.cargar_localizador(_portal_con(localizador="")) is None

    from src.portales.contrato import ErrorPortal
    with pytest.raises(ErrorPortal, match="no es invocable"):
        portales.cargar_extractor(_portal_con(extractor="modulo_portal_falso:no_invocable"))


@pytest.mark.parametrize("ruta", ["no.existe.jamas:extraer", "modulo_sin_atributo:x", "sin_dos_puntos", ":x", ""])
def test_cargar_extractor_con_ruta_invalida_da_error_portal(monkeypatch, ruta):
    import sys
    import types

    from src.portales.contrato import ErrorPortal
    monkeypatch.setitem(sys.modules, "modulo_sin_atributo", types.ModuleType("modulo_sin_atributo"))
    with pytest.raises(ErrorPortal) as exc:
        portales.cargar_extractor(_portal_con(extractor=ruta))
    assert exc.value.portal_id == "falso"


def test_cargar_localizador_con_ruta_invalida_da_error_portal():
    from src.portales.contrato import ErrorPortal
    with pytest.raises(ErrorPortal, match="localizador"):
        portales.cargar_localizador(_portal_con(localizador="no.existe.jamas:Loc"))


def test_importar_src_portales_no_carga_scraper_ni_playwright():
    import subprocess
    import sys
    from pathlib import Path
    codigo = (
        "import sys\n"
        "import src.portales, src.portales.contrato, src.portales.modalidades, src.portales.moneda\n"
        "malos = sorted(m for m in sys.modules if m.split('.')[0] == 'playwright'"
        " or m.startswith('src.scraper') or m.startswith('src.drilldown'))\n"
        "assert not malos, malos\n"
    )
    raiz = Path(__file__).resolve().parents[1]
    r = subprocess.run([sys.executable, "-c", codigo], cwd=raiz, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
