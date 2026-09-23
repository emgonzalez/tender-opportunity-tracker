"""Tests del adaptador de OCI Generative AI.

La suite está partida en dos mitades por una razón de diseño, no de comodidad:

- **La traducción es una función pura** (`traducir_pedido`) que devuelve dicts
  en la forma de alambre de OCI. Todo lo que se puede afirmar sobre *qué se le
  manda al modelo* —el clamp a 4096, el data-URI, que un PDF escaneado se abra
  en N imágenes, que nunca salga un bloque `DOCUMENT`— se afirma acá, **sin el
  SDK instalado**. Es el equivalente de lo que `tests/test_triage_raster.py`
  puede preguntarle al payload de Anthropic, y la razón por la que el adaptador
  de OCI es inspeccionable en vez de una caja negra de objetos generados.
- **La conversión dict → objetos del SDK** va aparte, detrás de
  `pytest.importorskip("oci.generative_ai_inference")`.

⚠️ **`importorskip("oci")` NO saltea en este repo** y da un falso positivo: hay
un directorio `oci/` en la raíz (scripts de despliegue) sin `__init__.py` que
Python trata como *namespace package*, así que `import oci` tiene éxito sin el
SDK y el test sigue hasta morir con un `AttributeError` opaco. Hay que pedir el
**submódulo**. Verificado, no supuesto.
"""
from __future__ import annotations

import types

import pytest
from tenacity import wait_none

from src import llm
from src.llm import modelos, proveedor_oci
from src.llm.contrato import (
    BloqueDocumento,
    BloqueImagen,
    BloqueTexto,
    LLMError,
    Pedido,
)

COMPARTMENT = "ocid1.compartment.oc1..aaaaaaaaejemplo"


def _cfg(**kw):
    """Config falsa: `SimpleNamespace`, el idioma de `tests/test_alerts.py`."""
    base = dict(
        llm_provider="oci",
        oci_region="us-chicago-1",
        oci_genai_compartment_ocid=COMPARTMENT,
        oci_max_tokens_salida=4096,
        oci_analisis_raster_pages=20,
        llm_timeout_s=600,
    )
    base.update(kw)
    return types.SimpleNamespace(**base)


def _tipos(peticion: dict) -> list[str]:
    """Los discriminantes del mensaje de usuario, aplanados."""
    return [b["type"] for b in peticion["messages"][-1]["content"]]


# ── Clamp de max_tokens ──────────────────────────────────────────────────────


def test_max_tokens_se_recorta_al_tope_de_4096():
    """16.384 es lo que pide el clasificador; OCI responde 400 por encima de 4096.

    No es una optimización: el tope se valida **en el request**
    (`400 Invalid 'maxTokens': Value is greater than maximum: 4096`, medido en
    la Tanda 0). Sin el clamp, la primera llamada de la corrida de las 07:00
    falla.
    """
    pedido = Pedido(etapa="clasificacion", contenido=[BloqueTexto(texto="x")], max_tokens=16_384)
    assert proveedor_oci.traducir_pedido(_cfg(), pedido)["max_tokens"] == 4096


def test_max_tokens_por_debajo_del_tope_pasa_intacto():
    pedido = Pedido(etapa="triaje", contenido=[BloqueTexto(texto="x")], max_tokens=300)
    assert proveedor_oci.traducir_pedido(_cfg(), pedido)["max_tokens"] == 300


def test_el_tope_se_puede_bajar_pero_no_subir():
    pedido = Pedido(etapa="analisis", contenido=[BloqueTexto(texto="x")], max_tokens=16_000)
    assert proveedor_oci.traducir_pedido(_cfg(oci_max_tokens_salida=1000), pedido)[
        "max_tokens"
    ] == 1000
    # Por encima del máximo que acepta la API se ignora la perilla: permitirlo
    # solo serviría para romper la corrida con un 400.
    assert proveedor_oci.traducir_pedido(_cfg(oci_max_tokens_salida=99_999), pedido)[
        "max_tokens"
    ] == 4096


# ── Imágenes: el data-URI ────────────────────────────────────────────────────


def test_bloque_imagen_sale_como_data_uri_bien_formado():
    """La forma que come `ImageUrl.url`, verificada contra la API real."""
    pedido = Pedido(
        etapa="triaje",
        contenido=[BloqueImagen(datos_b64="QUJD", media_type="image/jpeg")],
        max_tokens=100,
    )
    bloques = proveedor_oci.traducir_pedido(_cfg(), pedido)["messages"][-1]["content"]
    assert bloques == [
        {"type": "IMAGE", "image_url": {"url": "data:image/jpeg;base64,QUJD"}}
    ]


