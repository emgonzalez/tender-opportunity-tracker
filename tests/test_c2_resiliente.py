"""C2 conserva evidencia y aísla fallos con proveedores simulados."""
import json
from types import SimpleNamespace

import pytest

from src.drilldown import lectura
from src.llm.contrato import Respuesta, Uso


@pytest.fixture
def entorno(monkeypatch):
    monkeypatch.setattr(lectura.pdf, "page_count", lambda _: 6)
    monkeypatch.setattr(lectura, "_contenido", lambda _, paginas, lado: list(paginas))
    return SimpleNamespace(lectura_tiempo_max_s=120)


def filas(paginas):
    return [{"n": n, "texto": f"literal {n}"} for n in paginas]


@pytest.mark.parametrize("fallo", ["invalida", "incompleta", "duplicada", "ajena", "excepcion"])
def test_lote_fallido_se_reintenta_sin_perder_anteriores(entorno, monkeypatch, fallo):
    llamadas = []

    def llamar(_, pedido, **kw):
        lote = pedido.contenido
        llamadas.append(lote)
        if lote == [4, 5, 6]:
            if fallo == "excepcion":
                raise RuntimeError("proveedor simulado")
            return {"invalida": [*filas([4]), {"n": 5, "texto": None}],
                    "incompleta": filas([4, 5]), "duplicada": filas([4, 4, 6]),
                    "ajena": filas([4, 5, 1])}[fallo], "falso"
        if lote == [5]:
            return [{"n": 5, "texto": None}], "falso"
        return filas(lote), "falso"

    monkeypatch.setattr(lectura, "_llamar", llamar)
    diagnostico = {}
    salida = lectura.transcribir_paginas("falso.pdf", range(1, 7), entorno, diagnostico)
    assert llamadas == [[1, 2, 3], [4, 5, 6], [4], [5], [6]]
    assert [p["pagina"] for p in salida] == [1, 2, 3, 4, 6]
    assert all(p["texto"] == f'literal {p["pagina"]}' for p in salida)
    assert diagnostico == {"c2_transcritas": 5, "c2_paginas_fallidas": [5],
                           "c2_transcripcion": "parcial"}


@pytest.mark.parametrize("limite", [lectura._PresupuestoAgotado, lectura._TiempoAgotado])
def test_limite_durante_reintentos_conserva_paginas(entorno, monkeypatch, limite):
    llamadas = []

    def llamar(_, pedido, **kw):
        lote = pedido.contenido
        llamadas.append(lote)
        if lote == [4, 5, 6]:
            raise ValueError("lote incompleto")
        if lote == [5]:
            raise limite("límite simulado")
        return filas(lote), "falso"

    monkeypatch.setattr(lectura, "_llamar", llamar)
    diagnostico = {}
    salida = lectura.transcribir_paginas("falso.pdf", range(1, 7), entorno, diagnostico)
    assert [p["pagina"] for p in salida] == [1, 2, 3, 4]
    assert llamadas == [[1, 2, 3], [4, 5, 6], [4], [5]]
    assert diagnostico["c2_paginas_fallidas"] == [5, 6]
    assert diagnostico["c2_transcripcion"] == "parcial"


def test_paridad_sin_fallos(entorno, monkeypatch):
    llamadas = []

    def llamar(_, pedido, **kw):
        llamadas.append(pedido.contenido)
        return filas(pedido.contenido), "falso"

    monkeypatch.setattr(lectura, "_llamar", llamar)
    diagnostico = {}
    salida = lectura.transcribir_paginas("falso.pdf", [6, 1, 2, 3, 4, 6], entorno, diagnostico)
    assert llamadas == [[6, 1, 2], [3, 4]]
    assert salida == [{"pagina": n, "texto": f"literal {n}", "fuente": "vision_llm",
                       "modelo": "falso", "calidad": None} for n in [6, 1, 2, 3, 4]]
    assert diagnostico == {"c2_transcritas": 5, "c2_paginas_fallidas": [],
                           "c2_transcripcion": "ok"}


def test_truncada_no_recupera_texto_parcial(entorno, monkeypatch):
    monkeypatch.setattr(lectura.quota, "cabe_lectura", lambda *a, **kw: True)
    monkeypatch.setattr(lectura.quota, "cobrar_lectura", lambda *a, **kw: None)
    monkeypatch.setattr(lectura.llm, "modelo_de", lambda *a: "falso")
    llamadas = []

    def completar(_, pedido):
        lote = pedido.contenido
        llamadas.append(lote)
        return Respuesta(json.dumps({"paginas": filas(lote)}), Uso(1, 1), "falso", "falso",
                         truncada=len(lote) > 1 or lote == [2])

    monkeypatch.setattr(lectura.llm, "completar", completar)
    diagnostico = {}
    salida = lectura.transcribir_paginas("falso.pdf", [1, 2, 3], entorno, diagnostico)
    assert llamadas == [[1, 2, 3], [1], [2], [3]]
    assert [p["pagina"] for p in salida] == [1, 3]
    assert diagnostico["c2_paginas_fallidas"] == [2]


def test_diagnostico_vacio_y_error(entorno, monkeypatch):
    diagnostico = {}
    assert lectura.transcribir_paginas("falso.pdf", [], entorno, diagnostico) == []
    assert diagnostico["c2_transcripcion"] == "vacia"
    monkeypatch.setattr(lectura, "_llamar", lambda *a, **kw: (_ for _ in ()).throw(ValueError()))
    assert lectura.transcribir_paginas("falso.pdf", [1], entorno, diagnostico) == []
    assert diagnostico == {"c2_transcritas": 0, "c2_paginas_fallidas": [1],
                           "c2_transcripcion": "error:ValueError"}
