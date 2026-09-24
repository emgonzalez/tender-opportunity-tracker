"""Tests del comparador de proveedores de LLM (scripts/comparar_llm.py). Sin red, sin LLM.

Lo que este script produce es la evidencia con la que se va a decidir si AMALIA
se queda en OCI o vuelve a Claude. Sus modos de falla no son estéticos:

- **Invertir las hipótesis de facturación.** Si la "optimista" saliera más cara
  que la "pesimista", el informe mandaría a buscar el número equivocado en Cost
  Analysis y la pregunta que motivó todo el ejercicio quedaría peor que sin
  medir. Por eso la desigualdad se fija acá como propiedad, no como ejemplo.
- **Presentar un promedio sobre 3 llamadas con la misma cara que uno sobre 300.**
  Las advertencias son parte del contrato del informe, no decoración: se testean.
- **Caerse leyendo.** El JSONL es append-only y se lee mientras la corrida de las
  07:00 lo escribe; una línea cortada a la mitad no puede tumbar el informe.
- **Dividir por cero.** Un proveedor sin ninguna llamada es el caso NORMAL al
  principio de la comparación, no una anomalía.
"""
import json

import pytest

from scripts.comparar_llm import (
    CHARS_POR_TOKEN_OCI,
    TARIFAS_OCI_10K,
    _percentil,
    agregar,
    grupo_de,
    leer_registro,
    transacciones_optimista,
    transacciones_pesimista,
)

# El caso medido que motiva el script: una página rasterizada de un pliego.
CHARS_BASE64_UNA_PAGINA = 45_329
TOKENS_UNA_PAGINA = 2_491


def _llamada(
    *,
    proveedor="oci",
    familia="nativo",
    modelo="meta.llama-4-scout-17b-16e-instruct",
    etapa="triaje",
    ts="2026-09-22T07:01:23.456-06:00",
    ok=True,
    latencia_ms=1893,
    pedido=512,
    efectivo=512,
    truncada=False,
    caracteres_texto=4213,
    caracteres_sistema=512,
    imagenes=0,
    salida_caracteres=118,
    input_tokens=None,
    output_tokens=78,
    razonamiento_oculto=0,
    costo_estimado_usd=0.0,
    version="8fa7610",
):
    """Una línea del registro con el esquema real, parametrizada por lo que importa."""
    base64_chars = imagenes * CHARS_BASE64_UNA_PAGINA
    if input_tokens is None:
        texto = caracteres_texto + caracteres_sistema
        input_tokens = int(texto / CHARS_POR_TOKEN_OCI) + imagenes * TOKENS_UNA_PAGINA
    return {
        "ts": ts,
        "proveedor": proveedor, "familia": familia, "modelo": modelo, "etapa": etapa,
        "ok": ok, "error": None if ok else "timeout", "latencia_ms": latencia_ms,
        "max_tokens_pedido": pedido, "max_tokens_efectivo": efectivo, "truncada": truncada,
        "entrada": {
            "caracteres_texto": caracteres_texto,
            "caracteres_sistema": caracteres_sistema,
            "caracteres_base64": base64_chars,
            "bytes_imagen": int(base64_chars * 0.75),
            "bloques": {"text": 1, "image": imagenes, "document": 0},
        },
        "salida": {"caracteres": salida_caracteres},
        "uso": {"input_tokens": input_tokens, "output_tokens": output_tokens},
        "uso_crudo": {
            "prompt_tokens": input_tokens,
            "completion_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
        },
        "razonamiento_oculto": razonamiento_oculto,
        "costo_estimado_usd": costo_estimado_usd,
        "version": version,
    }


def _registro(tmp_path, llamadas, *, cola_rota=False, nombre="llm_consumo.jsonl"):
    """Escribe el JSONL y devuelve su ruta. `cola_rota` simula el append en curso."""
    path = tmp_path / nombre
    with open(path, "w", encoding="utf-8") as f:
        for ll in llamadas:
            f.write(json.dumps(ll, ensure_ascii=False) + "\n")
        if cola_rota:
            f.write('{"ts":"2026-09-22T07:09:00-06:00","proveedor":"oci","fami')
    return str(path)


def _informe(tmp_path, llamadas, **kw):
    """Atajo: escribir, leer y agregar. Es lo que hace `main` sin imprimir."""
    path = _registro(tmp_path, llamadas, **{k: v for k, v in kw.items() if k == "cola_rota"})
    filas, diag = leer_registro(path)
    return agregar(filas, diag)