def test_el_media_type_del_bloque_manda_en_el_data_uri():
    pedido = Pedido(
        etapa="triaje",
        contenido=[BloqueImagen(datos_b64="QUJD", media_type="image/png")],
        max_tokens=100,
    )
    url = proveedor_oci.traducir_pedido(_cfg(), pedido)["messages"][-1]["content"][0][
        "image_url"
    ]["url"]
    assert url.startswith("data:image/png;base64,")


# ── Documentos: la política por formato ──────────────────────────────────────


@pytest.fixture
def pdf_falso(monkeypatch):
    """Doble de `src.drilldown.pdf`: registra con qué se lo llamó.

    Se parchea el módulo real (no el import perezoso) porque el adaptador lo
    importa dentro de la función, así que ve el atributo ya pisado.
    """
    llamadas: dict[str, tuple] = {}

    def _texto(ruta, n):
        llamadas["texto"] = (ruta, n)
        return "OBJETO DE LA CONTRATACION: firewalls perimetrales"

    def _raster(ruta, n):
        llamadas["raster"] = (ruta, n)
        return [f"pagina{i}" for i in range(n)]

    monkeypatch.setattr("src.drilldown.pdf.extract_text_first_pages", _texto)
    monkeypatch.setattr("src.drilldown.pdf.rasterizar_paginas", _raster)
    return llamadas


def test_documento_nativo_se_traduce_a_un_unico_bloque_de_texto(pdf_falso):
    """PDF con capa de texto: se manda el texto, no imágenes. Y es más barato."""
    pedido = Pedido(
        etapa="analisis",
        contenido=[BloqueDocumento(ruta="/tmp/pliego.pdf", paginas=12, escaneado=False)],
        max_tokens=4096,
    )
    peticion = proveedor_oci.traducir_pedido(_cfg(), pedido)

    assert _tipos(peticion) == ["TEXT"]
    assert "firewalls perimetrales" in peticion["messages"][-1]["content"][0]["text"]
    assert pdf_falso["texto"] == ("/tmp/pliego.pdf", 12)
    assert "raster" not in pdf_falso


def test_documento_escaneado_se_abre_en_n_imagenes(pdf_falso):
    """20 páginas rasterizadas = 20 bloques IMAGE, uno por página."""
    pedido = Pedido(
        etapa="analisis",
        contenido=[BloqueDocumento(ruta="/tmp/escaneado.pdf", paginas=60, escaneado=True)],
        max_tokens=4096,
    )
    peticion = proveedor_oci.traducir_pedido(_cfg(oci_analisis_raster_pages=20), pedido)

    assert _tipos(peticion) == ["IMAGE"] * 20
    assert pdf_falso["raster"] == ("/tmp/escaneado.pdf", 20)
    assert peticion["messages"][-1]["content"][0]["image_url"]["url"].startswith(
        "data:image/jpeg;base64,"
    )


def test_un_escaneado_mas_corto_que_la_perilla_no_se_infla(pdf_falso):
    """No se piden 20 páginas a un PDF de 3: `rasterizar_paginas` recibe 3."""
    pedido = Pedido(
        etapa="analisis",
        contenido=[BloqueDocumento(ruta="/tmp/corto.pdf", paginas=3, escaneado=True)],
        max_tokens=4096,
    )
    peticion = proveedor_oci.traducir_pedido(_cfg(), pedido)

    assert pdf_falso["raster"] == ("/tmp/corto.pdf", 3)
    assert _tipos(peticion) == ["IMAGE"] * 3


def test_nunca_sale_un_bloque_document(pdf_falso):
    """La contracara de `assert "document" not in tipos` de test_triage_raster.

    `DocumentContent` está FUERA de v1 (toma una URL, no base64: obligaría a
    subir cada pliego a Object Storage y emitir un PAR por análisis). Si algún
    día alguien lo agrega sin leer el docstring, este test lo frena.
    """
    for escaneado in (True, False):
        pedido = Pedido(
            etapa="analisis",
            contenido=[
                BloqueTexto(texto="contexto"),
                BloqueDocumento(ruta="/tmp/p.pdf", paginas=4, escaneado=escaneado),
            ],
            max_tokens=4096,
        )
        tipos = _tipos(proveedor_oci.traducir_pedido(_cfg(), pedido))
        assert set(tipos) <= {"TEXT", "IMAGE"}
        assert "DOCUMENT" not in tipos
        assert "document" not in tipos


