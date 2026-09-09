"""
Tests del motor de clasificación semántica.
Usa datos mock para no consumir créditos de API en cada test.
"""
import json
from datetime import datetime, timedelta, timezone

import pytest
import yaml

from src.classifier.client_profile import (
    Brand,
    Catalog,
    CatalogCategory,
    ClientProfile,
    Intelligence,
    Notification,
    Rules,
    load_client_profile,
)
from src.classifier.engine import _parse_clasificaciones, _procesos_to_dicts
from src.classifier.rules import (
    _format_brands,
    _format_tag_taxonomy,
    get_system_prompt,
    get_user_prompt,
)
from src.scraper.models import Proceso

TZ = timezone(timedelta(hours=-6))


def make_proceso(institucion: str, descripcion: str) -> Proceso:
    return Proceso(
        institucion=institucion,
        descripcion=descripcion,
        fecha_publicacion=datetime.now(TZ),
        url_directa="https://comprasal.gob.sv/proceso/12345",
    )


@pytest.fixture
def sample_profile() -> ClientProfile:
    return ClientProfile(
        client_id="test_client",
        client_name="Empresa de Prueba",
        client_description="Integrador de soluciones empresariales de TI",
        active=True,
        catalog=Catalog(
            categories=[
                CatalogCategory(
                    id="infraestructura",
                    label="Infraestructura",
                    items=["Servidores rack", "Almacenamiento all-flash"],
                    brands=[
                        Brand(id="hpe", name="HPE", products=["servidores ProLiant"]),
                        Brand(id="purestorage", name="PureStorage", products=["FlashArray", "FlashBlade"]),
                    ],
                ),
                CatalogCategory(
                    id="ciberseguridad",
                    label="Ciberseguridad",
                    items=["Firewalls", "WAF"],
                    brands=[
                        Brand(id="fortinet", name="Fortinet", products=["FortiGate", "FortiSwitch"]),
                    ],
                ),
            ],
            inclusion_signals=["servidor, almacenamiento, firewall, FortiGate, HPE"],
            exclusion_signals=["agrícola, veterinario, medicamentos"],
            tag_taxonomy=["Servidores", "Almacenamiento", "Flash", "Firewall", "WAF"],
        ),
        rules=Rules(
            context_note="Las instituciones gubernamentales asignan categorías incorrectas.",
            categories="infraestructura, ciberseguridad, servicios_ti, otro",
        ),
        notification=Notification(
            email_to=["test@empresa.com"],
            system_admins_email=["admin@empresa.com"],
            report_title="Reporte de Prueba",
            subject_prefix="[TEST]",
            greeting="Equipo de Prueba,",
            intro_message="Licitaciones detectadas para su revisión.",
        ),
        intelligence=Intelligence(generated_at=None, processes_analyzed=0, profile=None),
    )


@pytest.fixture
def trained_profile(sample_profile) -> ClientProfile:
    sample_profile.intelligence = Intelligence(
        generated_at="2026-01-01T00:00:00",
        processes_analyzed=150,
        profile="Esta empresa participa principalmente en licitaciones de equipos de cómputo...",
    )
    return sample_profile


# ── Tests de parseo de respuesta ─────────────────────────────────────────────

