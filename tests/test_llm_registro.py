"""Tests del libro de consumo del LLM (`src/llm/registro.py`).

Lo que cubre, en orden de gravedad de la falla que previene:

1. **Que `caracteres_base64` sea exacto.** Es la razón de ser del archivo: la
   pregunta abierta es si OCI factura el base64 de una imagen (45.329
   caracteres medidos) o los tokens que reporta (2.491) — un factor 3,3× que,
   de ser el primero, invierte el argumento económico de la migración entera.
   Un contador mal sumado no se detecta leyendo el JSONL: los números se ven
   plausibles igual.
2. **Que el registro no pueda tumbar la corrida.** Es contabilidad colgada del
   único camino por el que pasan las cinco llamadas al modelo. Si un disco
   lleno matara `completar()`, el libro habría costado más que todo lo que
   mide.
3. **Que las llamadas fallidas se anoten y la excepción siga viaje.** Las dos
   mitades: sin lo primero el patrón de fallos no se compara; sin lo segundo el
   registro cambia el comportamiento del sistema, que es exactamente lo que no
   puede hacer.
4. **Que NO se filtre contenido.** Los pliegos son documentos de clientes. El
   test busca cadenas distintivas del prompt en el archivo y exige que no
   estén.
5. Que `razonamiento_oculto` recupere lo que Grok y Gemini cobran sin reportar.

La `cfg` es un `types.SimpleNamespace`, como en `test_llm_modelos.py`: el módulo
lee todo con `getattr(cfg, campo, default)` justamente para eso.
"""
from __future__ import annotations

import json
import types

import pytest

from src import llm
from src.llm import registro
from src.llm.contrato import BloqueImagen, BloqueTexto, LLMError, Pedido, Respuesta, Uso

# Cadenas que solo pueden llegar al archivo si alguien registró CONTENIDO.
TEXTO_SECRETO = "CLAUSULA-CONFIDENCIAL-XYZZY-42"
SISTEMA_SECRETO = "INSTRUCCION-DE-SISTEMA-PLUGH-77"
B64_SECRETO = "QUJDRA" * 40  # 240 caracteres de base64 de utilería
RESPUESTA_SECRETA = "RESPUESTA-DEL-MODELO-THUD-99"


def _cfg(tmp_path, **campos):
    base = {
        "llm_provider": "oci",
        "oci_familia": "nativo",
        "data_dir": str(tmp_path),
        "llm_registro_path": str(tmp_path / "llm_consumo.jsonl"),
        "llm_registro_habilitado": True,
        "app_version": "8fa7610",
    }
    base.update(campos)
    return types.SimpleNamespace(**base)


def _pedido(*, con_imagenes=0):
    contenido = [BloqueTexto(texto=TEXTO_SECRETO)]
    contenido += [BloqueImagen(datos_b64=B64_SECRETO) for _ in range(con_imagenes)]
    return Pedido(
        etapa="triaje",
        contenido=contenido,
        sistema=SISTEMA_SECRETO,
        max_tokens=512,
    )


def _respuesta(*, uso_crudo=None, texto=RESPUESTA_SECRETA, truncada=False):
    resp = Respuesta(
        texto=texto,
        uso=Uso(input_tokens=12455, output_tokens=78),
        modelo="meta.llama-4-scout-17b-16e-instruct",
        proveedor="oci",
        truncada=truncada,
    )
    if uso_crudo is not None:
        # El adaptador todavía no adjunta el `usage` crudo; el registro lo lee
        # por `getattr` para estar listo el día que lo haga (ver SOLICITUD DE
        # INTEGRACIÓN en el reporte de la rama).
        resp.uso_crudo = uso_crudo
    return resp


def _adaptador_falso(monkeypatch, funcion):
    """Reemplaza la resolución del adaptador: `completar()` llama a `funcion`."""
    monkeypatch.setattr(llm, "_adaptador", lambda proveedor, etapa="": funcion)