def test_documento_sin_paginas_conocidas_usa_los_defaults(pdf_falso):
    """`paginas=0` significa "el pipeline no supo": se cae a las constantes."""
    nativo = Pedido(
        etapa="analisis",
        contenido=[BloqueDocumento(ruta="/tmp/p.pdf", paginas=0, escaneado=False)],
    )
    proveedor_oci.traducir_pedido(_cfg(), nativo)
    assert pdf_falso["texto"][1] == proveedor_oci._PAGINAS_TEXTO_SI_DESCONOCIDO

    escaneado = Pedido(
        etapa="analisis",
        contenido=[BloqueDocumento(ruta="/tmp/p.pdf", paginas=0, escaneado=True)],
    )
    proveedor_oci.traducir_pedido(_cfg(oci_analisis_raster_pages=7), escaneado)
    assert pdf_falso["raster"][1] == 7


def test_un_bloque_desconocido_sale_como_LLMError():
    with pytest.raises(LLMError):
        proveedor_oci.traducir_bloque(_cfg(), object())


# ── El mensaje de sistema ────────────────────────────────────────────────────


def test_sistema_vacio_no_emite_mensaje_de_sistema():
    """El caso de `classifier/trainer.py`, que llama sin `system`.

    Mandar un `SystemMessage` con `""` cambiaría el prompt de una etapa que hoy
    funciona, y el cambio sería invisible hasta que la calidad baje.
    """
    pedido = Pedido(etapa="perfilado", contenido=[BloqueTexto(texto="x")], sistema="")
    roles = [m["role"] for m in proveedor_oci.traducir_pedido(_cfg(), pedido)["messages"]]
    assert roles == ["USER"]


def test_sistema_en_blanco_tampoco_emite_mensaje_de_sistema():
    pedido = Pedido(etapa="perfilado", contenido=[BloqueTexto(texto="x")], sistema="   \n ")
    roles = [m["role"] for m in proveedor_oci.traducir_pedido(_cfg(), pedido)["messages"]]
    assert roles == ["USER"]


def test_sistema_con_texto_va_primero_y_completo():
    pedido = Pedido(etapa="triaje", contenido=[BloqueTexto(texto="x")], sistema="Sos AMALIA.")
    mensajes = proveedor_oci.traducir_pedido(_cfg(), pedido)["messages"]
    assert [m["role"] for m in mensajes] == ["SYSTEM", "USER"]
    assert mensajes[0]["content"] == [{"type": "TEXT", "text": "Sos AMALIA."}]


# ── Modelo y compartment ─────────────────────────────────────────────────────


def test_el_modelo_lo_resuelve_modelos_y_cfg_lo_pisa():
    pedido = Pedido(etapa="triaje", contenido=[BloqueTexto(texto="x")])
    assert proveedor_oci.traducir_pedido(_cfg(), pedido)["model_id"] == (
        "meta.llama-4-scout-17b-16e-instruct"
    )
    pisado = _cfg(oci_model_triage="meta.llama-4-maverick-17b-128e-instruct-fp8")
    assert proveedor_oci.traducir_pedido(pisado, pedido)["model_id"] == (
        "meta.llama-4-maverick-17b-128e-instruct-fp8"
    )


def test_el_compartment_viaja_en_el_pedido():
    pedido = Pedido(etapa="triaje", contenido=[BloqueTexto(texto="x")])
    peticion = proveedor_oci.traducir_pedido(_cfg(), pedido)
    assert peticion["compartment_id"] == COMPARTMENT
    assert peticion["api_format"] == "GENERIC"


# ── El PEM del `.env`: los `\n` LITERALES ────────────────────────────────────