# ── 1 · Las dos hipótesis de facturación ─────────────────────────────────────


class TestHipotesisDeFacturacion:
    """La pregunta abierta: OCI no documenta cómo cuenta una imagen."""

    def test_con_imagen_la_pesimista_es_mayor_que_la_optimista(self):
        """La razón de ser del script: cobrar el base64 no es lo mismo que cobrar
        los tokens de la imagen, y la diferencia es lo que hay que ir a buscar a
        la factura."""
        fila = _llamada(imagenes=5)
        pes = transacciones_pesimista(fila)
        opt = transacciones_optimista(fila)
        assert pes > opt

    def test_el_factor_entre_hipotesis_es_el_medido(self):
        """45.329 chars de base64 contra 2.491 tokens, al ratio de 5,5: ~3,3×.

        Si este número se va lejos de 3, o la medición cambió o la aritmética se
        rompió; en los dos casos hay que mirar antes de creerle al informe.
        """
        fila = _llamada(imagenes=1, caracteres_texto=0, caracteres_sistema=0,
                        salida_caracteres=0)
        factor = transacciones_pesimista(fila) / transacciones_optimista(fila)
        assert 3.0 < factor < 3.6

    def test_sin_imagenes_las_dos_hipotesis_coinciden(self):
        """Donde no hay imagen no hay pregunta abierta: la doc de Oracle alcanza.

        Que coincidan por construcción evita una diferencia fantasma nacida de
        que el tokenizador no es exactamente 5,5 chars/token.
        """
        fila = _llamada(imagenes=0)
        assert transacciones_pesimista(fila) == transacciones_optimista(fila)

    def test_la_pesimista_es_la_suma_literal_de_caracteres(self):
        """La lectura literal de «1 character is calculated as 1 transaction»."""
        fila = _llamada(imagenes=2, caracteres_texto=1000, caracteres_sistema=500,
                        salida_caracteres=200)
        assert transacciones_pesimista(fila) == 1000 + 500 + 2 * CHARS_BASE64_UNA_PAGINA + 200

    def test_la_optimista_nunca_supera_a_la_pesimista(self):
        """Tope duro: si los tokens reportados fueran absurdamente altos, el
        aporte de la imagen se topa en su propio base64. Una hipótesis optimista
        más cara que la pesimista no es una hipótesis, es un error filtrándose."""
        fila = _llamada(imagenes=1, input_tokens=10_000_000)
        assert transacciones_optimista(fila) <= transacciones_pesimista(fila)

    def test_el_informe_da_las_dos_y_la_pesimista_cuesta_mas(self, tmp_path):
        res = _informe(tmp_path, [_llamada(imagenes=5) for _ in range(4)])
        m = res["grupos"]["oci/nativo"]
        assert m["costo_pesimista_usd"] > m["costo_optimista_usd"] > 0
        v = res["verificacion_factura"]
        assert v["aplica"] is True
        assert v["usd_hipotesis_pesimista"] > v["usd_hipotesis_optimista"]
        assert v["desde"] == "2026-09-22" and v["hasta"] == "2026-09-22"

    def test_la_tarifa_aplicada_es_la_de_lista_sin_margen(self, tmp_path):
        """Este informe NO usa la tarifa de `quota.py`: esa lleva un 1,5× de
        guardia. Comparar contra la factura con un precio inflado no compara nada."""
        fila = _llamada(imagenes=0, caracteres_texto=9_500, caracteres_sistema=500,
                        salida_caracteres=0)
        res = _informe(tmp_path, [fila])
        m = res["grupos"]["oci/nativo"]
        assert m["transacciones_pesimista"] == 10_000
        assert m["costo_pesimista_usd"] == pytest.approx(TARIFAS_OCI_10K["nativo"])