def _lineas(cfg) -> list[dict]:
    """Parsea el libro línea por línea. Falla si alguna no es JSON válido."""
    ruta = registro.ruta_del_libro(cfg)
    with open(ruta, "r", encoding="utf-8") as f:
        crudas = f.read().splitlines()
    assert crudas, "el libro quedó vacío"
    return [json.loads(linea) for linea in crudas]


# ── 1. El dato que motiva el archivo: caracteres de base64 ───────────────────


class TestCaracteresDeBase64:
    def test_suma_el_base64_de_cada_imagen(self, tmp_path, monkeypatch):
        """5 páginas rasterizadas = 5 × len(datos_b64), ni más ni menos."""
        cfg = _cfg(tmp_path)
        _adaptador_falso(monkeypatch, lambda c, p, cliente=None: _respuesta())
        llm.completar(cfg, _pedido(con_imagenes=5))

        entrada = _lineas(cfg)[0]["entrada"]
        assert entrada["caracteres_base64"] == 5 * len(B64_SECRETO)
        assert entrada["bloques"] == {"text": 1, "image": 5, "document": 0}

    def test_bytes_imagen_es_el_tamano_decodificado(self, tmp_path, monkeypatch):
        """Las dos lecturas del mismo dato: transporte (b64) y pixel (bytes)."""
        cfg = _cfg(tmp_path)
        _adaptador_falso(monkeypatch, lambda c, p, cliente=None: _respuesta())
        llm.completar(cfg, _pedido(con_imagenes=2))

        entrada = _lineas(cfg)[0]["entrada"]
        assert entrada["bytes_imagen"] == entrada["caracteres_base64"] * 3 // 4

    def test_sin_imagenes_queda_en_cero_y_no_en_None(self, tmp_path, monkeypatch):
        cfg = _cfg(tmp_path)
        _adaptador_falso(monkeypatch, lambda c, p, cliente=None: _respuesta())
        llm.completar(cfg, _pedido())

        entrada = _lineas(cfg)[0]["entrada"]
        assert (entrada["caracteres_base64"], entrada["bytes_imagen"]) == (0, 0)

    def test_el_sistema_se_cuenta_APARTE_del_texto(self, tmp_path, monkeypatch):
        """En OCI el prompt de sistema también se factura: va separado."""
        cfg = _cfg(tmp_path)
        _adaptador_falso(monkeypatch, lambda c, p, cliente=None: _respuesta())
        llm.completar(cfg, _pedido())

        entrada = _lineas(cfg)[0]["entrada"]
        medido = (entrada["caracteres_texto"], entrada["caracteres_sistema"])
        assert medido == (len(TEXTO_SECRETO), len(SISTEMA_SECRETO))


# ── 2. El JSONL es JSONL ─────────────────────────────────────────────────────


