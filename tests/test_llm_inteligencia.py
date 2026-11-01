"""Tests del contrato ampliado de la capa LLM (capa de inteligencia C0).

Cubre lo que agrega I0 sin cambiar lo existente:

1. **Compatibilidad hacia atrás.** Un `Pedido` armado con los campos viejos
   produce exactamente el mismo request que antes: ni `temperature`, ni
   `seed`, ni `response_format`, ni siquiera como `None`.
2. **Capacidades por modelo.** Temperatura y esquema solo viajan a los modelos
   que los admiten (Llama 4 Scout/Maverick; temperatura también Haiku 4.5).
   Los que razonan (xai/google), Sonnet 5 y lo desconocido no los reciben.
3. **Caída a "sin esquema".** Un 400 con `response_format` se reintenta UNA vez
   sin él; si pasa, el esquema se apaga para ese modelo el resto del proceso.
4. **`prompt_hash` estable y sin texto**, y el libro vuelca `contexto` y la
   huella en `detalle` sin contenido.
5. **Config** lee `LLM_TEMPERATURA` y los flags `INTEL_*`/`MARCA_VEREDICTO`.

Sin llamadas reales a OCI ni a Anthropic. Las pruebas que construyen objetos
del SDK de OCI piden el SUBMÓDULO (`oci.generative_ai_inference`): el `oci/`
de la raíz del repo sombrea al paquete y `importorskip("oci")` no saltea.
"""
from __future__ import annotations

import json
import logging
import types

import pytest
from tenacity import wait_none

from src.llm import modelos, proveedor_anthropic, proveedor_oci, registro
from src.llm.contrato import (
    BloqueDocumento,
    BloqueImagen,
    BloqueTexto,
    LLMError,
    Pedido,
    Respuesta,
    Uso,
    prompt_hash,
)

SCOUT = "meta.llama-4-scout-17b-16e-instruct"
MAVERICK = "meta.llama-4-maverick-17b-128e-instruct-fp8"
ESQUEMA = {
    "type": "object",
    "properties": {"viabilidad": {"type": "string", "enum": ["GO", "NO-GO", "INCIERTO"]}},
    "required": ["viabilidad"],
    "additionalProperties": False,
}
TEXTO_SECRETO = "CLAUSULA-CONFIDENCIAL-XYZZY-42"
SISTEMA_SECRETO = "INSTRUCCION-DE-SISTEMA-PLUGH-77"


def _cfg(**kw):
    base = dict(
        llm_provider="oci",
        oci_region="us-chicago-1",
        oci_genai_compartment_ocid="ocid1.compartment.oc1..aaaaaaaaejemplo",
        oci_max_tokens_salida=0,
        llm_timeout_s=600,
    )
    base.update(kw)
    return types.SimpleNamespace(**base)


def _pedido(**kw):
    base = dict(etapa="triaje", contenido=[BloqueTexto(texto="¿GO o NO-GO?")], sistema="Decidí.")
    base.update(kw)
    return Pedido(**base)


@pytest.fixture(autouse=True)
def _capacidades_limpias():
    proveedor_oci.reiniciar_capacidades()
    yield
    proveedor_oci.reiniciar_capacidades()


# ── 1. Compatibilidad: los campos nuevos son opcionales ─────────────────────


def test_pedido_viejo_tiene_los_campos_nuevos_en_su_default():
    p = Pedido(etapa="triaje", contenido=[BloqueTexto(texto="x")])
    assert (p.temperatura, p.semilla, p.esquema, p.nombre_esquema, p.contexto) == (
        None, None, None, "", None,
    )


def test_respuesta_trae_logprob_decision_en_None():
    r = Respuesta(texto="", uso=None, modelo="m", proveedor="oci")
    assert r.logprob_decision is None


def test_un_pedido_viejo_produce_el_mismo_request_de_oci():
    peticion = proveedor_oci.traducir_pedido(_cfg(), _pedido())
    assert set(peticion) == {"api_format", "compartment_id", "model_id", "max_tokens", "messages"}