def test_la_clave_privada_se_desescapa_antes_de_firmar(monkeypatch):
    """`04-app.sh` escribe el PEM en UNA línea del `.env`, con `\\n` literales.

    Verificado de punta a punta: ni `python-dotenv` ni `docker run --env-file`
    lo desescapan. Sin el `.replace()`, `Signer` recibe una sola línea y falla
    al parsear la clave con un error que no menciona nada de esto — el tipo de
    bug que cuesta una tarde.
    """
    firmas: list[dict] = []

    # La firma ESPEJA la real del SDK —`private_key_file_location` sin default—
    # a propósito. El doble anterior era `def _signer(**kw)`, que acepta
    # cualquier cosa, y por eso el test pasaba mientras la VM reventaba con
    # `TypeError: missing 1 required positional argument`. Un doble más
    # permisivo que la cosa doblada no prueba nada.
    def _signer(tenancy, user, fingerprint, private_key_file_location,
                pass_phrase=None, private_key_content=None):
        firmas.append({
            "tenancy": tenancy, "user": user, "fingerprint": fingerprint,
            "private_key_file_location": private_key_file_location,
            "pass_phrase": pass_phrase, "private_key_content": private_key_content,
        })
        return "SIGNER"

    monkeypatch.setattr(
        proveedor_oci, "_sdk", lambda: types.SimpleNamespace(Signer=_signer, config=None)
    )

    pem_env = "-----BEGIN PRIVATE KEY-----\\nMIIEv\\nQIBAD\\n-----END PRIVATE KEY-----\\n"
    cfg = _cfg(
        oci_key_content=pem_env,
        oci_tenancy_ocid="ocid1.tenancy.oc1..t",
        oci_user_ocid="ocid1.user.oc1..u",
        oci_fingerprint="aa:bb:cc",
    )

    config, signer = proveedor_oci._config_y_signer(cfg)

    assert signer == "SIGNER"
    assert config == {"region": "us-chicago-1"}
    pem = firmas[0]["private_key_content"]
    assert "\\n" not in pem, "quedaron backslash-n literales: el PEM no se desescapó"
    assert pem.count("\n") == 4  # los cuatro `\n` del .env, ahora de verdad
    assert pem.startswith("-----BEGIN PRIVATE KEY-----\n")
    assert firmas[0]["fingerprint"] == "aa:bb:cc"
    assert firmas[0]["private_key_file_location"] is None


def test_los_kwargs_del_signer_entran_en_la_firma_real_del_sdk():
    """El doble de arriba solo vale si se parece al SDK; esto lo ata al de verdad.

    Se saltea donde el extra `[oci]` no está instalado —la suite corre sin él a
    propósito— pero en la laptop y en la imagen `-oci` sí corre, que son los dos
    lugares donde el desajuste importa.

    Existe porque la falla real no fue una mala idea sino un parámetro sin
    default: `Signer` exige `private_key_file_location` aunque la clave venga
    por `private_key_content`. Con un doble permisivo, eso solo se descubre en
    la VM, de noche, cuando el timer llama al modelo.
    """
    import inspect

    oci = pytest.importorskip("oci", reason="el extra [oci] no está instalado")

    inspect.signature(oci.Signer.__init__).bind(
        object(),  # self
        tenancy="ocid1.tenancy.oc1..t",
        user="ocid1.user.oc1..u",
        fingerprint="aa:bb:cc",
        private_key_file_location=None,
        private_key_content="-----BEGIN PRIVATE KEY-----\n-----END PRIVATE KEY-----\n",
    )


def test_sin_clave_en_el_env_se_cae_al_archivo_de_perfiles(monkeypatch):
    """El camino de la laptop: `~/.oci/config` con los perfiles del operador."""
    vistos: list[dict] = []

    def _from_file(**kw):
        vistos.append(kw)
        return {"region": "us-ashburn-1", "user": "ocid1.user.oc1..u"}

    monkeypatch.setattr(
        proveedor_oci,
        "_sdk",
        lambda: types.SimpleNamespace(config=types.SimpleNamespace(from_file=_from_file)),
    )

    cfg = _cfg(oci_key_content="", oci_config_file="/casa/.oci/config",
               oci_config_profile="MULTICOMPUTOS")
    config, signer = proveedor_oci._config_y_signer(cfg)

    assert signer is None
    assert vistos[0] == {"file_location": "/casa/.oci/config", "profile_name": "MULTICOMPUTOS"}
    # cfg.oci_region PISA la región del perfil: el tenant vive en Ashburn y
    # GenAI solo sirve inferencia en Chicago.
    assert config["region"] == "us-chicago-1"


def test_la_clave_privada_nunca_aparece_en_el_mensaje_de_error():
    """Los logs de la VM se leen por journalctl, sin ninguna protección."""
    pem = "-----BEGIN PRIVATE KEY-----\\nSUPERSECRETOQUENODEBEAPARECER\\n-----END-----"
    cfg = _cfg(oci_key_content=pem)
    error = proveedor_oci._traducir_error(
        RuntimeError(f"falló firmando con {pem}"), cfg=cfg, etapa="triaje"
    )
    assert "SUPERSECRETO" not in str(error)
    assert "***" in str(error)


# ── Reintentos con tenacity ──────────────────────────────────────────────────


class _ServiceErrorFalso(Exception):
    def __init__(self, status):
        super().__init__(f"HTTP {status}")
        self.status = status


class _RequestExceptionFalsa(Exception):
    pass


class _ConnectTimeoutFalso(Exception):
    pass


