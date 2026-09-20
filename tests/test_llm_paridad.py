"""La misma batería contra los DOS adaptadores. Lo que tienen que hacer igual.

`test_llm_oci.py` prueba lo que el adaptador de OCI hace **distinto**; esto
prueba lo que los dos hacen **igual**, que es de lo que depende que
`LLM_PROVIDER` sea de verdad una perilla sola.

Cada caso está escrito una vez y se ejecuta dos veces, parametrizado sobre un
*banco de pruebas* por proveedor. El banco sabe tres cosas que solo él puede
saber —cómo se arma un cliente falso con la forma de respuesta de ese SDK, cómo
se ve un error crudo suyo, y dónde quedó el prompt de sistema en el request— y
nada más. El test no menciona ninguna de las dos APIs.

**Por qué la aserción de `usage` se hace sobre `UsoModelo` y no sobre `Uso`.**
`UsoModelo.acumular()` (`src/scraper/models.py`) lee los contadores con
`getattr(usage, "input_tokens", 0) or 0`: si el objeto no expone esos dos
nombres exactos **no falla**, suma cero, y el gasto de la llamada desaparece del
libro mayor sin dejar rastro. OCI reporta `prompt_tokens`/`completion_tokens`.
Ese acoplamiento por duck typing es justo lo que está bajo test: afirmarlo sobre
`Uso` probaría el dataclass, no la integración que puede romperse.
"""
from __future__ import annotations

import types

import pytest

from src.llm import modelos
from src.llm.contrato import BloqueImagen, BloqueTexto, LLMError, Pedido
from src.scraper.models import UsoModelo

TEXTO = '{"veredicto": "GO"}'
ENTRADA_TOKENS = 100
SALIDA_TOKENS = 7


# ── Bancos de prueba: lo único que sabe de cada SDK ──────────────────────────


class _BancoAnthropic:
    nombre = "anthropic"

    @staticmethod
    def modulo():
        return pytest.importorskip(
            "src.llm.proveedor_anthropic",
            reason="el adaptador de Anthropic todavía no está en esta rama",
        )

    @staticmethod
    def cfg():
        return types.SimpleNamespace(
            llm_provider="anthropic",
            anthropic_api_key="sk-ant-de-mentira",
            llm_timeout_s=600,
        )

    @staticmethod
    def cliente(*, texto=TEXTO, truncada=False, usage="normal", excepcion=None):
        """Doble con la forma de respuesta de Anthropic: `content[].text`, `stop_reason`."""
        registro: list[dict] = []

        if usage == "normal":
            bloque_uso = types.SimpleNamespace(
                input_tokens=ENTRADA_TOKENS, output_tokens=SALIDA_TOKENS
            )
        else:
            bloque_uso = usage

        mensaje = types.SimpleNamespace(
            content=[types.SimpleNamespace(type="text", text=texto)],
            usage=bloque_uso,
            stop_reason="max_tokens" if truncada else "end_turn",
        )

        def _create(**kwargs):
            registro.append(kwargs)
            if excepcion is not None:
                raise excepcion
            return mensaje

        cliente = types.SimpleNamespace(
            messages=types.SimpleNamespace(create=_create, stream=_create)
        )
        return cliente, registro

    @staticmethod
    def error_crudo():
        """Un error nativo del SDK de Anthropic, no una excepción cualquiera."""
        anthropic = pytest.importorskip("anthropic")
        httpx = pytest.importorskip("httpx")
        return anthropic.APIConnectionError(
            message="no hay red",
            request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"),
        )

    @staticmethod
    def hubo_sistema(registro) -> bool:
        """Anthropic: el prompt de sistema es un kwarg, y ausente ≠ cadena vacía."""
        return "system" in registro[0]


