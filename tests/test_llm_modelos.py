"""Tests de la resolución de modelo y del contrato neutral (`src/llm/`).

Lo que cubre, en orden de gravedad de la falla que previene:

1. **Que `Uso` siga siendo compatible con `UsoModelo.acumular()`.** Es un
   acoplamiento por duck typing con `getattr(..., 0)`: si se rompe, nada lanza,
   el gasto se contabiliza como CERO y el presupuesto diario deja de frenar nada
   mientras la factura corre. Es el defecto más caro y el más silencioso.
2. **Que un `LLM_PROVIDER` —o una `OCI_FAMILIA`— desconocidos fallen.** Un typo
   que degradara en silencio al default gastaría en el proveedor equivocado, o
   dejaría al operador creyendo que encendió Gemini, durante semanas.
3. **Que `max_tokens` se recorte al techo DEL MODELO.** La API rechaza con
   `400 Invalid 'maxTokens'` — sin clamp, `analisis` (16.000) y `clasificacion`
   (16.384) fallan en la primera llamada contra Meta. Y que ese techo sea por
   modelo y no por proveedor es la razón de ser de la Fase 9b: con Gemini los
   16.000 del análisis pasan enteros y con Meta se cortan a 4.096.
4. Que `cfg` pise la tabla y que la tabla sea el piso cuando `cfg` viene vacío,
   incluida la mezcla de familias por etapa.

La `cfg` es un `types.SimpleNamespace`, que es el idioma de `test_alerts.py`:
las funciones leen todo con `getattr(cfg, campo, ...)` justamente para que un
test no tenga que construir un `Config` entero ni tocar el entorno.
"""
import types

import pytest

from src import llm
from src.llm import modelos
from src.llm.contrato import LLMError, Uso
from src.scraper.models import UsoModelo


def _cfg(proveedor="anthropic", **campos):
    """cfg mínima: el proveedor y, opcionalmente, overrides de modelo."""
    return types.SimpleNamespace(llm_provider=proveedor, **campos)


# ── 1. El contrato de `Uso` con el contador de gasto ─────────────────────────


class TestUsoAlimentaElLibroMayor:
    def test_uso_de_anthropic_llega_entero_a_UsoModelo(self):
        crudo = types.SimpleNamespace(input_tokens=100, output_tokens=7)
        acumulador = UsoModelo(modelo="claude-haiku-4-5")
        acumulador.acumular(Uso.desde_anthropic(crudo))
        medido = (acumulador.input_tokens, acumulador.output_tokens, acumulador.llamadas)
        assert medido == (100, 7, 1)

    def test_uso_de_oci_se_traduce_y_NO_queda_en_cero(self):
        """OCI reporta prompt_tokens/completion_tokens.

        REGRESIÓN: sin la traducción, `acumular` suma 0 sin lanzar nada y el
        gasto desaparece del libro mayor. Este test es el freno de gasto.
        """
        crudo = types.SimpleNamespace(prompt_tokens=2491, completion_tokens=180)
        acumulador = UsoModelo(modelo="meta.llama-4-scout-17b-16e-instruct")
        acumulador.acumular(Uso.desde_oci(crudo))
        medido = (acumulador.input_tokens, acumulador.output_tokens, acumulador.llamadas)
        assert medido == (2491, 180, 1)

    @pytest.mark.parametrize("constructor", [Uso.desde_anthropic, Uso.desde_oci])
    def test_usage_None_devuelve_None(self, constructor):
        """None significa "no reportó consumo", distinto de "consumió cero"."""
        assert constructor(None) is None

    def test_usage_sin_los_campos_esperados_deja_constancia_en_el_log(self, caplog):
        """La falla silenciosa preexistente pasa a ser una línea greppable."""
        with caplog.at_level("WARNING"):
            uso = Uso.desde_oci(types.SimpleNamespace(tokens_totales=999))
        assert uso == Uso(0, 0)
        assert "NO se está contabilizando" in caplog.text


# ── 2. Resolución (proveedor, etapa) → modelo ────────────────────────────────