# ── 2. Capacidades por modelo ────────────────────────────────────────────────


@pytest.mark.parametrize("modelo", [SCOUT, MAVERICK])
def test_llama_4_soporta_esquema_y_temperatura(modelo):
    assert modelos.soporta_esquema(modelo) is True
    assert modelos.soporta_temperatura(modelo) is True


@pytest.mark.parametrize(
    "modelo", ["xai.grok-4.6", "google.gemini-2.5-flash", "claude-sonnet-5", "modelo.nuevo-x", ""]
)
def test_los_que_razonan_o_lo_rechazan_no_reciben_temperatura(modelo):
    assert modelos.soporta_temperatura(modelo) is False
    assert modelos.soporta_esquema(modelo) is False


@pytest.mark.parametrize("modelo", ["claude-haiku-4-5", "claude-haiku-4-5-20251001"])
def test_haiku_4_5_admite_temperatura_pero_el_esquema_no_va_por_la_api(modelo):
    assert modelos.soporta_temperatura(modelo) is True
    assert modelos.soporta_esquema(modelo) is False


def test_temperatura_configurada_lee_cfg_y_tolera_basura():
    assert modelos.temperatura_configurada(types.SimpleNamespace(llm_temperatura=0.0)) == 0.0
    assert modelos.temperatura_configurada(types.SimpleNamespace(llm_temperatura=None)) is None
    assert modelos.temperatura_configurada(types.SimpleNamespace()) is None
    assert modelos.temperatura_configurada(types.SimpleNamespace(llm_temperatura="x")) is None


# ── OCI: qué parámetros viajan en el request ─────────────────────────────────


def test_scout_recibe_temperatura_semilla_y_esquema():
    pedido = _pedido(temperatura=0.0, semilla=7, esquema=ESQUEMA, nombre_esquema="triaje_v1")
    peticion = proveedor_oci.traducir_pedido(_cfg(), pedido)
    assert peticion["temperature"] == 0.0
    assert peticion["seed"] == 7
    assert peticion["response_format"] == {
        "type": "JSON_SCHEMA",
        "json_schema": {"name": "triaje_v1", "schema": ESQUEMA, "is_strict": True},
    }


def test_sin_nombre_de_esquema_se_usa_uno_por_defecto():
    peticion = proveedor_oci.traducir_pedido(_cfg(), _pedido(esquema=ESQUEMA))
    assert peticion["response_format"]["json_schema"]["name"]


@pytest.mark.parametrize("familia", ["xai", "google"])
def test_las_familias_que_razonan_no_reciben_nada_de_c0(familia):
    pedido = _pedido(temperatura=0.0, semilla=7, esquema=ESQUEMA)
    peticion = proveedor_oci.traducir_pedido(_cfg(oci_familia=familia), pedido)
    assert "temperature" not in peticion
    assert "seed" not in peticion
    assert "response_format" not in peticion


def test_solo_la_temperatura_no_arrastra_la_semilla():
    peticion = proveedor_oci.traducir_pedido(_cfg(), _pedido(temperatura=0.2))
    assert peticion["temperature"] == 0.2
    assert "seed" not in peticion and "response_format" not in peticion


def _sdk_o_skip():
    pytest.importorskip(
        "oci.generative_ai_inference",
        reason="el SDK de OCI es un extra opcional: pip install '.[oci]'",
    )


def test_el_generic_chat_request_lleva_los_parametros_cuando_corresponde():
    _sdk_o_skip()
    from oci.generative_ai_inference.models import JsonSchemaResponseFormat

    pedido = _pedido(temperatura=0.0, semilla=42, esquema=ESQUEMA, nombre_esquema="triaje_v1")
    detalles = proveedor_oci._a_objetos(proveedor_oci.traducir_pedido(_cfg(), pedido))
    req = detalles.chat_request
    assert req.temperature == 0.0
    assert req.seed == 42
    assert isinstance(req.response_format, JsonSchemaResponseFormat)
    assert req.response_format.type == "JSON_SCHEMA"
    assert req.response_format.json_schema.name == "triaje_v1"
    assert req.response_format.json_schema.schema == ESQUEMA
    assert req.response_format.json_schema.is_strict is True