class _BancoOci:
    nombre = "oci"

    @staticmethod
    def modulo():
        # ⚠️ El submódulo, NO "oci" a secas: el directorio `oci/` de la raíz
        # (scripts de despliegue, sin __init__.py) hace que `import oci` tenga
        # éxito sin el SDK y el skip nunca dispare. Verificado.
        pytest.importorskip(
            "oci.generative_ai_inference",
            reason="el SDK de OCI es un extra opcional: pip install '.[oci]'",
        )
        return pytest.importorskip("src.llm.proveedor_oci")

    @staticmethod
    def cfg():
        return types.SimpleNamespace(
            llm_provider="oci",
            oci_region="us-chicago-1",
            oci_genai_compartment_ocid="ocid1.compartment.oc1..aaaaaaaaejemplo",
            oci_max_tokens_salida=4096,
            oci_analisis_raster_pages=20,
            llm_timeout_s=600,
        )

    @staticmethod
    def cliente(*, texto=TEXTO, truncada=False, usage="normal", excepcion=None):
        """Doble con la forma de OCI: `data.chat_response.choices[].message.content[].text`."""
        registro: list = []

        if usage == "normal":
            bloque_uso = types.SimpleNamespace(
                prompt_tokens=ENTRADA_TOKENS, completion_tokens=SALIDA_TOKENS
            )
        else:
            bloque_uso = usage

        choice = types.SimpleNamespace(
            message=types.SimpleNamespace(content=[types.SimpleNamespace(text=texto)]),
            finish_reason="LENGTH" if truncada else "stop",
        )
        datos = types.SimpleNamespace(
            data=types.SimpleNamespace(
                chat_response=types.SimpleNamespace(choices=[choice], usage=bloque_uso)
            )
        )

        class _Cliente:
            def chat(self, detalles):
                registro.append(detalles)
                if excepcion is not None:
                    raise excepcion
                return datos

        return _Cliente(), registro

    @staticmethod
    def error_crudo():
        """Un 400 y no un 429: reintentable=False, así el test no espera backoff."""
        excepciones = pytest.importorskip("oci.exceptions")
        return excepciones.ServiceError(400, "InvalidParameter", {}, "maxTokens inválido")

    @staticmethod
    def hubo_sistema(registro) -> bool:
        """OCI: el prompt de sistema es un `SystemMessage` al frente de `messages`."""
        return any(m.role == "SYSTEM" for m in registro[0].chat_request.messages)


BANCOS = [_BancoAnthropic, _BancoOci]


@pytest.fixture(params=BANCOS, ids=[b.nombre for b in BANCOS])
def banco(request):
    """Devuelve el banco ya validado: si su adaptador o su SDK falta, saltea."""
    b = request.param
    b.modulo()  # importorskip: el skip ocurre acá, no a mitad del test
    return b


def _pedido(**kw):
    base = dict(
        etapa="triaje",
        contenido=[BloqueTexto(texto="Objeto de la contratación: firewalls")],
        sistema="Decidí GO/NO-GO.",
        max_tokens=512,
    )
    base.update(kw)
    return Pedido(**base)


def _completar(banco, pedido, **kw):
    cliente, registro = banco.cliente(**kw)
    respuesta = banco.modulo().completar(banco.cfg(), pedido, cliente=cliente)
    return respuesta, registro


# ── 1. La normalización del `usage`, afirmada sobre UsoModelo ────────────────


def test_el_usage_llega_al_libro_mayor_como_input_output_tokens(banco):
    """El test que impide que el freno de gasto quede en cero en silencio.

    `prompt_tokens=100, completion_tokens=7` de OCI tiene que terminar en
    `UsoModelo(input_tokens=100, output_tokens=7, llamadas=1)`, exactamente
    igual que los `input_tokens/output_tokens` de Anthropic. `acumular()` no
    lanza si los nombres no coinciden: suma cero. Sin esta aserción, un cambio
    de nombre río arriba se vería como "gasto: $0.00" y nadie lo investigaría.
    """
    respuesta, _ = _completar(banco, _pedido())

    uso = UsoModelo(modelo=respuesta.modelo)
    uso.acumular(respuesta.uso)

    assert uso.input_tokens == ENTRADA_TOKENS
    assert uso.output_tokens == SALIDA_TOKENS
    assert uso.llamadas == 1


def test_sin_usage_el_libro_mayor_no_registra_la_llamada(banco):
    """`uso=None` significa "no reportó consumo", distinto de "consumió cero"."""
    respuesta, _ = _completar(banco, _pedido(), usage=None)

    uso = UsoModelo(modelo=respuesta.modelo)
    uso.acumular(respuesta.uso)

    assert uso.llamadas == 0


def test_un_usage_con_los_nombres_equivocados_no_inventa_tokens(banco):
    """La degradación es a cero, no a basura: el aviso lo da el log de `Uso`."""
    raro = types.SimpleNamespace(tokens_totales=999)
    respuesta, _ = _completar(banco, _pedido(), usage=raro)

    uso = UsoModelo(modelo=respuesta.modelo)
    uso.acumular(respuesta.uso)

    assert (uso.input_tokens, uso.output_tokens) == (0, 0)


# ── 2. Extracción del texto, en dos formas de respuesta distintas ────────────


def test_el_texto_sale_igual_de_las_dos_formas_de_respuesta(banco):
    respuesta, _ = _completar(banco, _pedido())
    assert respuesta.texto == TEXTO
    assert respuesta.proveedor == banco.nombre


def test_una_respuesta_vacia_da_texto_vacio_y_no_explota(banco):
    """Los tres call sites que parsean JSON ya tratan el vacío como parse_error.

    Lanzar acá los obligaría a aprender un segundo modo de falla para el mismo
    síntoma.
    """
    respuesta, _ = _completar(banco, _pedido(), texto="")
    assert respuesta.texto == ""