class TestTarifaDesconocida:
    """`xai` y `google`: OCI no publica el precio. Inventarlo sería el peor bug."""

    def test_familia_sin_tarifa_no_inventa_un_costo(self, tmp_path):
        res = _informe(tmp_path, [_llamada(familia="google", modelo="google.gemini-2.5-flash")])
        m = res["grupos"]["oci/google"]
        assert m["tarifa_10k"] is None
        assert m["costo_pesimista_usd"] is None
        assert m["costo_optimista_usd"] is None
        # Las transacciones SÍ se informan: son lo que no depende de la tarifa.
        assert m["transacciones_pesimista"] > 0
        assert any("TARIFA DESCONOCIDA" in a for a in res["advertencias"])

    def test_la_tarifa_se_puede_pasar_por_parametro(self, tmp_path):
        """El día que se consiga el número, el informe no necesita código nuevo."""
        path = _registro(tmp_path, [_llamada(familia="xai", modelo="xai.grok-4.6")])
        filas, diag = leer_registro(path)
        res = agregar(filas, diag, tarifas={"nativo": 0.0018, "xai": 0.01, "google": 0.01})
        m = res["grupos"]["oci/xai"]
        assert m["tarifa_10k"] == 0.01
        assert m["costo_pesimista_usd"] > 0

    def test_las_familias_sin_tarifa_quedan_fuera_del_total_a_verificar(self, tmp_path):
        """Y el informe lo nombra: un total que esconde media muestra es peor que
        un total que declara qué le falta."""
        res = _informe(tmp_path, [
            _llamada(),
            _llamada(familia="google", modelo="google.gemini-2.5-flash"),
        ])
        assert res["verificacion_factura"]["familias_sin_tarifa"] == ["google"]


# ── 2 · Agregación y comparación ─────────────────────────────────────────────


class TestAgregadoPorEtapaYProveedor:
    def test_separa_por_proveedor_y_por_familia(self, tmp_path):
        res = _informe(tmp_path, [
            _llamada(proveedor="anthropic", familia="", modelo="claude-haiku-4-5"),
            _llamada(familia="nativo"),
            _llamada(familia="google", modelo="google.gemini-2.5-flash"),
        ])
        assert set(res["grupos"]) == {"anthropic", "oci/nativo", "oci/google"}

    def test_la_familia_no_ensucia_la_clave_de_anthropic(self):
        """Anthropic no tiene familias: `anthropic/` sería una clave inventada."""
        assert grupo_de({"proveedor": "anthropic", "familia": ""}) == "anthropic"
        assert grupo_de({"proveedor": "oci", "familia": "nativo"}) == "oci/nativo"

    def test_cada_etapa_lleva_su_propia_cuenta(self, tmp_path):
        res = _informe(tmp_path, [
            _llamada(etapa="triaje"),
            _llamada(etapa="triaje"),
            _llamada(etapa="analisis", pedido=16000, efectivo=4096),
        ])
        assert res["etapas"]["triaje"]["oci/nativo"]["n"] == 2
        assert res["etapas"]["analisis"]["oci/nativo"]["n"] == 1

    def test_las_etapas_salen_en_orden_de_pipeline(self, tmp_path):
        """El informe se lee de arriba abajo como corre el sistema, no alfabético."""
        res = _informe(tmp_path, [_llamada(etapa="analisis"), _llamada(etapa="triaje")])
        assert list(res["etapas"]) == ["triaje", "analisis"]

    def test_suma_tokens_y_promedia_por_llamada(self, tmp_path):
        res = _informe(tmp_path, [
            _llamada(input_tokens=1000, output_tokens=100),
            _llamada(input_tokens=3000, output_tokens=300),
        ])
        m = res["grupos"]["oci/nativo"]
        assert m["input_tokens"] == 4000
        assert m["input_tokens_por_llamada"] == pytest.approx(2000)
        assert m["output_tokens_por_llamada"] == pytest.approx(200)

    def test_el_costo_de_anthropic_sale_del_registro_no_de_transacciones(self, tmp_path):
        """Anthropic factura por token y el registro ya trae el costo calculado.
        Tarifarlo con la conversión de OCI sería comparar peras con caracteres."""
        res = _informe(tmp_path, [
            _llamada(proveedor="anthropic", familia="", modelo="claude-haiku-4-5",
                     costo_estimado_usd=0.0041),
            _llamada(proveedor="anthropic", familia="", modelo="claude-haiku-4-5",
                     costo_estimado_usd=0.0059),
        ])
        m = res["grupos"]["anthropic"]
        assert m["costo_registrado_usd"] == pytest.approx(0.01)
        assert m["usd_por_llamada_registrado"] == pytest.approx(0.005)
        assert m["costo_pesimista_usd"] is None  # no aplica: no hay dos hipótesis