@pytest.fixture
def sdk_de_excepciones(monkeypatch):
    """Instala las clases de excepción que mira `_traducir_error`, sin el SDK."""
    monkeypatch.setattr(
        proveedor_oci,
        "_sdk_cache",
        types.SimpleNamespace(
            ServiceError=_ServiceErrorFalso,
            RequestException=_RequestExceptionFalsa,
            ConnectTimeout=_ConnectTimeoutFalso,
        ),
    )


@pytest.fixture
def sin_dormir(monkeypatch):
    """Tenacity sin esperas: 4 intentos con backoff real son ~30 s en CI."""
    monkeypatch.setattr(proveedor_oci, "_ESPERA", wait_none())


class _ClienteFalso:
    """Cliente que falla `fallas` veces y después responde."""

    def __init__(self, fallas, excepcion):
        self.fallas = fallas
        self.excepcion = excepcion
        self.llamadas = 0

    def chat(self, detalles):
        self.llamadas += 1
        if self.llamadas <= self.fallas:
            raise self.excepcion
        return "OK"


def test_un_429_se_reintenta_y_la_tercera_pasa(sdk_de_excepciones, sin_dormir):
    cliente = _ClienteFalso(2, _ServiceErrorFalso(429))
    resultado = proveedor_oci._chat_con_reintentos(
        cliente, "detalles", cfg=_cfg(), etapa="triaje"
    )
    assert resultado == "OK"
    assert cliente.llamadas == 3


def test_cuatro_fallas_agotan_los_intentos_y_sale_LLMError(sdk_de_excepciones, sin_dormir):
    """`stop_after_attempt(4)` = el mismo sobre de fallo que el SDK de Anthropic.

    Si los dos proveedores no reintentaran lo mismo, cambiar `LLM_PROVIDER`
    también cambiaría la tasa de errores observada y el benchmark no compararía
    nada.
    """
    cliente = _ClienteFalso(99, _ServiceErrorFalso(503))
    with pytest.raises(LLMError) as exc:
        proveedor_oci._chat_con_reintentos(cliente, "detalles", cfg=_cfg(), etapa="analisis")
    assert cliente.llamadas == 4
    assert exc.value.reintentable is True
    assert exc.value.proveedor == "oci"
    assert exc.value.etapa == "analisis"


def test_un_400_no_se_reintenta(sdk_de_excepciones, sin_dormir):
    """Un request inválido no mejora por repetirlo; reintentarlo solo gasta."""
    cliente = _ClienteFalso(99, _ServiceErrorFalso(400))
    with pytest.raises(LLMError) as exc:
        proveedor_oci._chat_con_reintentos(cliente, "detalles", cfg=_cfg(), etapa="triaje")
    assert cliente.llamadas == 1
    assert exc.value.reintentable is False


def test_los_errores_de_red_son_reintentables(sdk_de_excepciones, sin_dormir):
    """ConnectTimeout NO hereda de RequestException en este SDK: van las dos."""
    for excepcion in (_RequestExceptionFalsa("sin ruta"), _ConnectTimeoutFalso("timeout")):
        cliente = _ClienteFalso(1, excepcion)
        assert (
            proveedor_oci._chat_con_reintentos(cliente, "d", cfg=_cfg(), etapa="triaje") == "OK"
        )
        assert cliente.llamadas == 2


@pytest.mark.parametrize("codigo", sorted(proveedor_oci._HTTP_REINTENTABLES))
def test_la_tabla_de_codigos_reintentables_se_respeta(codigo, sdk_de_excepciones):
    error = proveedor_oci._traducir_error(
        _ServiceErrorFalso(codigo), cfg=_cfg(), etapa="triaje"
    )
    assert error.reintentable is True


def test_una_excepcion_ajena_al_sdk_igual_sale_como_LLMError(sdk_de_excepciones):
    """La red de contención: el mapeo a LLMError tiene que ser TOTAL.

    Si un error de OCI se escapa sin traducir, el `except anthropic.APIError` de
    `engine.py` deja de atraparlo y la semántica de fallo cambia en silencio.
    """
    error = proveedor_oci._traducir_error(
        ZeroDivisionError("algo raro"), cfg=_cfg(), etapa="clasificacion"
    )
    assert isinstance(error, LLMError)
    assert error.reintentable is False
    assert "ZeroDivisionError" in str(error)


# ── Lectura de la respuesta ──────────────────────────────────────────────────