# ── 3. `truncada`, normalizada desde dos vocabularios ────────────────────────


def test_el_corte_por_tope_de_salida_se_reporta_como_truncada(banco):
    """Anthropic dice `stop_reason="max_tokens"`; OCI, `finish_reason="LENGTH"`.

    Se normaliza en los dos porque hoy solo `engine.py` mira el motivo de corte:
    en `triage` y `analysis` un truncamiento se reporta como `parse_error`, como
    si el modelo hubiera dicho algo inválido. Con el techo de 4096 de OCI eso
    pasa de rareza a modo de falla habitual.
    """
    respuesta, _ = _completar(banco, _pedido(), truncada=True)
    assert respuesta.truncada is True


def test_un_corte_normal_no_se_reporta_como_truncada(banco):
    respuesta, _ = _completar(banco, _pedido(), truncada=False)
    assert respuesta.truncada is False


# ── 4. `sistema=""` no emite bloque de sistema en ninguno ────────────────────


def test_sistema_vacio_no_emite_bloque_de_sistema(banco):
    """El caso de `classifier/trainer.py`, que llama sin `system`.

    Una cadena vacía no es lo mismo que la ausencia del campo: mandarla
    cambiaría el prefijo del prompt de una etapa que hoy funciona, y el cambio
    sería invisible hasta que baje la calidad.
    """
    _, registro = _completar(banco, _pedido(etapa="perfilado", sistema=""))
    assert banco.hubo_sistema(registro) is False


def test_sistema_con_texto_si_emite_bloque_de_sistema(banco):
    _, registro = _completar(banco, _pedido(sistema="Sos AMALIA."))
    assert banco.hubo_sistema(registro) is True


# ── 5. Todo error crudo sale como LLMError ───────────────────────────────────


def test_un_error_nativo_del_sdk_sale_siempre_como_LLMError(banco):
    """El riesgo #1 de la migración, convertido en aserción.

    Si un error de OCI se escapa sin traducir, el `except anthropic.APIError` de
    `engine.py` deja de atraparlo y la semántica de fallo del sistema cambia en
    silencio: un lote que hoy se degrada pasaría a tumbar la corrida.
    """
    with pytest.raises(LLMError) as exc:
        _completar(banco, _pedido(), excepcion=banco.error_crudo())

    assert exc.value.proveedor == banco.nombre
    assert exc.value.etapa == "triaje"


def test_el_LLMError_dice_de_que_etapa_viene(banco):
    """Sin la etapa, un error en journalctl no dice qué parte del pipeline cayó."""
    for etapa in ("analisis", "clasificacion"):
        with pytest.raises(LLMError) as exc:
            _completar(
                banco,
                _pedido(etapa=etapa, max_tokens=512),
                excepcion=banco.error_crudo(),
            )
        assert exc.value.etapa == etapa


# ── 6. El modelo de la Respuesta: resuelto por tabla y con precio conocido ───


def test_el_modelo_de_la_respuesta_es_el_que_resolvio_la_tabla(banco):
    """Nadie hardcodea un ID de modelo del lado del adaptador."""
    for etapa in modelos.ETAPAS:
        cfg = banco.cfg()
        respuesta, _ = _completar(banco, _pedido(etapa=etapa, max_tokens=512))
        assert respuesta.modelo == modelos.modelo_de(cfg, etapa)
        assert respuesta.modelo == modelos.DEFAULTS[(banco.nombre, etapa)]


def test_el_modelo_de_la_respuesta_tiene_precio_en_el_libro_mayor(banco):
    """Un modelo sin tarifa se cobra al precio del más caro y sesga la cuota.

    `quota.precio()` nunca falla —devuelve `_PRECIO_DESCONOCIDO` y loguea—, así
    que la única forma de notar que falta una tarifa es preguntarlo acá.
    """
    from src.drilldown import quota

    for etapa in modelos.ETAPAS:
        modelo = modelos.DEFAULTS[(banco.nombre, etapa)]
        if banco.nombre == "oci" and modelo not in quota.PRECIOS_USD_MTOK:
            # La sección OCI de PRECIOS_USD_MTOK llega con `quota.py` (Tanda 3,
            # agente G). El skip se borra solo el día que esa tabla exista: no
            # hay nada que recordar hacer.
            pytest.skip("la sección OCI de quota.PRECIOS_USD_MTOK todavía no existe")
        assert modelo in quota.PRECIOS_USD_MTOK, f"{modelo} no tiene tarifa"


# ── 7. Los dos adaptadores aceptan el mismo contenido ────────────────────────