class TestPercentilesDeLatencia:
    def test_p50_y_p95_por_rango_mas_cercano(self):
        muestras = [100.0, 200.0, 300.0, 400.0, 500.0]
        assert _percentil(muestras, 0.50) == 300.0
        assert _percentil(muestras, 0.95) == 500.0

    def test_una_lista_vacia_no_revienta(self):
        assert _percentil([], 0.50) is None

    def test_el_p95_delata_la_cola_que_el_promedio_esconde(self, tmp_path):
        """La razón de pedir percentiles y no promedio: dieciocho llamadas de 1 s
        y dos de 30 s dan un promedio de 3,9 s que no le pasó a nadie. El p50
        dice 1 s (lo típico) y el p95 dice 30 s (lo que sufre el usuario de la
        cola); el promedio no dice ninguna de las dos cosas.

        Son DOS lentas y no una a propósito. Con una sola en veinte, el p95 vale
        1000 y tiene razón: diecinueve de veinte llamadas terminaron en 1 s, así
        que el percentil 95 de esa muestra ES 1 s y quien delata la cola es el
        máximo, no el p95. Pedirle al p95 que denuncie al único outlier de una
        muestra de veinte es pedirle que mienta."""
        llamadas = [_llamada(latencia_ms=1000) for _ in range(18)]
        llamadas += [_llamada(latencia_ms=30_000) for _ in range(2)]
        m = _informe(tmp_path, llamadas)["grupos"]["oci/nativo"]
        assert m["latencia_ms"]["p50"] == 1000
        assert m["latencia_ms"]["p95"] == 30_000

        # (18·1000 + 2·30000) / 20. El promedio no describe a NINGUNA de las
        # veinte llamadas: es casi 4× lo típico y casi 8× menos que la cola.
        # Por eso el informe publica percentiles y el promedio va al lado, no
        # en su lugar.
        assert m["latencia_ms"]["media"] == pytest.approx(3900)
        assert m["latencia_ms"]["p50"] < m["latencia_ms"]["media"] < m["latencia_ms"]["p95"]

    def test_las_llamadas_fallidas_no_entran_en_la_latencia(self, tmp_path):
        """Un timeout de 60 s no es una respuesta lenta, es un error. Mezclarlos
        convierte la tasa de error en un percentil: lo peor de los dos mundos."""
        res = _informe(tmp_path, [
            _llamada(ok=True, latencia_ms=1000),
            _llamada(ok=False, latencia_ms=60_000),
        ])
        m = res["grupos"]["oci/nativo"]
        assert m["latencia_ms"]["n"] == 1
        assert m["latencia_ms"]["p95"] == 1000
        assert m["tasa_error"] == pytest.approx(0.5)


# ── 3 · Razonamiento oculto ──────────────────────────────────────────────────


class TestRazonamientoOculto:
    """Grok y Gemini facturan tokens de razonamiento que no reportan. Con Claude
    ese costo no existe: ignorarlo inclina la comparación sin que se note."""

    def test_es_un_desglose_del_gasto_de_salida_y_no_un_extra(self, tmp_path):
        """El oculto ya está DENTRO de `output_tokens`, no encima.

        `Uso.desde_oci` fija `output_tokens = total_tokens - prompt_tokens`
        justamente para capturar lo que estas familias facturan sin reportar.
        Así que la línea real del registro para una llamada medida es
        `output_tokens=514` con `razonamiento_oculto=496`: 18 visibles y 496
        invisibles, 514 pagados. Sumar los dos campos daría 1.010 tokens y
        sobreestimaría el gasto de las familias externas casi al doble — justo
        en el eje que esta comparación existe para medir."""
        res = _informe(tmp_path, [
            _llamada(familia="google", modelo="google.gemini-2.5-flash",
                     output_tokens=514, razonamiento_oculto=496),
            _llamada(familia="google", modelo="google.gemini-2.5-flash",
                     output_tokens=514, razonamiento_oculto=496),
        ])
        m = res["grupos"]["oci/google"]
        assert m["output_tokens"] == 1028, "lo facturado, oculto incluido"
        assert m["razonamiento_oculto"] == 992
        assert m["output_tokens_visible"] == 36, "1028 - 992: lo que el proveedor sí reportó"
        # 992 / 1028: casi todo el gasto de salida es invisible.
        assert m["fraccion_razonamiento_oculto"] == pytest.approx(992 / 1028)

    def test_el_informe_nunca_presenta_visible_y_oculto_sumando_mas_que_lo_pagado(
        self, tmp_path
    ):
        """La invariante que el doble conteo rompía, como propiedad y no como caso."""
        m = _informe(tmp_path, [
            _llamada(familia="xai", modelo="xai.grok-4.6",
                     output_tokens=514, razonamiento_oculto=496),
        ])["grupos"]["oci/xai"]
        assert m["output_tokens_visible"] + m["razonamiento_oculto"] == m["output_tokens"]

    def test_con_claude_la_fraccion_es_cero_no_None(self, tmp_path):
        """Cero es un hecho medido ("Claude no razona en oculto"), no un hueco."""
        m = _informe(tmp_path, [
            _llamada(proveedor="anthropic", familia="", modelo="claude-haiku-4-5",
                     output_tokens=78),
        ])["grupos"]["anthropic"]
        assert m["razonamiento_oculto"] == 0
        assert m["fraccion_razonamiento_oculto"] == 0.0

    def test_sin_salida_ninguna_la_fraccion_es_None_y_no_divide_por_cero(self, tmp_path):
        m = _informe(tmp_path, [
            _llamada(ok=False, output_tokens=0, razonamiento_oculto=0),
        ])["grupos"]["oci/nativo"]
        assert m["fraccion_razonamiento_oculto"] is None


