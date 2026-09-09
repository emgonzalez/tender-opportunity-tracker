"""Tests de la lógica de agregación del script de medición del corpus. Sin red.

El script `scripts/medir_pliegos.py` es el que produce el dato con el que se
calibrará la heurística de cuota. Si su agregación miente, la calibración
hereda el error — de ahí que se testee como código de producción.
"""
import types

import fitz  # pymupdf
import pytest

from scripts.medir_pliegos import _percentil, medir_pdf, resumir


def _cfg(raster=5):
    return types.SimpleNamespace(triage_raster_pages=raster)


def _pdf(tmp_path, nombre, ancho, alto, paginas=1, texto=None):
    path = tmp_path / nombre
    doc = fitz.open()
    for _ in range(paginas):
        page = doc.new_page(width=ancho, height=alto)
        if texto:
            page.insert_text((72, 100), texto)
    doc.save(str(path))
    doc.close()
    return str(path)


class TestPercentil:
    def test_lista_vacia(self):
        assert _percentil([], 90) == 0.0

    def test_un_solo_valor(self):
        assert _percentil([7], 90) == 7.0

    def test_mediana(self):
        assert _percentil([1, 2, 3, 4, 5], 50) == 3

    def test_p90_interpola(self):
        assert _percentil([10, 20], 50) == 15.0

    def test_p100_es_el_maximo(self):
        assert _percentil([3, 1, 2], 100) == 3


class TestMedirPdf:
    def test_detecta_nativo_y_cuenta_texto(self, tmp_path):
        p = _pdf(tmp_path, "n.pdf", 612, 792,
                 texto="Servidor rack con firewall y almacenamiento institucional")
        m = medir_pdf(p, _cfg())
        assert m["formato"] == "NATIVO"
        assert m["n_paginas"] == 1
        assert m["tamano_bytes"] > 0
        assert m["chars_texto"] > 40

    def test_detecta_escaneado(self, tmp_path):
        p = _pdf(tmp_path, "s.pdf", 2550, 3300)  # sin capa de texto
        m = medir_pdf(p, _cfg())
        assert m["formato"] == "ESCANEADO"
        assert "chars_texto" not in m  # solo se extrae texto de los nativos

    def test_triaje_cuesta_menos_que_el_documento_completo(self, tmp_path):
        """Es el ahorro que introdujo el rasterizado acotado; debe ser medible."""
        p = _pdf(tmp_path, "s.pdf", 2550, 3300, paginas=20)
        m = medir_pdf(p, _cfg(raster=5))
        assert m["tokens_triaje_img"] * 4 == m["tokens_doc_completo_img"]

    def test_pdf_corrupto_reporta_error_sin_reventar(self, tmp_path):
        bad = tmp_path / "malo.pdf"
        bad.write_bytes(b"no soy un pdf")
        m = medir_pdf(str(bad), _cfg())
        assert m["error"].startswith("pdf_corrupto")
        assert m["n_paginas"] == 0


class TestResumir:
    def _fila(self, paginas=10, formato="ESCANEADO", tokens_doc=25330, tam=1_000_000):
        return {
            "n_paginas": paginas, "formato": formato, "tamano_bytes": tam,
            "tokens_doc_completo_img": tokens_doc,
            "tokens_triaje_img": tokens_doc // max(paginas, 1) * 5,
            "heuristica_actual": paginas * 2000 + 5000,
        }

    def test_muestra_vacia(self):
        assert resumir([]) == {"medidos": 0}

    def test_ignora_filas_con_error(self):
        r = resumir([self._fila(), {"error": "sin_pliego"}])
        assert r["medidos"] == 1
        assert r["con_error"] == 1

    def test_cuenta_los_que_pasan_de_80_y_100_paginas(self):
        filas = [self._fila(paginas=n) for n in (10, 85, 120)]
        r = resumir(filas)
        assert r["sobre_80_paginas"] == 2   # 85 y 120
        assert r["sobre_100_paginas"] == 1  # solo 120

    def test_reporta_porcentaje_de_escaneados(self):
        filas = [self._fila(formato="ESCANEADO"), self._fila(formato="ESCANEADO"),
                 self._fila(formato="NATIVO"), self._fila(formato="NATIVO")]
        assert resumir(filas)["formato"]["pct_escaneado"] == 50.0

    def test_detecta_que_la_heuristica_subestima(self):
        """25 págs escaneadas: real 63.325 tok vs heurística 55.000 → 1.15x."""
        r = resumir([self._fila(paginas=25, tokens_doc=63_325)])
        assert r["heuristica_vs_real"]["ratio_mediano"] == pytest.approx(1.15, abs=0.01)
        assert r["heuristica_vs_real"]["subestima_en"] == 1

    def test_detecta_que_la_heuristica_sobreestima_los_nativos(self):
        """25 págs nativas cuestan ~16k, no 55k: la heurística sobra 3x."""
        r = resumir([self._fila(paginas=25, formato="NATIVO", tokens_doc=16_150)])
        assert r["heuristica_vs_real"]["ratio_mediano"] < 0.5
        assert r["heuristica_vs_real"]["subestima_en"] == 0

    def test_separa_tokens_por_pagina_segun_formato(self):
        """El hallazgo que justifica calibrar por formato y no por páginas."""
        filas = [self._fila(paginas=10, formato="ESCANEADO", tokens_doc=25_330),
                 self._fila(paginas=10, formato="NATIVO", tokens_doc=6_460)]
        t = resumir(filas)["tokens_por_pagina_img"]
        assert t["ESCANEADO"] == 2533
        assert t["NATIVO"] == 646

    def test_costea_cada_modelo_a_su_precio(self):
        """Haiku $1/MTok y Sonnet $3/MTok: sumar tokens de ambos sería erróneo."""
        r = resumir([self._fila(paginas=10, formato="ESCANEADO", tokens_doc=1_000_000)])
        c = r["costo_usd_muestra"]
        assert c["analisis_sonnet"] == pytest.approx(3.0)
        assert c["triaje_haiku"] < c["analisis_sonnet"]