def test_el_generic_chat_request_de_un_pedido_viejo_no_los_lleva():
    _sdk_o_skip()
    detalles = proveedor_oci._a_objetos(proveedor_oci.traducir_pedido(_cfg(), _pedido()))
    req = detalles.chat_request
    assert req.temperature is None
    assert req.seed is None
    assert req.response_format is None


# ── 3. Caída a "sin esquema" ante un 400 ─────────────────────────────────────


class _ServiceErrorFalso(Exception):
    def __init__(self, status):
        super().__init__(f"HTTP {status}")
        self.status = status


def _respuesta_cruda(texto='{"viabilidad": "GO"}'):
    mensaje = types.SimpleNamespace(content=[types.SimpleNamespace(text=texto)])
    choice = types.SimpleNamespace(message=mensaje, finish_reason="stop")
    chat = types.SimpleNamespace(
        choices=[choice], usage=types.SimpleNamespace(prompt_tokens=10, completion_tokens=5)
    )
    return types.SimpleNamespace(data=types.SimpleNamespace(chat_response=chat))


@pytest.fixture
def sdk_falso(monkeypatch):
    """El SDK reducido a lo que miran `_a_objetos` y `_traducir_error`.

    `GenericChatRequest` es un SimpleNamespace: así el test ve los kwargs que
    se le pasaron sin necesitar el SDK instalado.
    """

    class _GCR(types.SimpleNamespace):
        API_FORMAT_GENERIC = "GENERIC"

    sdk = types.SimpleNamespace(
        ServiceError=_ServiceErrorFalso,
        RequestException=type("_RE", (Exception,), {}),
        ConnectTimeout=type("_CT", (Exception,), {}),
        GenericChatRequest=_GCR,
        JsonSchemaResponseFormat=types.SimpleNamespace,
        ResponseJsonSchema=types.SimpleNamespace,
        TextContent=types.SimpleNamespace,
        ImageContent=types.SimpleNamespace,
        ImageUrl=types.SimpleNamespace,
        SystemMessage=types.SimpleNamespace,
        UserMessage=types.SimpleNamespace,
        ChatDetails=types.SimpleNamespace,
        OnDemandServingMode=types.SimpleNamespace,
    )
    monkeypatch.setattr(proveedor_oci, "_sdk_cache", sdk)
    monkeypatch.setattr(proveedor_oci, "_ESPERA", wait_none())
    return sdk


class _ClienteQueRechazaElEsquema:
    """400 a todo request con `response_format`; responde bien sin él."""

    def __init__(self, *, rechaza_siempre=False):
        self.requests = []
        self.rechaza_siempre = rechaza_siempre

    def chat(self, detalles):
        self.requests.append(detalles.chat_request)
        con_esquema = getattr(detalles.chat_request, "response_format", None) is not None
        if con_esquema or self.rechaza_siempre:
            raise _ServiceErrorFalso(400)
        return _respuesta_cruda()