# ── 4 · Salud operativa ──────────────────────────────────────────────────────


class TestSaludOperativa:
    def test_cuenta_truncamientos_por_etapa(self, tmp_path):
        res = _informe(tmp_path, [
            _llamada(etapa="analisis", truncada=True),
            _llamada(etapa="analisis", truncada=False),
            _llamada(etapa="triaje", truncada=False),
        ])
        assert res["etapas"]["analisis"]["oci/nativo"]["n_truncada"] == 1
        assert res["etapas"]["analisis"]["oci/nativo"]["tasa_truncamiento"] == pytest.approx(0.5)
        assert res["etapas"]["triaje"]["oci/nativo"]["n_truncada"] == 0

    def test_detecta_el_clamp_del_techo_de_salida(self, tmp_path):
        """El análisis pide 16.000 y el techo de Meta es 4.096: el recorte es lo
        que explica los truncamientos, así que se cuenta aparte."""
        res = _informe(tmp_path, [
            _llamada(etapa="analisis", pedido=16000, efectivo=4096),
            _llamada(etapa="triaje", pedido=512, efectivo=512),
        ])
        assert res["etapas"]["analisis"]["oci/nativo"]["n_clamp"] == 1
        assert res["etapas"]["triaje"]["oci/nativo"]["n_clamp"] == 0


# ── 5 · Robustez de la lectura ───────────────────────────────────────────────


class TestLecturaRobusta:
    def test_una_linea_cortada_al_final_no_tumba_el_informe(self, tmp_path):
        """El caso real: el JSONL es append-only y la corrida de las 07:00 lo
        está escribiendo mientras lo leemos."""
        path = _registro(tmp_path, [_llamada() for _ in range(3)], cola_rota=True)
        filas, diag = leer_registro(path)
        assert len(filas) == 3
        assert diag["corruptas"] == 1

    def test_el_informe_declara_cuantas_saltea(self, tmp_path):
        """Una línea salteada en silencio es una medición que se pierde sin que
        nadie se entere; por eso sube a ADVERTENCIAS y no solo al log."""
        res = _informe(tmp_path, [_llamada() for _ in range(3)], cola_rota=True)
        assert res["registro"]["corruptas"] == 1
        assert any("ilegible" in a for a in res["advertencias"])

    def test_una_linea_corrupta_en_el_medio_tampoco(self, tmp_path):
        path = tmp_path / "r.jsonl"
        path.write_text(
            json.dumps(_llamada()) + "\n"
            + "{esto no es json}\n"
            + "[1, 2, 3]\n"          # JSON válido pero no es una llamada
            + "\n"                   # línea vacía: no cuenta como corrupta
            + json.dumps(_llamada()) + "\n",
            encoding="utf-8",
        )
        filas, diag = leer_registro(str(path))
        assert len(filas) == 2
        assert diag["corruptas"] == 2

    def test_una_linea_a_la_que_le_faltan_campos_no_revienta(self, tmp_path):
        """Una línea escrita por una versión anterior del registro vale menos que
        una completa, pero vale más que un stack trace."""
        path = tmp_path / "r.jsonl"
        path.write_text(
            json.dumps({"ts": "2026-09-22T07:00:00-06:00", "proveedor": "oci",
                        "familia": "nativo", "etapa": "triaje", "ok": True}) + "\n",
            encoding="utf-8",
        )
        filas, diag = leer_registro(str(path))
        res = agregar(filas, diag)
        assert res["grupos"]["oci/nativo"]["n"] == 1
        assert res["grupos"]["oci/nativo"]["input_tokens"] == 0