class TestResolucionDeModelo:
    @pytest.mark.parametrize("etapa", modelos.ETAPAS)
    @pytest.mark.parametrize("proveedor", modelos.PROVEEDORES)
    def test_cada_par_resuelve_a_su_default(self, proveedor, etapa):
        assert modelos.modelo_de(_cfg(proveedor), etapa) == modelos.DEFAULTS[(proveedor, etapa)]

    def test_la_tabla_cubre_todos_los_pares(self):
        esperados = {(p, e) for p in modelos.PROVEEDORES for e in modelos.ETAPAS}
        assert set(modelos.DEFAULTS) == esperados
        assert set(modelos._CAMPO_CFG) == esperados

    def test_los_defaults_de_anthropic_son_los_modelos_en_uso(self):
        assert modelos.DEFAULTS[("anthropic", "triaje")] == "claude-haiku-4-5"
        assert modelos.DEFAULTS[("anthropic", "analisis")] == "claude-sonnet-5"
        # engine.py y trainer.py hardcodean este ID; la tabla lo hereda tal cual.
        assert modelos.DEFAULTS[("anthropic", "clasificacion")] == "claude-haiku-4-5-20251001"
        assert modelos.DEFAULTS[("anthropic", "perfilado")] == "claude-haiku-4-5-20251001"

    def test_los_defaults_de_oci_son_solo_Scout_y_Maverick(self):
        """REGRESIÓN de la Tanda 0: `meta.llama-3.3-70b-instruct` NO EXISTE en
        este tenant — `list-models` solo devuelve Scout y Maverick. La doc lo
        lista igual, así que es fácil reintroducirlo por copiar de ahí."""
        de_oci = {v for (p, _), v in modelos.DEFAULTS.items() if p == "oci"}
        assert de_oci == {
            "meta.llama-4-scout-17b-16e-instruct",
            "meta.llama-4-maverick-17b-128e-instruct-fp8",
        }
        assert not any("llama-3" in m for m in de_oci)

    def test_ningun_default_de_oci_sale_a_un_tercero(self):
        """El default NO manda pliegos afuera.

        Desde la Fase 9b xAI y Google son configurables, pero hay que pedirlos:
        `OCI_FAMILIA` sin tocar resuelve a `nativo`, y `nativo` es todo Meta.
        Cohere sigue descartado por incompatibilidad, no por preferencia.
        """
        de_oci = [v for (p, _), v in modelos.DEFAULTS.items() if p == "oci"]
        assert all(m.startswith("meta.") for m in de_oci)

    def test_las_entradas_de_oci_espejan_la_familia_por_defecto(self):
        """`DEFAULTS[("oci", etapa)]` es un espejo GENERADO de `DEFAULTS_OCI`.

        Lo leen los tests de paridad y los de triaje rasterizado, y de ahí sale
        la guardia de tarifas de `test_quota.py`. Transcribirlo a mano sería
        invitar a que las dos tablas se desincronicen.
        """
        for etapa in modelos.ETAPAS:
            assert (
                modelos.DEFAULTS[("oci", etapa)]
                == modelos.DEFAULTS_OCI[(modelos.FAMILIA_DEFAULT, etapa)]
            )


# ── 2b. Familias dentro de OCI ───────────────────────────────────────────────


class TestFamiliaActiva:
    def test_sin_configurar_es_nativo(self):
        assert modelos.familia_activa(types.SimpleNamespace()) == "nativo"
        assert modelos.FAMILIA_DEFAULT == "nativo"

    def test_con_valor_lo_devuelve(self):
        assert modelos.familia_activa(_cfg("oci", oci_familia="google")) == "google"

    @pytest.mark.parametrize("crudo", [" XAI ", "Xai", "xai\n"])
    def test_se_normaliza(self, crudo):
        """Un "XAI " con espacio en el .env de una VM no puede degradar a nativo."""
        assert modelos.familia_activa(_cfg("oci", oci_familia=crudo)) == "xai"

    @pytest.mark.parametrize("vacio", ["", "   ", None])
    def test_vacia_cae_al_default(self, vacio):
        assert modelos.familia_activa(_cfg("oci", oci_familia=vacio)) == "nativo"

    def test_una_familia_desconocida_NO_se_resuelve_a_nativo(self):
        """Igual que `proveedor_activo`: normaliza, no valida. Quien falla es
        `modelo_de` / `validar_configuracion`."""
        assert modelos.familia_activa(_cfg("oci", oci_familia="googel")) == "googel"