def test_una_imagen_no_rompe_a_ninguno_de_los_dos(banco):
    """El 66% del corpus está escaneado y llega al triaje como JPEG base64."""
    respuesta, _ = _completar(
        banco,
        _pedido(contenido=[BloqueImagen(datos_b64="QUJD"), BloqueTexto(texto="¿qué dice?")]),
    )
    assert respuesta.texto == TEXTO


# ── 8. Contabilidad honesta: el razonamiento que OCI no reporta ──────────────
#
# Esto es lo único de este archivo que NO se parametriza sobre los dos bancos, y
# la asimetría es el punto: Anthropic reporta `output_tokens` y con eso alcanza;
# OCI, en las familias externas, reporta un `completion_tokens` que deja afuera
# los tokens de razonamiento. Medido el 2026-09-19 en `us-chicago-1` con el
# mismo prompt y max_tokens=20:
#
#     meta.llama-4-scout       prompt=39  completion=20    total=59   (cierra)
#     xai.grok-4.6             prompt=39  completion=18    total=514  (~450 ocultos)
#     google.gemini-2.5-flash  prompt=5   completion=None  total=43
#
# Grok facturó ~450 tokens y reportó 18: una subestimación de ~25×.
#
# La aserción va sobre `UsoModelo` y no sobre `Uso` por la misma razón que el
# bloque 1: lo que está bajo test es el acoplamiento por duck typing que mueve
# el gasto al libro mayor, no el dataclass.


@pytest.fixture
def oci():
    """El banco de OCI a solas, ya validado (saltea si falta el SDK)."""
    _BancoOci.modulo()
    return _BancoOci


def _uso_acumulado(banco, usage):
    respuesta, _ = _completar(banco, _pedido(), usage=usage)
    libro = UsoModelo(modelo=respuesta.modelo)
    libro.acumular(respuesta.uso)
    return libro


def test_el_razonamiento_oculto_de_grok_llega_al_libro_mayor(oci):
    """El caso Grok exacto: 18 reportados, 475 contabilizados.

    Sube el gasto medido, y ése es el objetivo: `DAILY_BUDGET_USD` existe para
    frenar antes de la factura, y un freno que mira un número 25× menor que el
    real no frena nada.
    """
    usage = types.SimpleNamespace(prompt_tokens=39, completion_tokens=18, total_tokens=514)

    libro = _uso_acumulado(oci, usage)

    assert (libro.input_tokens, libro.output_tokens, libro.llamadas) == (39, 475, 1)


def test_completion_tokens_none_se_contabiliza_desde_el_total(oci):
    """El caso Gemini: sin `completion_tokens`, el total es la única señal."""
    usage = types.SimpleNamespace(prompt_tokens=5, completion_tokens=None, total_tokens=43)

    libro = _uso_acumulado(oci, usage)

    assert (libro.input_tokens, libro.output_tokens) == (5, 38)


def test_cuando_el_total_cierra_exacto_no_cambia_nada(oci):
    """El caso Llama: 39+20=59. Las dos vías dan lo mismo, así que no se toca.

    Es la garantía de que la contabilidad honesta no altera el gasto medido de
    la familia nativa, que es la que corre hoy en los cuatro tenants.
    """
    usage = types.SimpleNamespace(prompt_tokens=39, completion_tokens=20, total_tokens=59)

    libro = _uso_acumulado(oci, usage)

    assert (libro.input_tokens, libro.output_tokens) == (39, 20)


def test_sin_total_tokens_manda_lo_reportado(oci):
    """La forma de `usage` de la Tanda 0 —sin `total_tokens`— sigue valiendo."""
    usage = types.SimpleNamespace(prompt_tokens=100, completion_tokens=7)

    libro = _uso_acumulado(oci, usage)

    assert (libro.input_tokens, libro.output_tokens) == (100, 7)


@pytest.mark.parametrize(
    "total",
    [
        pytest.param(10, id="menor-que-el-prompt"),
        pytest.param(0, id="cero"),
        pytest.param(-5, id="negativo"),
        pytest.param("mucho", id="no-numerico"),
        pytest.param(None, id="none"),
    ],
)
def test_un_total_tokens_basura_nunca_produce_negativos(oci, total):
    """La resta solo se adopta si RECUPERA tokens; si no, se ignora.

    Un `output_tokens` negativo restaría gasto del libro mayor: la guardia de
    presupuesto se soltaría justo por el campo que existe para apretarla.
    """
    usage = types.SimpleNamespace(prompt_tokens=39, completion_tokens=18, total_tokens=total)

    libro = _uso_acumulado(oci, usage)

    assert (libro.input_tokens, libro.output_tokens) == (39, 18)