class TestVentanaDeFechas:
    def test_filtra_por_rango_inclusive_en_los_dos_extremos(self, tmp_path):
        from scripts.comparar_llm import _limite

        path = _registro(tmp_path, [
            _llamada(ts="2026-09-19T07:00:00-06:00"),
            _llamada(ts="2026-09-20T07:00:00-06:00"),
            _llamada(ts="2026-09-22T23:59:00-06:00"),
            _llamada(ts="2026-09-23T07:00:00-06:00"),
        ])
        filas, diag = leer_registro(
            path, _limite("2026-09-20", fin=False), _limite("2026-09-22", fin=True)
        )
        assert len(filas) == 2
        assert diag["fuera_de_rango"] == 2

    def test_un_limite_sin_huso_no_explota_contra_un_ts_con_huso(self, tmp_path):
        """`--desde 2026-09-20` es naive y el registro escribe con offset -06:00.
        Comparar aware con naive es un TypeError, y sería un TypeError en
        producción, no en un test."""
        from scripts.comparar_llm import _limite

        path = _registro(tmp_path, [_llamada(ts="2026-09-22T07:00:00-06:00")])
        filas, _ = leer_registro(path, _limite("2026-09-22", fin=False), None)
        assert len(filas) == 1


# ── 6 · Honestidad del informe: cuándo la comparación NO es justa ────────────


class TestAdvertenciasDeComparabilidad:
    def test_un_proveedor_sin_ninguna_llamada_se_dice_no_se_divide_por_cero(self, tmp_path):
        """El caso NORMAL al empezar la comparación: solo hay datos de uno."""
        res = _informe(tmp_path, [_llamada() for _ in range(3)])
        assert "anthropic" not in res["grupos"]
        assert any("SIN DATOS" in a and "anthropic" in a for a in res["advertencias"])

    def test_un_registro_vacio_no_revienta(self, tmp_path):
        res = _informe(tmp_path, [])
        assert res["grupos"] == {}
        assert res["etapas"] == {}
        assert res["verificacion_factura"]["aplica"] is False
        assert len(res["advertencias"]) >= 2  # falta anthropic y falta oci

    def test_una_muestra_chica_se_marca_como_tal(self, tmp_path):
        """Un promedio sobre 3 llamadas presentado igual que uno sobre 300 es
        engañoso: es el modo de falla que estas advertencias existen para tapar."""
        res = _informe(tmp_path, [_llamada() for _ in range(3)])
        assert any("solo 3 llamada" in a for a in res["advertencias"])

    def test_una_muestra_grande_no_se_marca(self, tmp_path):
        res = _informe(tmp_path, [_llamada() for _ in range(40)])
        assert not any("mínimo sugerido" in a for a in res["advertencias"])

    def test_el_N_siempre_esta_en_el_informe(self, tmp_path):
        """Que el N viaje con cada número es la mitad de la honestidad; la otra
        mitad son las advertencias."""
        res = _informe(tmp_path, [_llamada(etapa="triaje") for _ in range(7)])
        assert res["grupos"]["oci/nativo"]["n"] == 7
        assert res["etapas"]["triaje"]["oci/nativo"]["n"] == 7

    def test_avisa_cuando_un_grupo_no_cubre_las_mismas_etapas(self, tmp_path):
        res = _informe(tmp_path, [
            _llamada(proveedor="anthropic", familia="", modelo="claude-haiku-4-5",
                     etapa="triaje"),
            _llamada(proveedor="anthropic", familia="", modelo="claude-sonnet-5",
                     etapa="analisis"),
            _llamada(etapa="triaje"),
        ])
        assert any("no tiene datos de: analisis" in a and "oci/nativo" in a
                   for a in res["advertencias"])

    def test_avisa_cuando_ninguna_etapa_es_comparable(self, tmp_path):
        res = _informe(tmp_path, [
            _llamada(proveedor="anthropic", familia="", modelo="claude-sonnet-5",
                     etapa="analisis"),
            _llamada(etapa="triaje"),
        ])
        assert any("NINGUNA etapa" in a for a in res["advertencias"])

    def test_avisa_cuando_los_periodos_no_se_solapan(self, tmp_path):
        """Si Claude se midió en junio y OCI en septiembre, la diferencia puede
        ser el corpus de esos días y no el proveedor."""
        res = _informe(tmp_path, [
            _llamada(proveedor="anthropic", familia="", modelo="claude-haiku-4-5",
                     ts="2026-06-01T07:00:00-06:00"),
            _llamada(ts="2026-09-22T07:00:00-06:00"),
        ])
        assert any("NO se solapan" in a for a in res["advertencias"])

    def test_no_avisa_de_periodos_cuando_si_se_solapan(self, tmp_path):
        res = _informe(tmp_path, [
            _llamada(proveedor="anthropic", familia="", modelo="claude-haiku-4-5",
                     ts="2026-09-20T07:00:00-06:00"),
            _llamada(proveedor="anthropic", familia="", modelo="claude-haiku-4-5",
                     ts="2026-09-23T07:00:00-06:00"),
            _llamada(ts="2026-09-22T07:00:00-06:00"),
        ])
        assert not any("NO se solapan" in a for a in res["advertencias"])

    def test_avisa_cuando_el_registro_abarca_varias_versiones_del_codigo(self, tmp_path):
        """Parte de la diferencia puede ser un cambio de prompt y no del modelo."""
        res = _informe(tmp_path, [
            _llamada(version="8fa7610"),
            _llamada(version="deadbee"),
        ])
        assert any("versiones del código" in a for a in res["advertencias"])

    def test_avisa_que_el_ratio_de_las_familias_externas_no_esta_medido(self, tmp_path):
        res = _informe(tmp_path, [_llamada(familia="xai", modelo="xai.grok-4.6")])
        assert any("ratio chars/token" in a for a in res["advertencias"])