def test_un_400_por_esquema_reintenta_una_vez_sin_el_y_lo_apaga(sdk_falso, caplog):
    cliente = _ClienteQueRechazaElEsquema()
    pedido = _pedido(temperatura=0.0, esquema=ESQUEMA, nombre_esquema="triaje_v1")

    with caplog.at_level(logging.WARNING, logger="src.llm.proveedor_oci"):
        r = proveedor_oci.completar(_cfg(), pedido, cliente=cliente)

    assert r.texto == '{"viabilidad": "GO"}'
    assert len(cliente.requests) == 2
    assert cliente.requests[0].response_format is not None
    assert getattr(cliente.requests[1], "response_format", None) is None
    # La temperatura NO se pierde en el reintento: solo se saca el esquema.
    assert cliente.requests[1].temperature == 0.0
    assert any("APAGADO" in m for m in caplog.messages)

    # El resto del proceso ya no lo manda: una sola llamada, sin 400.
    otro = _ClienteQueRechazaElEsquema()
    proveedor_oci.completar(_cfg(), pedido, cliente=otro)
    assert len(otro.requests) == 1
    assert getattr(otro.requests[0], "response_format", None) is None


def test_reiniciar_capacidades_vuelve_a_encender_el_esquema(sdk_falso):
    proveedor_oci.completar(
        _cfg(), _pedido(esquema=ESQUEMA), cliente=_ClienteQueRechazaElEsquema()
    )
    assert "response_format" not in proveedor_oci.traducir_pedido(_cfg(), _pedido(esquema=ESQUEMA))
    proveedor_oci.reiniciar_capacidades()
    assert "response_format" in proveedor_oci.traducir_pedido(_cfg(), _pedido(esquema=ESQUEMA))


def test_si_sin_esquema_tambien_falla_se_lanza_y_no_se_apaga(sdk_falso):
    """El 400 era por otra cosa: la capacidad sigue encendida."""
    cliente = _ClienteQueRechazaElEsquema(rechaza_siempre=True)
    with pytest.raises(LLMError):
        proveedor_oci.completar(_cfg(), _pedido(esquema=ESQUEMA), cliente=cliente)
    assert len(cliente.requests) == 2
    assert "response_format" in proveedor_oci.traducir_pedido(_cfg(), _pedido(esquema=ESQUEMA))


def test_un_400_sin_esquema_no_reintenta(sdk_falso):
    cliente = _ClienteQueRechazaElEsquema(rechaza_siempre=True)
    with pytest.raises(LLMError):
        proveedor_oci.completar(_cfg(), _pedido(), cliente=cliente)
    assert len(cliente.requests) == 1


# ── Anthropic: temperatura solo si viene y el modelo la admite ───────────────


class _AnthropicFalso:
    def __init__(self):
        self.kwargs = []
        self.messages = self

    def create(self, **kwargs):
        self.kwargs.append(kwargs)
        return types.SimpleNamespace(
            content=[types.SimpleNamespace(type="text", text="ok")],
            usage=types.SimpleNamespace(input_tokens=1, output_tokens=1),
            stop_reason="end_turn",
        )


def _cfg_anthropic(**kw):
    base = dict(llm_provider="anthropic", anthropic_api_key="sk-ejemplo")
    base.update(kw)
    return types.SimpleNamespace(**base)


def test_anthropic_sin_temperatura_no_cambia_el_request():
    cli = _AnthropicFalso()
    proveedor_anthropic.completar(_cfg_anthropic(), _pedido(esquema=ESQUEMA, semilla=3), cliente=cli)
    assert "temperature" not in cli.kwargs[0]
    assert "seed" not in cli.kwargs[0]
    assert set(cli.kwargs[0]) == {"model", "max_tokens", "messages", "system"}


def test_anthropic_haiku_recibe_la_temperatura():
    cli = _AnthropicFalso()
    proveedor_anthropic.completar(_cfg_anthropic(), _pedido(temperatura=0.0), cliente=cli)
    assert cli.kwargs[0]["model"].startswith("claude-haiku-4-5")
    assert cli.kwargs[0]["temperature"] == 0.0


def test_anthropic_sonnet_5_no_recibe_temperatura():
    """Sonnet 5 responde 400 a `temperature`: se omite en vez de romper el análisis."""
    cli = _AnthropicFalso()
    pedido = _pedido(etapa="analisis", temperatura=0.0)
    proveedor_anthropic.completar(_cfg_anthropic(model_analysis="claude-sonnet-5"), pedido, cliente=cli)
    assert "temperature" not in cli.kwargs[0]