class TestParseClasificaciones:
    def test_parsea_respuesta_v2_completa(self):
        procesos = [
            make_proceso("Ministerio de Defensa", "Adquisición de servidores y almacenamiento all-flash"),
            make_proceso("Ministerio de Agricultura", "Cría de cerdos en granja experimental"),
        ]
        response_json = json.dumps({
            "clasificaciones": [
                {
                    "indice": 0,
                    "es_relevante": True,
                    "categoria_detectada": "infraestructura",
                    "match_tecnico": 0.95,
                    "match_categoria": 0.90,
                    "factibilidad": 0.92,
                    "tags": ["Servidores", "Almacenamiento", "Flash"],
                    "marcas_recomendadas": [
                        {"marca": "HPE", "razon": "Servidores rack de alto rendimiento"},
                        {"marca": "PureStorage", "razon": "Almacenamiento all-flash"},
                    ],
                    "razon": "Infraestructura de data center con servidores y almacenamiento.",
                },
                {
                    "indice": 1,
                    "es_relevante": False,
                    "categoria_detectada": "otro",
                    "match_tecnico": 0.0,
                    "match_categoria": 0.0,
                    "factibilidad": 0.02,
                    "tags": [],
                    "marcas_recomendadas": [],
                    "razon": "Proceso agrícola.",
                },
            ]
        })
        result = _parse_clasificaciones(response_json, procesos)

        assert len(result) == 2

        # Proceso relevante
        r = result[0]
        assert r.es_relevante is True
        assert r.categoria_detectada == "infraestructura"
        assert r.match_tecnico == pytest.approx(0.95)
        assert r.match_categoria == pytest.approx(0.90)
        assert r.factibilidad == pytest.approx(0.92)
        assert r.confianza == pytest.approx(0.92)  # confianza = factibilidad
        assert r.tags == ["Servidores", "Almacenamiento", "Flash"]
        assert len(r.marcas_recomendadas) == 2
        assert r.marcas_recomendadas[0].marca == "HPE"
        assert r.marcas_recomendadas[1].marca == "PureStorage"

        # Proceso no relevante
        assert result[1].es_relevante is False
        assert result[1].marcas_recomendadas == []

    def test_fallback_en_json_invalido(self):
        procesos = [make_proceso("Inst A", "Descripción")]
        result = _parse_clasificaciones("esto no es json", procesos)
        assert len(result) == 1
        assert result[0].es_relevante is False
        assert result[0].tags == []
        assert result[0].marcas_recomendadas == []

    def test_proceso_sin_clasificacion_mantiene_defaults(self):
        procesos = [make_proceso("Inst A", "Desc A"), make_proceso("Inst B", "Desc B")]
        response_json = json.dumps({
            "clasificaciones": [
                {
                    "indice": 0,
                    "es_relevante": True,
                    "categoria_detectada": "ciberseguridad",
                    "match_tecnico": 0.88,
                    "match_categoria": 0.85,
                    "factibilidad": 0.86,
                    "tags": ["Firewall"],
                    "marcas_recomendadas": [{"marca": "Fortinet", "razon": "FortiGate"}],
                    "razon": "Firewall.",
                },
            ]
        })
        result = _parse_clasificaciones(response_json, procesos)
        assert result[0].es_relevante is True
        assert result[0].tags == ["Firewall"]
        assert result[1].es_relevante is False
        assert result[1].tags == []
        assert result[1].match_tecnico == 0.0

    def test_backward_compat_respuesta_v1(self):
        """Parsear una respuesta estilo v1 (solo confianza, sin campos v2) debe funcionar."""
        procesos = [make_proceso("Min. Hacienda", "Compra de servidores")]
        response_json = json.dumps({
            "clasificaciones": [
                {
                    "indice": 0,
                    "es_relevante": True,
                    "categoria_detectada": "hardware",
                    "confianza": 0.95,
                    "razon": "Menciona servidores.",
                },
            ]
        })
        result = _parse_clasificaciones(response_json, procesos)
        r = result[0]
        assert r.es_relevante is True
        # Sin factibilidad en respuesta → defaults a 0.0, confianza = 0.0
        assert r.match_tecnico == 0.0
        assert r.match_categoria == 0.0
        assert r.factibilidad == 0.0
        assert r.tags == []
        assert r.marcas_recomendadas == []


# ── Tests de construcción de prompts ─────────────────────────────────────────