class TestDespachoPorFamilia:
    @pytest.mark.parametrize("etapa", modelos.ETAPAS)
    @pytest.mark.parametrize("familia", modelos.FAMILIAS)
    def test_cada_par_familia_etapa_resuelve_a_su_default(self, familia, etapa):
        cfg = _cfg("oci", oci_familia=familia)
        assert modelos.modelo_de(cfg, etapa) == modelos.DEFAULTS_OCI[(familia, etapa)]

    def test_la_tabla_cubre_todas_las_familias_y_etapas(self):
        esperados = {(f, e) for f in modelos.FAMILIAS for e in modelos.ETAPAS}
        assert set(modelos.DEFAULTS_OCI) == esperados

    def test_xai_y_google_usan_un_solo_modelo_en_las_cuatro_etapas(self):
        """No se parten como Meta: los dos son multimodales y sin tope
        restrictivo, así que no hay un "barato para volumen" que separar."""
        for familia, esperado in (("xai", "xai.grok-4.6"), ("google", "google.gemini-2.5-flash")):
            resueltos = {modelos.DEFAULTS_OCI[(familia, e)] for e in modelos.ETAPAS}
            assert resueltos == {esperado}

    def test_las_familias_externas_son_exactamente_xai_y_google(self):
        """`src/llm/__init__` usa este conjunto para el WARNING de gobernanza:
        si una familia externa se cayera de acá, los pliegos saldrían a un
        tercero sin que nadie lo diga en el log."""
        assert modelos.FAMILIAS_EXTERNAS == {"xai", "google"}
        assert modelos.FAMILIA_DEFAULT not in modelos.FAMILIAS_EXTERNAS
        assert modelos.FAMILIAS_EXTERNAS < set(modelos.FAMILIAS)

    def test_la_familia_no_afecta_a_anthropic(self):
        """Las dos perillas son ortogonales: OCI_FAMILIA no hace nada mientras
        LLM_PROVIDER=anthropic, ni siquiera si trae basura."""
        cfg = _cfg("anthropic", oci_familia="googel")
        assert modelos.modelo_de(cfg, "triaje") == modelos.DEFAULTS[("anthropic", "triaje")]

    def test_una_familia_desconocida_FALLA(self):
        """Mismo criterio que un proveedor desconocido. Degradar a `nativo` en
        silencio dejaría al operador creyendo que encendió otra cosa."""
        with pytest.raises(LLMError) as exc:
            modelos.modelo_de(_cfg("oci", oci_familia="googel"), "triaje")
        assert "googel" in str(exc.value)
        assert "OCI_FAMILIA" in str(exc.value)

    def test_una_familia_desconocida_falla_aunque_cfg_pise_el_modelo(self):
        """Un typo en OCI_FAMILIA es un error de configuración se use o no.
        Descubrirlo recién el día que alguien borre el OCI_MODEL_* sería la peor
        versión del mismo problema."""
        cfg = _cfg("oci", oci_familia="googel", oci_model_triage="meta.lo-que-sea")
        with pytest.raises(LLMError):
            modelos.modelo_de(cfg, "triaje")


class TestMezclaDeFamilias:
    def test_el_campo_por_etapa_le_gana_a_la_familia(self):
        """El caso de uso explícito: triaje barato en Llama y análisis en
        Gemini, que es la única familia cuyo techo deja pasar los 16.000."""
        cfg = _cfg(
            "oci",
            oci_familia="nativo",
            oci_model_analysis="google.gemini-2.5-flash",
        )
        assert modelos.modelo_de(cfg, "triaje") == "meta.llama-4-scout-17b-16e-instruct"
        assert modelos.modelo_de(cfg, "analisis") == "google.gemini-2.5-flash"
        assert llm.max_tokens_de(cfg, "triaje", 16_000) == 4096
        assert llm.max_tokens_de(cfg, "analisis", 16_000) == 16_000

    def test_la_familia_gobierna_las_etapas_que_cfg_no_pisa(self):
        cfg = _cfg("oci", oci_familia="google", oci_model_triage="meta.llama-4-scout-17b-16e-instruct")
        assert modelos.modelo_de(cfg, "triaje") == "meta.llama-4-scout-17b-16e-instruct"
        for etapa in ("analisis", "clasificacion", "perfilado"):
            assert modelos.modelo_de(cfg, etapa) == "google.gemini-2.5-flash"