class TestFormatoDelLibro:
    def test_cada_llamada_es_una_linea_json_valida(self, tmp_path, monkeypatch):
        cfg = _cfg(tmp_path)
        _adaptador_falso(monkeypatch, lambda c, p, cliente=None: _respuesta())
        for _ in range(3):
            llm.completar(cfg, _pedido(con_imagenes=1))

        lineas = _lineas(cfg)
        assert len(lineas) == 3
        assert all(isinstance(linea, dict) for linea in lineas)

    def test_el_libro_es_APPEND_ONLY_entre_corridas(self, tmp_path, monkeypatch):
        """No se resetea: es la diferencia con `gasto_diario.json`."""
        cfg = _cfg(tmp_path)
        _adaptador_falso(monkeypatch, lambda c, p, cliente=None: _respuesta())
        llm.completar(cfg, _pedido())
        llm.completar(cfg, _pedido())
        assert len(_lineas(cfg)) == 2

    def test_la_linea_trae_el_esquema_completo(self, tmp_path, monkeypatch):
        """El script de comparación se escribe contra estas claves."""
        cfg = _cfg(tmp_path)
        _adaptador_falso(monkeypatch, lambda c, p, cliente=None: _respuesta())
        llm.completar(cfg, _pedido(con_imagenes=1))

        linea = _lineas(cfg)[0]
        esperadas = {
            "ts", "proveedor", "familia", "modelo", "etapa", "ok", "error",
            "latencia_ms", "max_tokens_pedido", "max_tokens_efectivo", "truncada",
            "entrada", "salida", "uso", "uso_crudo", "razonamiento_oculto",
            "costo_estimado_usd", "version",
        }
        assert esperadas <= set(linea)
        assert set(linea["entrada"]) == {
            "caracteres_texto", "caracteres_sistema", "caracteres_base64",
            "bytes_imagen", "bloques",
        }
        assert (linea["proveedor"], linea["familia"], linea["etapa"]) == ("oci", "nativo", "triaje")
        assert linea["version"] == "8fa7610"
        assert linea["uso"] == {"input_tokens": 12455, "output_tokens": 78}
        assert linea["salida"]["caracteres"] == len(RESPUESTA_SECRETA)
        assert linea["latencia_ms"] >= 0

    def test_max_tokens_efectivo_muestra_el_recorte_del_modelo(self, tmp_path, monkeypatch):
        """Pedir 16.000 contra Meta (techo 4.096) tiene que quedar registrado."""
        cfg = _cfg(tmp_path)
        _adaptador_falso(monkeypatch, lambda c, p, cliente=None: _respuesta())
        pedido = _pedido()
        pedido.max_tokens = 16000
        llm.completar(cfg, pedido)

        linea = _lineas(cfg)[0]
        assert (linea["max_tokens_pedido"], linea["max_tokens_efectivo"]) == (16000, 4096)

    def test_con_anthropic_la_familia_queda_vacia(self, tmp_path, monkeypatch):
        """El eje `familia` solo existe dentro de OCI."""
        cfg = _cfg(tmp_path, llm_provider="anthropic")
        respuesta = Respuesta(
            texto="ok", uso=Uso(1, 2), modelo="claude-haiku-4-5", proveedor="anthropic"
        )
        _adaptador_falso(monkeypatch, lambda c, p, cliente=None: respuesta)
        llm.completar(cfg, _pedido())

        linea = _lineas(cfg)[0]
        assert (linea["proveedor"], linea["familia"]) == ("anthropic", "")
        assert "input_tokens" in linea["uso_crudo"]


# ── 3. Las llamadas que fallan también cuentan ───────────────────────────────


class TestLlamadasFallidas:
    def test_registra_ok_false_y_RE_LANZA(self, tmp_path, monkeypatch):
        cfg = _cfg(tmp_path)

        def explota(c, p, cliente=None):
            raise LLMError("429 too many requests", proveedor="oci", etapa="triaje")

        _adaptador_falso(monkeypatch, explota)
        with pytest.raises(LLMError):
            llm.completar(cfg, _pedido(con_imagenes=3))

        linea = _lineas(cfg)[0]
        assert linea["ok"] is False
        assert "429" in linea["error"]
        assert linea["uso"] == {"input_tokens": 0, "output_tokens": 0}
        # Lo que SÍ se midió aunque la llamada muriera: cuánto se le mandó.
        assert linea["entrada"]["caracteres_base64"] == 3 * len(B64_SECRETO)

    def test_el_modelo_de_una_fallida_se_resuelve_igual(self, tmp_path, monkeypatch):
        """Sin `Respuesta` no hay `modelo`; se resuelve el que le tocaba."""
        cfg = _cfg(tmp_path)

        def explota(c, p, cliente=None):
            raise LLMError("timeout")

        _adaptador_falso(monkeypatch, explota)
        with pytest.raises(LLMError):
            llm.completar(cfg, _pedido())

        assert _lineas(cfg)[0]["modelo"] == "meta.llama-4-scout-17b-16e-instruct"

    def test_el_mensaje_de_error_se_acota(self, tmp_path, monkeypatch):
        """Un SDK puede devolver el request entero —o sea, el prompt— en el error."""
        cfg = _cfg(tmp_path)

        def explota(c, p, cliente=None):
            raise RuntimeError("x" * 5000)

        _adaptador_falso(monkeypatch, explota)
        with pytest.raises(RuntimeError):
            llm.completar(cfg, _pedido())

        assert len(_lineas(cfg)[0]["error"]) == registro.MAX_ERROR

    def test_una_llamada_ok_deja_error_en_None(self, tmp_path, monkeypatch):
        cfg = _cfg(tmp_path)
        _adaptador_falso(monkeypatch, lambda c, p, cliente=None: _respuesta())
        llm.completar(cfg, _pedido())

        linea = _lineas(cfg)[0]
        assert (linea["ok"], linea["error"]) == (True, None)