def _respuesta_cruda(textos, *, finish_reason=None, usage=None):
    """Doble de `resp.data.chat_response` con la forma exacta del SDK."""
    mensaje = types.SimpleNamespace(
        content=[types.SimpleNamespace(text=t) for t in textos]
    )
    choice = types.SimpleNamespace(message=mensaje, finish_reason=finish_reason)
    chat_response = types.SimpleNamespace(choices=[choice], usage=usage)
    return types.SimpleNamespace(data=types.SimpleNamespace(chat_response=chat_response))


def test_el_texto_se_concatena_de_todos_los_bloques():
    datos = _respuesta_cruda(["hola ", "mundo"])
    r = proveedor_oci._respuesta_de(datos, modelo="meta.x", etapa="triaje")
    assert r.texto == "hola mundo"
    assert r.proveedor == "oci"
    assert r.modelo == "meta.x"


@pytest.mark.parametrize("motivo", ["length", "LENGTH", "max_tokens", "MAX_TOKENS"])
def test_finish_reason_de_tope_marca_truncada(motivo):
    datos = _respuesta_cruda(["a"], finish_reason=motivo)
    assert proveedor_oci._respuesta_de(datos, modelo="m", etapa="triaje").truncada is True


@pytest.mark.parametrize("motivo", ["stop", "STOP", None, ""])
def test_un_corte_normal_no_marca_truncada(motivo):
    datos = _respuesta_cruda(["a"], finish_reason=motivo)
    assert proveedor_oci._respuesta_de(datos, modelo="m", etapa="triaje").truncada is False


def test_el_usage_de_oci_se_normaliza_a_input_output_tokens():
    """OCI dice prompt/completion; el libro mayor lee input/output."""
    usage = types.SimpleNamespace(prompt_tokens=100, completion_tokens=7)
    datos = _respuesta_cruda(["a"], usage=usage)
    uso = proveedor_oci._respuesta_de(datos, modelo="m", etapa="triaje").uso
    assert (uso.input_tokens, uso.output_tokens) == (100, 7)


def test_una_respuesta_sin_chat_response_sale_como_LLMError():
    datos = types.SimpleNamespace(data=types.SimpleNamespace(chat_response=None))
    with pytest.raises(LLMError):
        proveedor_oci._respuesta_de(datos, modelo="m", etapa="triaje")


# ── Respuestas mutiladas: `choice.message` puede ser None ────────────────────
#
# No es defensa especulativa. Medido el 2026-09-19 contra
# `google.gemini-2.5-flash` con max_tokens=20: la API devuelve
# `finish_reason=max_tokens`, `message=None` y ningún texto, porque gastó el
# presupuesto entero en tokens de razonamiento. Un acceso directo a
# `choice.message.content` explota con `AttributeError` ahí mismo, y el triaje
# de un pliego pesado se cae en vez de reintentar.


def _crudo(choices, *, usage=None):
    """Igual que `_respuesta_cruda`, pero con los `choices` armados a mano."""
    chat_response = types.SimpleNamespace(choices=choices, usage=usage)
    return types.SimpleNamespace(data=types.SimpleNamespace(chat_response=chat_response))


def _choice(mensaje, *, finish_reason=None):
    return types.SimpleNamespace(message=mensaje, finish_reason=finish_reason)


def _mensaje(*textos):
    return types.SimpleNamespace(content=[types.SimpleNamespace(text=t) for t in textos])


def test_un_choice_sin_mensaje_da_texto_vacio_y_truncada():
    """El caso Gemini exacto: se responde texto vacío + truncada, NO una excepción.

    `truncada=True` es lo que hace que el log diga la verdad —se cortó por tope
    de salida— en vez de reportarlo como `parse_error`, culpando al modelo de
    haber dicho algo inválido cuando no dijo nada.
    """
    datos = _crudo([_choice(None, finish_reason="max_tokens")])

    r = proveedor_oci._respuesta_de(datos, modelo="google.gemini-2.5-flash", etapa="triaje")

    assert r.texto == ""
    assert r.truncada is True


def test_choices_vacio_no_explota():
    r = proveedor_oci._respuesta_de(_crudo([]), modelo="m", etapa="triaje")
    assert (r.texto, r.truncada) == ("", False)


def test_choices_none_no_explota():
    r = proveedor_oci._respuesta_de(_crudo(None), modelo="m", etapa="triaje")
    assert (r.texto, r.truncada) == ("", False)


def test_un_choice_sin_mensaje_no_tapa_al_que_si_trae_texto():
    """Un choice mutilado se saltea; los demás se leen igual."""
    datos = _crudo([_choice(None), _choice(_mensaje("el ", "veredicto"))])

    r = proveedor_oci._respuesta_de(datos, modelo="m", etapa="triaje")

    assert r.texto == "el veredicto"