class TestCfgPisaLaTabla:
    def test_cfg_gana_sobre_el_default(self):
        cfg = _cfg("anthropic", model_triage="claude-opus-5")
        assert modelos.modelo_de(cfg, "triaje") == "claude-opus-5"

    def test_cfg_de_oci_usa_su_propio_campo(self):
        cfg = _cfg("oci", oci_model_analysis="meta.otro-modelo")
        assert modelos.modelo_de(cfg, "analisis") == "meta.otro-modelo"

    def test_el_campo_de_anthropic_NO_gobierna_a_oci(self):
        """El motivo de tener variables separadas: si `MODEL_TRIAGE` gobernara
        los dos, poner LLM_PROVIDER=oci en una VM desplegada mandaría
        "claude-haiku-4-5" como model_id a OCI y daría un 404 confuso."""
        cfg = _cfg("oci", model_triage="claude-haiku-4-5")
        assert modelos.modelo_de(cfg, "triaje") == modelos.DEFAULTS[("oci", "triaje")]

    @pytest.mark.parametrize("vacio", ["", "   ", None])
    def test_un_campo_vacio_cae_al_default(self, vacio):
        """Es el caso NORMAL: el .env de producción no define los cuatro modelos."""
        cfg = _cfg("anthropic", model_classifier=vacio)
        assert modelos.modelo_de(cfg, "clasificacion") == modelos.DEFAULTS[
            ("anthropic", "clasificacion")
        ]

    def test_cfg_sin_el_atributo_no_revienta(self):
        """`getattr` con default, nunca acceso directo: es lo que permite el
        SimpleNamespace de estos tests y el idioma de memoria/service.py."""
        assert modelos.modelo_de(types.SimpleNamespace(), "triaje") == modelos.DEFAULTS[
            ("anthropic", "triaje")
        ]


# ── 3. Proveedor desconocido: falla, no degrada ──────────────────────────────


class TestProveedorActivo:
    def test_sin_configurar_es_anthropic(self):
        assert llm.proveedor_activo(types.SimpleNamespace()) == "anthropic"

    @pytest.mark.parametrize("crudo", ["OCI", " oci ", "Oci"])
    def test_se_normaliza(self, crudo):
        assert llm.proveedor_activo(_cfg(crudo)) == "oci"

    def test_vacio_cae_al_default(self):
        assert llm.proveedor_activo(_cfg("")) == "anthropic"

    def test_un_proveedor_desconocido_FALLA_en_la_validacion(self):
        """Un typo que degradara en silencio a Anthropic gastaría en el
        proveedor equivocado durante semanas sin ningún síntoma."""
        with pytest.raises(LLMError) as exc:
            llm.validar_configuracion(_cfg("ocl"))
        assert "ocl" in str(exc.value)

    def test_un_proveedor_desconocido_NO_se_resuelve_a_anthropic(self):
        assert llm.proveedor_activo(_cfg("ocl")) == "ocl"

    def test_resolver_un_modelo_con_proveedor_desconocido_falla(self):
        with pytest.raises(LLMError):
            modelos.modelo_de(_cfg("gemini"), "triaje")

    def test_una_etapa_desconocida_falla(self):
        with pytest.raises(LLMError) as exc:
            modelos.modelo_de(_cfg("anthropic"), "resumen")
        assert "resumen" in str(exc.value)