# ── 7 · Salida ───────────────────────────────────────────────────────────────


class TestSalida:
    def test_el_informe_de_texto_nombra_el_numero_a_buscar_en_la_consola(
        self, tmp_path, capsys
    ):
        """Sin el paso de verificación, las dos hipótesis son una especulación
        bien formateada."""
        from scripts.comparar_llm import imprimir

        res = _informe(tmp_path, [_llamada(imagenes=5) for _ in range(3)])
        imprimir(res)
        salida = capsys.readouterr().out
        assert "Cost Analysis" in salida
        assert "pesimista" in salida and "optimista" in salida
        assert "ADVERTENCIAS" in salida

    def test_el_informe_no_revienta_con_un_registro_vacio(self, tmp_path, capsys):
        from scripts.comparar_llm import imprimir

        imprimir(_informe(tmp_path, []))
        assert "Nada que comparar" in capsys.readouterr().out

    def test_json_serializa_sin_sets_ni_datetimes(self, tmp_path, capsys):
        from scripts.comparar_llm import main

        path = _registro(tmp_path, [_llamada(imagenes=1), _llamada(etapa="analisis")])
        assert main(["--registro", path, "--json"]) == 0
        datos = json.loads(capsys.readouterr().out)
        assert datos["grupos"]["oci/nativo"]["n"] == 2
        assert isinstance(datos["advertencias"], list)

    def test_main_con_un_registro_inexistente_falla_con_codigo_y_mensaje(
        self, tmp_path, capsys
    ):
        from scripts.comparar_llm import main

        assert main(["--registro", str(tmp_path / "no-existe.jsonl")]) == 1
        assert "No existe el registro" in capsys.readouterr().err

    def test_main_con_una_fecha_invalida_no_tira_stack_trace(self, tmp_path, capsys):
        from scripts.comparar_llm import main

        path = _registro(tmp_path, [_llamada()])
        assert main(["--registro", path, "--desde", "el martes"]) == 2
        assert "Fecha inválida" in capsys.readouterr().err

    def test_la_tarifa_por_linea_de_comandos_alcanza_a_todas_las_familias(
        self, tmp_path, capsys
    ):
        from scripts.comparar_llm import main

        path = _registro(tmp_path, [_llamada(familia="google",
                                             modelo="google.gemini-2.5-flash")])
        assert main(["--registro", path, "--json", "--tarifa-oci-10k", "0.05"]) == 0
        datos = json.loads(capsys.readouterr().out)
        assert datos["grupos"]["oci/google"]["tarifa_10k"] == 0.05
        assert datos["grupos"]["oci/google"]["costo_pesimista_usd"] > 0