class TestPrompts:
    def test_system_prompt_incluye_nombre_cliente(self, sample_profile):
        prompt = get_system_prompt(sample_profile)
        assert "Empresa de Prueba" in prompt
        assert len(prompt) > 100

    def test_system_prompt_incluye_catalogo(self, sample_profile):
        prompt = get_system_prompt(sample_profile)
        assert "Infraestructura" in prompt
        assert "Servidores rack" in prompt

    def test_system_prompt_incluye_marcas(self, sample_profile):
        prompt = get_system_prompt(sample_profile)
        assert "HPE" in prompt
        assert "PureStorage" in prompt
        assert "Fortinet" in prompt
        assert "Marcas y partnerships" in prompt

    def test_system_prompt_incluye_tag_taxonomy(self, sample_profile):
        prompt = get_system_prompt(sample_profile)
        assert "Taxonomía de tags" in prompt
        assert "Servidores" in prompt
        assert "Flash" in prompt

    def test_system_prompt_incluye_scoring_multidimensional(self, sample_profile):
        prompt = get_system_prompt(sample_profile)
        assert "match_tecnico" in prompt
        assert "match_categoria" in prompt
        assert "factibilidad" in prompt
        assert "multi-dimensional" in prompt

    def test_system_prompt_incluye_marcas_recomendadas_schema(self, sample_profile):
        prompt = get_system_prompt(sample_profile)
        assert "marcas_recomendadas" in prompt

    def test_system_prompt_incluye_context_note(self, sample_profile):
        prompt = get_system_prompt(sample_profile)
        assert "categorías incorrectas" in prompt

    def test_system_prompt_incluye_categorias_del_cliente(self, sample_profile):
        prompt = get_system_prompt(sample_profile)
        assert "infraestructura, ciberseguridad, servicios_ti, otro" in prompt

    def test_system_prompt_sin_inteligencia_no_incluye_seccion(self, sample_profile):
        prompt = get_system_prompt(sample_profile)
        assert "Inteligencia histórica" not in prompt

    def test_system_prompt_con_inteligencia_incluye_seccion(self, trained_profile):
        prompt = get_system_prompt(trained_profile)
        assert "Inteligencia histórica" in prompt
        assert "150 procesos" in prompt
        assert trained_profile.intelligence.profile in prompt

    def test_user_prompt_incluye_procesos(self):
        procesos_dicts = [
            {"referencia": "001", "institucion": "Min. Hacienda", "descripcion": "Compra de servidores", "metodo": "Licitación", "fecha": "2025-03-15 09:00"},
            {"referencia": "002", "institucion": "Policía Nacional", "descripcion": "Equipos de cómputo", "metodo": "Comparación de precios", "fecha": "2025-03-15 10:00"},
        ]
        prompt = get_user_prompt(procesos_dicts)
        assert "Min. Hacienda" in prompt
        assert "Compra de servidores" in prompt
        assert "[0]" in prompt
        assert "[1]" in prompt

    def test_procesos_to_dicts_incluye_campos(self):
        procesos = [Proceso(
            institucion="Inst A", descripcion="Desc A",
            fecha_publicacion=datetime.now(TZ), url_directa="https://x.com",
            numero_referencia="REF-001", forma_contratacion="Licitación",
        )]
        dicts = _procesos_to_dicts(procesos)
        assert dicts[0]["referencia"] == "REF-001"
        assert dicts[0]["metodo"] == "Licitación"
        assert dicts[0]["institucion"] == "Inst A"
        assert "fecha" in dicts[0]


# ── Tests de helpers de formateo ─────────────────────────────────────────────

class TestFormatHelpers:
    def test_format_brands_con_marcas(self, sample_profile):
        result = _format_brands(sample_profile)
        assert "**HPE**" in result
        assert "**PureStorage**" in result
        assert "**Fortinet**" in result
        assert "Infraestructura" in result
        assert "Ciberseguridad" in result

    def test_format_brands_sin_marcas(self):
        profile = ClientProfile(
            client_id="empty",
            client_name="Empty",
            client_description="",
            active=True,
            catalog=Catalog(
                categories=[CatalogCategory(id="hw", label="HW", items=["x"])],
                inclusion_signals=[], exclusion_signals=[],
            ),
            rules=Rules(context_note="", categories="otro"),
            notification=Notification(email_to=["a@b.com"], system_admins_email=[], report_title="T", subject_prefix="[T]", greeting="Hi", intro_message=""),
            intelligence=Intelligence(generated_at=None, processes_analyzed=0, profile=None),
        )
        assert _format_brands(profile) == "(sin marcas definidas)"

    def test_format_tag_taxonomy_con_tags(self, sample_profile):
        result = _format_tag_taxonomy(sample_profile)
        assert "Servidores" in result
        assert "Flash" in result
        assert ", " in result  # comma-separated

    def test_format_tag_taxonomy_sin_tags(self):
        profile = ClientProfile(
            client_id="empty",
            client_name="Empty",
            client_description="",
            active=True,
            catalog=Catalog(
                categories=[], inclusion_signals=[], exclusion_signals=[],
            ),
            rules=Rules(context_note="", categories="otro"),
            notification=Notification(email_to=["a@b.com"], system_admins_email=[], report_title="T", subject_prefix="[T]", greeting="Hi", intro_message=""),
            intelligence=Intelligence(generated_at=None, processes_analyzed=0, profile=None),
        )
        assert _format_tag_taxonomy(profile) == "(sin taxonomía definida)"