class TestTablaDeDespacho:
    def test_los_proveedores_de_la_fachada_coinciden_con_los_de_la_tabla(self):
        assert set(llm._PROVEEDORES) == set(modelos.PROVEEDORES)

    def test_la_tabla_guarda_NOMBRES_y_no_callables(self):
        """El import tiene que ser perezoso: `proveedor_oci` arrastra el SDK de
        OCI, que es un extra opcional, y a nivel de módulo rompería el arranque
        de cualquier imagen construida sin ese extra."""
        for modulo, funcion in llm._PROVEEDORES.values():
            assert isinstance(modulo, str) and isinstance(funcion, str)
            assert modulo.startswith("src.llm.proveedor_")

    def test_un_adaptador_ausente_sale_como_LLMError_accionable(self, monkeypatch):
        """Estructural: si el módulo del proveedor no se puede importar, el
        operador tiene que leer en journalctl qué instalar, no un ImportError.

        Se simula con un nombre de módulo inexistente en vez de apoyarse en que
        `proveedor_oci` todavía no esté escrito: esa versión se volvía verde por
        la razón equivocada y roja en cuanto el adaptador aterrizó (Tanda 2E).
        """
        monkeypatch.setitem(
            llm._PROVEEDORES, "oci", ("src.llm.proveedor_inexistente", "completar")
        )
        with pytest.raises(LLMError) as exc:
            llm._adaptador("oci", "triaje")
        assert "oci" in str(exc.value)


# ── 4. Clamp de max_tokens ───────────────────────────────────────────────────


class TestMaxTokens:
    def test_anthropic_no_recorta(self):
        """Sus topes están muy por encima de los 16.384 del clasificador."""
        assert llm.max_tokens_de(_cfg("anthropic"), "clasificacion", 16384) == 16384

    def test_oci_con_meta_recorta_a_4096(self):
        """MEDIDO: por encima, la API responde
        `400 Invalid 'maxTokens': Value is greater than maximum: 4096`.
        El clamp no es una optimización, es lo que evita el error duro."""
        assert llm.max_tokens_de(_cfg("oci"), "analisis", 16000) == 4096
        assert llm.max_tokens_de(_cfg("oci"), "clasificacion", 16384) == 4096

    def test_oci_no_toca_lo_que_ya_entra(self):
        assert llm.max_tokens_de(_cfg("oci"), "triaje", 512) == 512

    def test_oci_deja_bajar_el_techo_por_config(self):
        cfg = _cfg("oci", oci_max_tokens_salida=1024)
        assert llm.max_tokens_de(cfg, "analisis", 16000) == 1024

    def test_oci_NO_deja_subir_el_techo_por_config(self):
        """Permitirlo solo serviría para romper la corrida de las 07:00 con un 400."""
        cfg = _cfg("oci", oci_max_tokens_salida=16000)
        assert llm.max_tokens_de(cfg, "analisis", 16000) == 4096

    def test_la_perilla_baja_el_techo_alto_de_una_familia_externa(self):
        """Sigue siendo un tope manual, no un ancla al 4096 de Meta."""
        cfg = _cfg("oci", oci_familia="google", oci_max_tokens_salida=8000)
        assert llm.max_tokens_de(cfg, "analisis", 16000) == 8000
        assert llm.max_tokens_de(_cfg("oci", oci_familia="google", oci_max_tokens_salida=2000),
                                 "analisis", 16000) == 2000

    @pytest.mark.parametrize("sin_override", [0, None, "", "0"])
    def test_sin_override_manda_el_techo_del_modelo(self, sin_override):
        """REGRESIÓN del modo de falla más silencioso de la Fase 9b.

        `cfg.oci_max_tokens_salida` valía 4096 por default —el techo de Meta, de
        cuando era el techo del proveedor entero—. Aplicado como override,
        `min(65536, 4096)` deja a Gemini en 4.096 y **la funcionalidad entera
        muere al nacer**, sin ningún síntoma: la familia se ve como una que "no
        mejora nada". 0/vacío significa "sin override: usá el techo del modelo".
        """
        cfg = _cfg("oci", oci_familia="google", oci_max_tokens_salida=sin_override)
        assert llm.max_tokens_de(cfg, "analisis", 16000) == 16000

    def test_el_default_de_config_no_ancla_el_techo(self, monkeypatch):
        """Lo mismo, pero contra la `Config` real y no contra un namespace.

        Si alguien devuelve el default a 4096, este test es el que lo atrapa.
        Se borra la variable del entorno a propósito: lo que se fija es el
        DEFAULT del campo, no lo que tenga el `.env` de esta máquina.
        """
        from src.config import Config

        monkeypatch.delenv("OCI_MAX_TOKENS_SALIDA", raising=False)
        factoria = Config.__dataclass_fields__["oci_max_tokens_salida"].default_factory
        assert factoria() == 0