# ── 4. El registro NUNCA tumba la corrida ────────────────────────────────────


class TestDegradacionElegante:
    def test_un_path_imposible_no_rompe_completar(self, tmp_path, monkeypatch, caplog):
        """El padre del libro es un ARCHIVO: `makedirs` no puede con eso."""
        obstaculo = tmp_path / "soy-un-archivo"
        obstaculo.write_text("no soy un directorio", encoding="utf-8")
        cfg = _cfg(tmp_path, llm_registro_path=str(obstaculo / "sub" / "libro.jsonl"))

        esperada = _respuesta()
        _adaptador_falso(monkeypatch, lambda c, p, cliente=None: esperada)
        with caplog.at_level("WARNING"):
            devuelta = llm.completar(cfg, _pedido())

        assert devuelta is esperada
        assert any("registro" in r.message for r in caplog.records)

    def test_un_fallo_al_escribir_no_se_traga_la_excepcion_del_LLM(
        self, tmp_path, monkeypatch
    ):
        """Dos fallos a la vez: el del proveedor manda, el del libro se calla."""
        cfg = _cfg(tmp_path)

        def escritura_rota(*a, **k):
            raise OSError("disco lleno")

        monkeypatch.setattr(registro, "_escribir", escritura_rota)

        def explota(c, p, cliente=None):
            raise LLMError("503 service unavailable")

        _adaptador_falso(monkeypatch, explota)
        with pytest.raises(LLMError, match="503"):
            llm.completar(cfg, _pedido())

    def test_deshabilitado_no_escribe_nada(self, tmp_path, monkeypatch):
        cfg = _cfg(tmp_path, llm_registro_habilitado=False)
        _adaptador_falso(monkeypatch, lambda c, p, cliente=None: _respuesta())
        llm.completar(cfg, _pedido())

        assert not (tmp_path / "llm_consumo.jsonl").exists()

    def test_registrar_directamente_tampoco_lanza_con_basura(self, tmp_path):
        """Contrato duro: `registrar` no lanza ni con un pedido que no es un Pedido."""
        cfg = _cfg(tmp_path)
        registro.registrar(cfg, object(), proveedor="oci", latencia_ms=1)

    def test_la_ruta_por_defecto_cuelga_de_data_dir(self, tmp_path):
        cfg = types.SimpleNamespace(data_dir=str(tmp_path))
        assert registro.ruta_del_libro(cfg).endswith(registro.NOMBRE_DEFAULT)
        assert str(tmp_path) in registro.ruta_del_libro(cfg)


# ── 5. Privacidad: tamaños, nunca contenido ──────────────────────────────────