def test_un_mensaje_sin_content_tampoco_explota():
    datos = _crudo([_choice(types.SimpleNamespace(content=None))])
    assert proveedor_oci._respuesta_de(datos, modelo="m", etapa="triaje").texto == ""


def test_completion_tokens_none_se_recupera_desde_el_total():
    """Gemini no reporta `completion_tokens`; el total sí trae el gasto real."""
    usage = types.SimpleNamespace(prompt_tokens=5, completion_tokens=None, total_tokens=43)
    datos = _crudo([_choice(None, finish_reason="max_tokens")], usage=usage)

    uso = proveedor_oci._respuesta_de(datos, modelo="m", etapa="triaje").uso

    assert (uso.input_tokens, uso.output_tokens) == (5, 38)


# ── La otra mitad: dict → objetos del SDK ────────────────────────────────────
#
# ⚠️ El submódulo, NO "oci" a secas: ver el docstring del módulo.


def _sdk_o_skip():
    pytest.importorskip(
        "oci.generative_ai_inference",
        reason="el SDK de OCI es un extra opcional: pip install '.[oci]'",
    )


def test_el_dict_se_convierte_en_objetos_del_sdk(pdf_falso):
    _sdk_o_skip()
    from oci.generative_ai_inference.models import ImageContent, TextContent

    pedido = Pedido(
        etapa="analisis",
        contenido=[
            BloqueTexto(texto="analizá esto"),
            BloqueDocumento(ruta="/tmp/e.pdf", paginas=2, escaneado=True),
        ],
        sistema="Sos AMALIA.",
        max_tokens=16_000,
    )
    detalles = proveedor_oci._a_objetos(proveedor_oci.traducir_pedido(_cfg(), pedido))

    assert detalles.compartment_id == COMPARTMENT
    assert detalles.serving_mode.model_id == "meta.llama-4-maverick-17b-128e-instruct-fp8"
    assert detalles.chat_request.max_tokens == 4096
    assert detalles.chat_request.api_format == "GENERIC"

    sistema, usuario = detalles.chat_request.messages
    assert sistema.role == "SYSTEM"
    assert isinstance(sistema.content[0], TextContent)

    assert usuario.role == "USER"
    assert isinstance(usuario.content[0], TextContent)
    assert [type(c) for c in usuario.content[1:]] == [ImageContent, ImageContent]
    assert usuario.content[1].image_url.url.startswith("data:image/jpeg;base64,")


def test_sin_sistema_el_sdk_recibe_un_solo_mensaje():
    _sdk_o_skip()
    pedido = Pedido(etapa="perfilado", contenido=[BloqueTexto(texto="x")], sistema="")
    detalles = proveedor_oci._a_objetos(proveedor_oci.traducir_pedido(_cfg(), pedido))
    assert len(detalles.chat_request.messages) == 1
    assert detalles.chat_request.messages[0].role == "USER"


def test_completar_de_punta_a_punta_con_un_cliente_inyectado():
    """El camino entero sin red: traducir → objetos del SDK → leer respuesta."""
    _sdk_o_skip()
    vistos = []

    class _Cliente:
        def chat(self, detalles):
            vistos.append(detalles)
            return _respuesta_cruda(
                ["{\"veredicto\": \"GO\"}"],
                finish_reason="stop",
                usage=types.SimpleNamespace(prompt_tokens=2491, completion_tokens=40),
            )

    pedido = Pedido(
        etapa="triaje",
        contenido=[BloqueImagen(datos_b64="QUJD")],
        sistema="Decidí GO/NO-GO.",
        max_tokens=16_000,
    )
    r = proveedor_oci.completar(_cfg(), pedido, cliente=_Cliente())

    assert r.texto == '{"veredicto": "GO"}'
    assert r.modelo == "meta.llama-4-scout-17b-16e-instruct"
    assert r.proveedor == "oci"
    assert r.truncada is False
    assert (r.uso.input_tokens, r.uso.output_tokens) == (2491, 40)
    assert vistos[0].chat_request.max_tokens == 4096


# ── Validación de configuración ──────────────────────────────────────────────


def _validar_o_skip(cfg):
    _sdk_o_skip()
    return proveedor_oci.validar_configuracion(cfg)


def test_validar_exige_el_compartment():
    with pytest.raises(LLMError) as exc:
        _validar_o_skip(_cfg(oci_genai_compartment_ocid="", oci_key_content=""))
    assert "OCI_GENAI_COMPARTMENT_OCID" in str(exc.value)