class TestTechoPorModelo:
    """La razón de ser de la Fase 9b: el techo es del MODELO, no del proveedor.

    Hasta acá `OCI_MAX_TOKENS_SALIDA` era una constante del proveedor y le
    recortaba a Gemini sus 65.536 a 4.096 sin ninguna razón.
    """

    def test_el_mismo_deseado_se_recorta_con_meta_y_pasa_entero_con_gemini(self):
        """16.000 es lo que pide `analysis.py`. Con `nativo` se cae a 4.096;
        con `google` es la primera vez que la matriz de riesgos no está
        limitada por el techo del proveedor."""
        assert llm.max_tokens_de(_cfg("oci", oci_familia="nativo"), "analisis", 16000) == 4096
        assert llm.max_tokens_de(_cfg("oci", oci_familia="google"), "analisis", 16000) == 16000

    def test_el_recorte_de_gemini_llega_recien_en_su_propio_techo(self):
        cfg = _cfg("oci", oci_familia="google")
        assert llm.max_tokens_de(cfg, "analisis", 100_000) == 65536

    def test_grok_acepta_muchisimo_mas(self):
        cfg = _cfg("oci", oci_familia="xai")
        assert llm.max_tokens_de(cfg, "analisis", 100_000) == 100_000
        assert llm.max_tokens_de(cfg, "analisis", 1_000_000) == 200_000

    def test_los_techos_son_los_MEDIDOS_por_biseccion(self):
        """Valores del 2026-09-19 contra la API real, no de documentación."""
        assert modelos.TECHO_SALIDA == {
            "meta.llama-4-scout-17b-16e-instruct": 4096,
            "meta.llama-4-maverick-17b-128e-instruct-fp8": 4096,
            "google.gemini-2.5-flash": 65536,
            "xai.grok-4.6": 200000,
        }

    def test_un_modelo_sin_techo_medido_se_recorta_al_mas_restrictivo(self):
        """Un ID nuevo tiene que DEGRADAR, no reventar con un 400 a las 07:00."""
        assert modelos.TECHO_DESCONOCIDO == 4096
        assert modelos.TECHO_DESCONOCIDO == min(modelos.TECHO_SALIDA.values())
        cfg = _cfg("oci", oci_model_analysis="meta.modelo-que-todavia-no-existe")
        assert llm.max_tokens_de(cfg, "analisis", 16000) == modelos.TECHO_DESCONOCIDO

    def test_todo_default_de_oci_tiene_techo_medido(self):
        """Agregar una familia sin medir su techo la dejaría en 4.096 sin que
        nadie se entere: se vería como una familia que "no mejora nada"."""
        for (familia, etapa), modelo in modelos.DEFAULTS_OCI.items():
            assert modelo in modelos.TECHO_SALIDA, (
                f"el default de ({familia}, {etapa}) es {modelo} y no tiene techo medido"
            )