class TestNoSeFiltraContenido:
    @pytest.mark.parametrize(
        "secreto",
        [TEXTO_SECRETO, SISTEMA_SECRETO, B64_SECRETO, RESPUESTA_SECRETA],
        ids=["texto", "sistema", "base64", "respuesta"],
    )
    def test_el_libro_no_contiene_el_prompt_ni_la_respuesta(
        self, tmp_path, monkeypatch, secreto
    ):
        """Los pliegos son datos de clientes. Al libro entran longitudes."""
        cfg = _cfg(tmp_path)
        _adaptador_falso(monkeypatch, lambda c, p, cliente=None: _respuesta())
        llm.completar(cfg, _pedido(con_imagenes=2))

        crudo = (tmp_path / "llm_consumo.jsonl").read_text(encoding="utf-8")
        assert secreto not in crudo

    def test_tampoco_se_filtra_por_la_via_del_error(self, tmp_path, monkeypatch):
        """El mensaje acotado es lo único que entra, y entra recortado."""
        cfg = _cfg(tmp_path)

        def explota(c, p, cliente=None):
            raise LLMError("400 bad request: " + "z" * 400 + TEXTO_SECRETO)

        _adaptador_falso(monkeypatch, explota)
        with pytest.raises(LLMError):
            llm.completar(cfg, _pedido())

        crudo = (tmp_path / "llm_consumo.jsonl").read_text(encoding="utf-8")
        assert TEXTO_SECRETO not in crudo


# ── 6. Lo que los proveedores cobran y no reportan ───────────────────────────


class TestRazonamientoOculto:
    def test_recupera_los_496_tokens_que_grok_no_informa(self):
        """Medido 2026-09-19: `completion_tokens=18` sobre `total_tokens=553`."""
        crudo = types.SimpleNamespace(prompt_tokens=39, completion_tokens=18, total_tokens=553)
        assert registro.razonamiento_oculto(crudo) == 496

    def test_cuando_cierra_exacto_da_cero(self):
        """Meta no razona invisiblemente: 39 + 20 = 59, no hay nada que recuperar."""
        crudo = types.SimpleNamespace(prompt_tokens=39, completion_tokens=20, total_tokens=59)
        assert registro.razonamiento_oculto(crudo) == 0

    @pytest.mark.parametrize(
        "crudo",
        [
            None,
            types.SimpleNamespace(prompt_tokens=39, completion_tokens=18),
            types.SimpleNamespace(prompt_tokens=39, completion_tokens=None, total_tokens=43),
            types.SimpleNamespace(prompt_tokens=39, completion_tokens=18, total_tokens="basura"),
            types.SimpleNamespace(prompt_tokens=39, completion_tokens=18, total_tokens=10),
        ],
        ids=["sin-usage", "sin-total", "gemini-sin-completion", "total-basura", "total-menor"],
    )
    def test_lo_incompleto_o_corrupto_nunca_da_negativo(self, crudo):
        assert registro.razonamiento_oculto(crudo) == 0

    def test_llega_entero_hasta_el_libro(self, tmp_path, monkeypatch):
        cfg = _cfg(tmp_path)
        crudo = types.SimpleNamespace(prompt_tokens=39, completion_tokens=18, total_tokens=553)
        _adaptador_falso(
            monkeypatch, lambda c, p, cliente=None: _respuesta(uso_crudo=crudo)
        )
        llm.completar(cfg, _pedido())

        linea = _lineas(cfg)[0]
        assert linea["razonamiento_oculto"] == 496
        # Y el `usage` del proveedor queda VERBATIM, con sus nombres nativos:
        # sin esto no hay contra qué auditar la normalización de `contrato.Uso`.
        assert linea["uso_crudo"] == {
            "prompt_tokens": 39, "completion_tokens": 18, "total_tokens": 553,
        }

    def test_sin_usage_crudo_la_linea_se_marca_como_reconstruida(
        self, tmp_path, monkeypatch
    ):
        """No se audita una normalización contra su propio resultado."""
        cfg = _cfg(tmp_path)
        _adaptador_falso(monkeypatch, lambda c, p, cliente=None: _respuesta())
        llm.completar(cfg, _pedido())

        crudo = _lineas(cfg)[0]["uso_crudo"]
        assert crudo["_reconstruido"] is True
        assert crudo["prompt_tokens"] == 12455
