"""Tests del calibrador de la cuota (scripts/calibrar_cuota.py). Sin red, sin LLM.

Lo que se calibra es el coeficiente ANALISIS_TOKENS_POR_PAGINA, que es la única
pieza de la guardia que no podemos medir del lado cliente (el PDF lo rasteriza
Anthropic del lado servidor). Que el calibrador se equivoque en la dirección de
la recomendación es peor que no tenerlo: sugerir bajar un coeficiente que en
realidad subestima abre la puerta al gasto que la guardia debía frenar.
"""
import json

import pytest

from scripts.calibrar_cuota import (
    _recomendacion,
    _stats,
    analizar,
    leer_analisis,
)
from src.drilldown.quota import ANALISIS_TOKENS_POR_PAGINA


def _fila(uuid="P1", *, n_paginas=20, formato="ESCANEADO",
          triaje=None, analisis=None, estado="ANALIZADO"):
    def uso(modelo, inp, out):
        return {"modelo": modelo, "input_tokens": inp, "output_tokens": out, "llamadas": 1}

    return {
        "proceso_uuid": uuid, "estado": estado, "formato": formato,
        "n_paginas": n_paginas, "tamano_bytes": 1_000_000,
        "uso_triaje": uso("claude-haiku-4-5", *triaje) if triaje else None,
        "uso_analisis": uso("claude-sonnet-5", *analisis) if analisis else None,
    }


def _escribir(tmp_path, cliente, filas):
    d = tmp_path / "history"
    d.mkdir(exist_ok=True)
    with open(d / f"{cliente}.analisis.jsonl", "w", encoding="utf-8") as f:
        for fila in filas:
            f.write(json.dumps(fila) + "\n")


class TestLectura:
    def test_junta_varios_clientes(self, tmp_path):
        _escribir(tmp_path, "a", [_fila("P1"), _fila("P2")])
        _escribir(tmp_path, "b", [_fila("P3")])
        assert len(leer_analisis(str(tmp_path))) == 3

    def test_una_linea_corrupta_no_invalida_el_historial(self, tmp_path):
        _escribir(tmp_path, "a", [_fila("P1")])
        with open(tmp_path / "history" / "a.analisis.jsonl", "a") as f:
            f.write("{ esto no es json\n")
            f.write(json.dumps(_fila("P2")) + "\n")
        assert len(leer_analisis(str(tmp_path))) == 2

    def test_sin_archivos_devuelve_vacio(self, tmp_path):
        assert leer_analisis(str(tmp_path)) == []


class TestStats:
    def test_la_mediana_resiste_un_outlier(self):
        """El promedio lo secuestra un pliego de 100 páginas entre veinte de cinco."""
        s = _stats([100, 100, 100, 100, 100_000])
        assert s["mediana"] == 100
        assert s["max"] == 100_000

    def test_p90_por_encima_de_la_mediana(self):
        s = _stats(list(range(1, 101)))
        assert s["mediana"] <= s["p90"] <= s["max"]

    def test_vacio_no_revienta(self):
        assert _stats([]) == {}


class TestAnalisis:
    def test_separa_el_gasto_por_modelo(self, tmp_path):
        filas = [_fila("P1", triaje=(10_000, 50), analisis=(60_000, 1_000))]
        res = analizar(filas)
        assert res["modelos"]["claude-haiku-4-5"]["gasto_usd"] == pytest.approx(0.01025)
        assert res["modelos"]["claude-sonnet-5"]["gasto_usd"] == pytest.approx(0.195)

    def test_agrupa_tokens_por_pagina_por_etapa_y_formato(self, tmp_path):
        filas = [
            _fila("P1", n_paginas=20, formato="ESCANEADO", analisis=(60_000, 0)),
            _fila("P2", n_paginas=10, formato="NATIVO", analisis=(10_000, 0)),
        ]
        res = analizar(filas)
        assert res["etapas"]["analisis/ESCANEADO"]["tokens_por_unidad"]["mediana"] == 3000
        assert res["etapas"]["analisis/NATIVO"]["tokens_por_unidad"]["mediana"] == 1000

    def test_el_triaje_se_normaliza_por_llamada_no_por_pagina(self):
        """Su costo está topado en triage_raster_pages: dividirlo por páginas daría
        un 'tok/pág' que BAJA cuanto más largo el pliego, la lectura al revés."""
        filas = [_fila("P1", n_paginas=10, triaje=(12_660, 48)),
                 _fila("P2", n_paginas=60, triaje=(12_660, 48))]
        e = analizar(filas)["etapas"]["triaje/ESCANEADO"]
        assert e["unidad"] == "llamada"
        assert e["tokens_por_unidad"]["min"] == e["tokens_por_unidad"]["max"] == 12_660

    def test_ignora_las_corridas_anteriores_a_la_instrumentacion(self, tmp_path):
        """n_paginas = 0 no se puede dividir; esas filas no aportan al coeficiente."""
        filas = [_fila("P1", n_paginas=0, analisis=(60_000, 0)),
                 _fila("P2", n_paginas=20, analisis=(60_000, 0))]
        res = analizar(filas)
        assert res["etapas"]["analisis/ESCANEADO"]["tokens_por_unidad"]["n"] == 1
        # pero su gasto sí cuenta: el dinero se gastó igual
        assert res["modelos"]["claude-sonnet-5"]["llamadas"] == 2

    def test_una_etapa_que_no_corrio_no_aparece(self, tmp_path):
        res = analizar([_fila("P1", triaje=(10_000, 50))])  # NO-GO: sin análisis
        assert "triaje/ESCANEADO" in res["etapas"]
        assert not any(k.startswith("analisis/") for k in res["etapas"])


class TestRecomendacion:
    def test_avisa_cuando_el_coeficiente_subestima(self):
        caro = ANALISIS_TOKENS_POR_PAGINA * 2
        res = analizar([_fila(f"P{i}", n_paginas=10, analisis=(caro * 10, 0))
                        for i in range(12)])
        sugerido, nota = _recomendacion(res)
        assert sugerido > ANALISIS_TOKENS_POR_PAGINA
        assert "provisional" not in nota  # 12 muestras: ya no es provisional

    def test_usa_el_p90_no_la_mediana(self):
        """La guardia debe dimensionarse para la cola cara, no para el pliego típico.

        Con 10 pliegos baratos y 4 caros la mediana daría 1.000 tok/pág y dejaría
        la guardia corta en casi un tercio del corpus.
        """
        filas = [_fila(f"P{i}", n_paginas=10, analisis=(10_000, 0)) for i in range(10)]
        filas += [_fila(f"C{i}", n_paginas=10, analisis=(80_000, 0)) for i in range(4)]
        sugerido, _ = _recomendacion(analizar(filas))
        assert sugerido == 8_000

    def test_marca_las_muestras_chicas_como_provisionales(self):
        res = analizar([_fila("P1", n_paginas=10, analisis=(30_000, 0))])
        _, nota = _recomendacion(res)
        assert "provisional" in nota

    def test_sin_datos_de_analisis_no_inventa_un_numero(self):
        """Es el estado de hoy: hasta que corra en producción, no hay qué calibrar."""
        sugerido, nota = _recomendacion(analizar([_fila("P1", triaje=(10_000, 50))]))
        assert sugerido is None
        assert "Sin datos" in nota