class TestTarifasDeLasFamilias:
    """La guardia de precios, ajustada a las familias.

    `quota.precio()` nunca falla: lo que no está en `PRECIOS_USD_MTOK` se cobra
    como `_PRECIO_DESCONOCIDO`, el más caro que conocemos. Por eso la guardia
    general de `tests/test_quota.py` exige tarifa para todo valor de
    `modelos.DEFAULTS` — que espeja solo la familia por defecto.

    **`xai.grok-4.6` y `google.gemini-2.5-flash` quedan fuera de la tabla de
    precios A PROPÓSITO**: no hay tarifa medida (la página de precios de Oracle
    devuelve 403 y la Usage API está denegada), y una tarifa inventada es peor
    que ninguna porque se ve precisa y no lo es. Cobrarlas como el modelo más
    caro hace que la guardia APRIETE en vez de soltarse, que es exactamente lo
    que se le pide a una guardia de presupuesto.
    """

    def test_la_familia_por_defecto_tiene_tarifa_conocida(self):
        from src.drilldown import quota

        for etapa in modelos.ETAPAS:
            modelo = modelos.DEFAULTS_OCI[(modelos.FAMILIA_DEFAULT, etapa)]
            assert modelo in quota.PRECIOS_USD_MTOK, f"{modelo} no tiene tarifa"

    def test_una_familia_externa_sin_tarifa_se_cobra_como_la_mas_cara(self):
        from src.drilldown import quota

        caro = max(p[0] for p in quota.PRECIOS_USD_MTOK.values())
        for familia in modelos.FAMILIAS_EXTERNAS:
            modelo = modelos.DEFAULTS_OCI[(familia, "analisis")]
            if modelo in quota.PRECIOS_USD_MTOK:
                # El día que se cargue la tarifa real (consola de OCI → Cost
                # Management → Price List) este skip desaparece solo.
                continue
            assert quota.precio(modelo)[0] >= caro

# ── Piso de salida para familias que razonan sin decirlo ─────────────────────


class TestPisoDeRazonamiento:
    """El `max_tokens=512` del triaje está calibrado para Claude Haiku.

    Grok y Gemini gastan tokens de razonamiento que cuentan contra `maxTokens`
    pero no se reportan en `completion_tokens`. Medido el 2026-09-19: Gemini
    consumió 508 para emitir 21 de respuesta visible, el JSON del triaje quedó
    cortado a la mitad y **un GO se convirtió en NO-GO con confianza 0.0** por
    `parse_error`. Es el peor modo de falla del sistema —el pliego se descarta
    en silencio— y es intermitente, así que documentarlo no alcanzaba.
    """

    @pytest.mark.parametrize("familia", sorted(modelos.FAMILIAS_CON_RAZONAMIENTO_OCULTO))
    def test_el_triaje_recibe_aire_en_las_familias_que_razonan(self, familia):
        cfg = _cfg("oci", oci_familia=familia)
        assert modelos.max_tokens_de(cfg, "triaje", 512) == modelos.PISO_SALIDA_CON_RAZONAMIENTO

    def test_meta_no_lleva_piso_porque_no_razona_a_escondidas(self):
        """Llama reporta todo lo que gasta: 39 + 20 = 59 cierra exacto."""
        cfg = _cfg("oci", oci_familia="nativo")
        assert modelos.max_tokens_de(cfg, "triaje", 512) == 512

    def test_anthropic_tampoco(self):
        assert modelos.max_tokens_de(_cfg("anthropic"), "triaje", 512) == 512

    def test_el_piso_no_pisa_un_pedido_mayor(self):
        """Sube lo que viene corto; nunca baja lo que ya era suficiente."""
        cfg = _cfg("oci", oci_familia="google")
        assert modelos.max_tokens_de(cfg, "analisis", 16000) == 16000

    def test_el_piso_sigue_respetando_el_techo_del_modelo(self):
        """Un piso por encima del techo no puede generar un 400."""
        cfg = _cfg("oci", oci_familia="google",
                   oci_model_analysis="meta.llama-4-scout-17b-16e-instruct")
        assert modelos.max_tokens_de(cfg, "analisis", 100) <= modelos.TECHO_SALIDA[
            "meta.llama-4-scout-17b-16e-instruct"]


@pytest.mark.parametrize("modelo", [
    "meta.llama-4-scout-17b-16e-instruct",
    "meta.llama-4-maverick-17b-128e-instruct-fp8",
])
def test_verificacion_comparte_capacidades_nativas(modelo):
    cfg = _cfg("oci", oci_model_verification=modelo)
    assert llm.modelo_de(cfg, "verificacion") == modelo
    assert llm.soporta_esquema(modelo)
    assert llm.soporta_temperatura(modelo)
    assert llm.max_tokens_de(cfg, "verificacion", 8000) == 4096