# ── 4. prompt_hash ───────────────────────────────────────────────────────────


def test_prompt_hash_es_estable_y_corto():
    a = prompt_hash(_pedido(sistema=SISTEMA_SECRETO, contenido=[BloqueTexto(texto=TEXTO_SECRETO)]))
    b = prompt_hash(_pedido(sistema=SISTEMA_SECRETO, contenido=[BloqueTexto(texto=TEXTO_SECRETO)]))
    assert a == b
    assert len(a) == 16 and all(c in "0123456789abcdef" for c in a)


def test_prompt_hash_normaliza_blancos_y_unicode():
    compuesto = "licitación  de\n servidores "
    descompuesto = "licitación de servidores"
    a = prompt_hash(_pedido(contenido=[BloqueTexto(texto=compuesto)]))
    b = prompt_hash(_pedido(contenido=[BloqueTexto(texto=descompuesto)]))
    assert a == b


def test_prompt_hash_cambia_con_el_contenido_y_el_sistema():
    base = prompt_hash(_pedido())
    assert prompt_hash(_pedido(sistema="Otro sistema.")) != base
    assert prompt_hash(_pedido(contenido=[BloqueTexto(texto="otra cosa")])) != base
    assert prompt_hash(_pedido(contenido=[BloqueImagen(datos_b64="QUJD")])) != prompt_hash(
        _pedido(contenido=[BloqueImagen(datos_b64="REVG")])
    )


def test_prompt_hash_ignora_parametros_y_el_directorio_temporal():
    base = _pedido(contenido=[BloqueDocumento(ruta="/tmp/a/pliego.pdf", paginas=3)])
    otro = _pedido(
        contenido=[BloqueDocumento(ruta="/tmp/b/pliego.pdf", paginas=3)],
        temperatura=0.0, semilla=1, contexto={"cliente_id": "x"},
    )
    assert prompt_hash(base) == prompt_hash(otro)


def test_prompt_hash_no_contiene_texto():
    h = prompt_hash(_pedido(sistema=SISTEMA_SECRETO, contenido=[BloqueTexto(texto=TEXTO_SECRETO)]))
    assert TEXTO_SECRETO not in h and SISTEMA_SECRETO not in h


# ── Registro: contexto y huella en `detalle`, sin contenido ──────────────────


def _cfg_registro(tmp_path):
    return types.SimpleNamespace(
        llm_provider="oci",
        oci_familia="nativo",
        data_dir=str(tmp_path),
        llm_registro_path=str(tmp_path / "llm_consumo.jsonl"),
        llm_registro_habilitado=True,
        app_version="test",
    )


def _respuesta():
    return Respuesta(
        texto="RESPUESTA", uso=Uso(input_tokens=10, output_tokens=5), modelo=SCOUT, proveedor="oci"
    )


def test_el_registro_vuelca_contexto_y_prompt_hash_sin_contenido(tmp_path, monkeypatch):
    monkeypatch.setattr(registro, "_adb_primaria", lambda cfg: False)
    cfg = _cfg_registro(tmp_path)
    pedido = _pedido(
        sistema=SISTEMA_SECRETO,
        contenido=[BloqueTexto(texto=TEXTO_SECRETO)],
        temperatura=0.0,
        semilla=7,
        esquema=ESQUEMA,
        nombre_esquema="triaje_v1",
        contexto={
            "proceso_gid": "96-2026-P0011",
            "cliente_id": "cliente_ejemplo",
            "capa": "C0",
            "variante": "sombra",
            "anidado": {"texto": TEXTO_SECRETO},
            "largo": "x" * 1000,
        },
    )
    registro.registrar(cfg, pedido, proveedor="oci", respuesta=_respuesta(), latencia_ms=5)

    crudo = (tmp_path / "llm_consumo.jsonl").read_text(encoding="utf-8")
    assert TEXTO_SECRETO not in crudo and SISTEMA_SECRETO not in crudo
    linea = json.loads(crudo.splitlines()[-1])

    assert linea["prompt_hash"] == prompt_hash(pedido)
    assert linea["contexto"]["proceso_gid"] == "96-2026-P0011"
    assert linea["contexto"]["capa"] == "C0"
    assert "anidado" not in linea["contexto"]
    assert len(linea["contexto"]["largo"]) == registro.MAX_CONTEXTO_VALOR
    assert linea["parametros"] == {"temperatura": 0.0, "semilla": 7, "esquema": "triaje_v1"}

    detalle = json.loads(registro._binds_adb(linea)["detalle"])
    assert detalle["prompt_hash"] == linea["prompt_hash"]
    assert detalle["contexto"]["cliente_id"] == "cliente_ejemplo"
    assert detalle["parametros"]["esquema"] == "triaje_v1"
    assert TEXTO_SECRETO not in json.dumps(detalle)