# ── Tests del ClientProfile ───────────────────────────────────────────────────

class TestClientProfile:
    def test_intelligence_not_trained_by_default(self, sample_profile):
        assert sample_profile.intelligence.is_trained is False

    def test_intelligence_trained_when_profile_set(self, trained_profile):
        assert trained_profile.intelligence.is_trained is True

    def test_load_from_yaml_with_brands_and_tags(self, tmp_path):
        yaml_content = {
            "client": {"id": "test", "name": "Test Co", "description": "Test", "active": True},
            "catalog": {
                "categories": [
                    {
                        "id": "infra",
                        "label": "Infraestructura",
                        "items": ["Servidores"],
                        "brands": [
                            {"id": "hpe", "name": "HPE", "products": ["ProLiant", "storage"]},
                        ],
                    },
                ],
                "inclusion_signals": ["servidor"],
                "exclusion_signals": ["alimentos"],
                "tag_taxonomy": ["Servidores", "Almacenamiento"],
            },
            "rules": {
                "context_note": "Nota de contexto.",
                "categories": "infraestructura, otro",
            },
            "notification": {
                "email_to": ["test@test.com", "otro@test.com"],
                "report_title": "Reporte Test",
                "subject_prefix": "[TEST]",
                "greeting": "Hola,",
                "intro_message": "Mensaje intro.",
            },
            "classification": {"relevance_threshold": 0.6, "confidence_threshold": 0.7},
            "intelligence": {"generated_at": None, "processes_analyzed": 0, "profile": None},
        }
        (tmp_path / "test.yaml").write_text(yaml.dump(yaml_content, allow_unicode=True))

        profile = load_client_profile("test", str(tmp_path))
        assert profile.client_name == "Test Co"
        assert profile.active is True
        assert len(profile.catalog.categories) == 1
        assert len(profile.catalog.categories[0].brands) == 1
        assert profile.catalog.categories[0].brands[0].name == "HPE"
        assert profile.catalog.categories[0].brands[0].products == ["ProLiant", "storage"]
        assert profile.catalog.tag_taxonomy == ["Servidores", "Almacenamiento"]
        assert profile.rules.categories == "infraestructura, otro"

    def test_load_from_yaml_without_brands(self, tmp_path):
        """YAML sin brands ni tag_taxonomy debe cargar con defaults vacíos."""
        yaml_content = {
            "client": {"id": "legacy", "name": "Legacy Co", "description": "Legacy", "active": True},
            "catalog": {
                "categories": [{"id": "hw", "label": "Hardware", "items": ["Laptops"]}],
                "inclusion_signals": ["laptop"],
                "exclusion_signals": ["alimentos"],
            },
            "rules": {"context_note": "Nota.", "categories": "hardware, otro"},
            "notification": {
                "email_to": "single@test.com",
                "report_title": "T", "subject_prefix": "[T]", "greeting": "Hi", "intro_message": "",
            },
            "classification": {},
            "intelligence": {"generated_at": None, "processes_analyzed": 0, "profile": None},
        }
        (tmp_path / "legacy.yaml").write_text(yaml.dump(yaml_content, allow_unicode=True))

        profile = load_client_profile("legacy", str(tmp_path))
        assert profile.catalog.categories[0].brands == []
        assert profile.catalog.tag_taxonomy == []

    def test_inactive_client_not_loaded_by_load_all(self, tmp_path):
        from src.classifier.client_profile import load_all_active_profiles

        active = {
            "client": {"id": "active", "name": "Activo", "description": "X", "active": True},
            "catalog": {"categories": [], "inclusion_signals": [], "exclusion_signals": []},
            "rules": {"context_note": "", "categories": "otro"},
            "notification": {"email_to": "a@a.com", "report_title": "T", "subject_prefix": "[A]", "greeting": "Hi", "intro_message": ""},
            "classification": {}, "intelligence": {"generated_at": None, "processes_analyzed": 0, "profile": None},
        }
        inactive = dict(active)
        inactive["client"] = {"id": "inactive", "name": "Inactivo", "description": "X", "active": False}

        (tmp_path / "active.yaml").write_text(yaml.dump(active, allow_unicode=True))
        (tmp_path / "inactive.yaml").write_text(yaml.dump(inactive, allow_unicode=True))

        profiles = load_all_active_profiles(str(tmp_path))
        assert len(profiles) == 1
        assert profiles[0].client_id == "active"