def test_validar_exige_las_tres_variables_que_acompañan_a_la_clave():
    cfg = _cfg(oci_key_content="-----BEGIN-----\\nabc\\n-----END-----",
               oci_tenancy_ocid="", oci_user_ocid="", oci_fingerprint="")
    with pytest.raises(LLMError) as exc:
        _validar_o_skip(cfg)
    for var in ("OCI_TENANCY_OCID", "OCI_USER_OCID", "OCI_FINGERPRINT"):
        assert var in str(exc.value)


def test_validar_pasa_con_la_credencial_de_la_vm_completa():
    cfg = _cfg(
        oci_key_content="-----BEGIN-----\\nabc\\n-----END-----",
        oci_tenancy_ocid="ocid1.tenancy.oc1..t",
        oci_user_ocid="ocid1.user.oc1..u",
        oci_fingerprint="aa:bb",
    )
    assert _validar_o_skip(cfg) is None


def test_validar_señala_que_no_hay_con_que_firmar(tmp_path):
    cfg = _cfg(oci_key_content="", oci_config_file=str(tmp_path / "no-existe"))
    with pytest.raises(LLMError) as exc:
        _validar_o_skip(cfg)
    assert "no hay con qué firmar" in str(exc.value)


# ── Gobernanza del dato: adónde salen los pliegos ────────────────────────────
#
# `OCI_FAMILIA=xai|google` no cambia solo de modelo: cambia de JURISDICCIÓN. OCI
# expone esos modelos, pero la inferencia sale por llamadas externas a servidores
# de xAI y de Google — con el texto completo del pliego y, si está escaneado, con
# sus páginas como imágenes. Fue la razón por la que estaban vetados.
#
# La decisión de encenderlos es del operador y no se bloquea. Lo que sí es
# obligatorio es que quede escrito en el log de la corrida, nombrando al tercero:
# "familia externa" no le dice nada a nadie; "servidores de xAI" sí.
#
# El test apunta a `llm._validar_familia` y no a `llm.validar_configuracion`
# porque la segunda importa el adaptador entero y arrastra el SDK de OCI: la
# gobernanza se puede afirmar sin él.

sin_familias = pytest.mark.skipif(
    not hasattr(modelos, "FAMILIAS_EXTERNAS"),
    reason="las familias de OCI llegan con la Tanda A de la Fase 9b",
)


@sin_familias
@pytest.mark.parametrize(
    ("familia", "tercero"),
    [("xai", "xAI"), ("google", "Google")],
)
def test_una_familia_externa_avisa_y_nombra_al_tercero(familia, tercero, caplog):
    cfg = _cfg(oci_familia=familia)

    with caplog.at_level("WARNING", logger="src.llm"):
        llm._validar_familia(cfg, "oci")

    assert "GOBERNANZA DE DATOS" in caplog.text
    assert tercero in caplog.text
    assert "pliegos" in caplog.text


@sin_familias
def test_la_familia_externa_avisa_pero_NO_bloquea(caplog):
    """Es una decisión informada del operador, no un error de configuración."""
    with caplog.at_level("WARNING", logger="src.llm"):
        assert llm._validar_familia(_cfg(oci_familia="xai"), "oci") is None


@sin_familias
def test_la_familia_nativa_no_avisa_nada(caplog):
    with caplog.at_level("WARNING", logger="src.llm"):
        llm._validar_familia(_cfg(oci_familia="nativo"), "oci")

    assert "GOBERNANZA DE DATOS" not in caplog.text


@sin_familias
def test_con_anthropic_la_familia_de_oci_no_se_mira(caplog):
    """Una VM que eligió Anthropic no manda nada a xAI, diga lo que diga su `.env`.

    Se valida **solo el proveedor activo**, igual que con las credenciales: un
    `OCI_FAMILIA` viejo de un experimento que ya no corre no tiene por qué
    ensuciar el log ni caerse al arrancar.
    """
    with caplog.at_level("WARNING", logger="src.llm"):
        llm._validar_familia(_cfg(oci_familia="xai"), "anthropic")

    assert "GOBERNANZA DE DATOS" not in caplog.text


@sin_familias
def test_una_familia_desconocida_falla_al_arrancar():
    """Igual que un `LLM_PROVIDER` desconocido, y por la misma razón.

    Si `OCI_FAMILIA=gogle` se resolviera en silencio a `nativo`, el operador
    creería estar midiendo Gemini durante semanas mirando números de Llama.
    """
    with pytest.raises(LLMError) as exc:
        llm._validar_familia(_cfg(oci_familia="gogle"), "oci")

    assert "gogle" in str(exc.value)