def test_el_prompt_hash_del_call_site_manda(tmp_path, monkeypatch):
    monkeypatch.setattr(registro, "_adb_primaria", lambda cfg: False)
    cfg = _cfg_registro(tmp_path)
    pedido = _pedido(contexto={"prompt_hash": "abc123"})
    registro.registrar(cfg, pedido, proveedor="oci", respuesta=_respuesta(), latencia_ms=1)
    linea = json.loads((tmp_path / "llm_consumo.jsonl").read_text().splitlines()[-1])
    assert linea["prompt_hash"] == "abc123"


def test_una_linea_vieja_del_spool_sin_campos_c0_sigue_insertandose():
    linea = {"ts": "2026-11-01T07:00:00-06:00", "entrada": {}, "uso": {}}
    detalle = json.loads(registro._binds_adb(linea)["detalle"])
    assert set(detalle) == {"bloques", "uso_crudo"}


# ── 5. Config ────────────────────────────────────────────────────────────────

_FLAGS = [f"INTEL_C{i}" for i in range(9)] + ["MARCA_VEREDICTO"]


@pytest.fixture
def entorno_config(monkeypatch):
    monkeypatch.setenv("COMPRASAL_EMAIL", "bot@example.com")
    monkeypatch.setenv("COMPRASAL_PASSWORD", "x")
    monkeypatch.setenv("SMTP_USER", "bot@example.com")
    monkeypatch.setenv("SMTP_PASSWORD", "x")
    for flag in _FLAGS + ["LLM_TEMPERATURA"]:
        monkeypatch.delenv(flag, raising=False)
    from src.config import Config

    return Config


def test_config_por_defecto_temperatura_cero_y_capas_apagadas(entorno_config):
    cfg = entorno_config()
    assert cfg.llm_temperatura == 0.0
    for flag in _FLAGS:
        assert getattr(cfg, flag.lower()) == "off"


def test_config_lee_los_flags_y_la_temperatura(entorno_config, monkeypatch):
    monkeypatch.setenv("INTEL_C0", "Sombra")
    monkeypatch.setenv("INTEL_C8", " activo ")
    monkeypatch.setenv("MARCA_VEREDICTO", "sombra")
    monkeypatch.setenv("INTEL_C3", "encendido")  # typo ⇒ off, nunca enciende
    monkeypatch.setenv("LLM_TEMPERATURA", "0.3")
    cfg = entorno_config()
    assert (cfg.intel_c0, cfg.intel_c8, cfg.marca_veredicto) == ("sombra", "activo", "sombra")
    assert cfg.intel_c3 == "off"
    assert cfg.llm_temperatura == 0.3
    assert modelos.temperatura_configurada(cfg) == 0.3


def test_llm_temperatura_vacia_es_None(entorno_config, monkeypatch):
    monkeypatch.setenv("LLM_TEMPERATURA", "")
    assert entorno_config().llm_temperatura is None
