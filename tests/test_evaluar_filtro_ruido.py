"""Evaluación offline con títulos y juicios sintéticos."""
import json

import pytest

from scripts.evaluar_filtro_ruido import evaluar_archivo, main


def _datos(tmp_path, filas):
    ruta = tmp_path / "juicios.jsonl"
    ruta.write_text("\n".join(json.dumps(fila) for fila in filas), encoding="utf-8")
    return ruta


def test_metricas_y_revision(tmp_path):
    ruta = _datos(tmp_path, [
        {"titulo": "Compra de aire acondicionado", "legado": 1, "juicio_relevante": False},
        {"titulo": "Compra de equipo medico", "legado": 1, "juicio_relevante": True},
        {"titulo": "Software para equipo medico", "legado": 1, "juicio_relevante": True},
        {"titulo": "Televisores para sala", "legado": 1, "juicio_relevante": False},
        {"titulo": "Compra de aire acondicionado", "legado": 0, "juicio_relevante": False},
    ])
    informe = evaluar_archivo(ruta)
    assert informe["total"] == 5
    assert informe["filtraria"] == 3
    assert informe["relevantes_filtrados"] == 1
    assert informe["perdida_recall_casos"] == 1
    assert informe["perdida_recall"] == 0.5
    assert informe["falsos_positivos_eliminados"] == 1
    assert informe["reduccion_ruido"] == 0.5
    assert informe["precision_filtro"] == pytest.approx(2 / 3)
    assert [fila["linea"] for fila in informe["revision"]] == [2]


def test_vacio_y_cli(tmp_path, capsys):
    ruta = _datos(tmp_path, [])
    assert main(["--datos", str(ruta), "--json"]) == 0
    informe = json.loads(capsys.readouterr().out)
    assert informe["precision_filtro"] is None
    assert informe["perdida_recall"] is None
    assert main(["--datos", str(ruta)]) == 0
    assert "sin denominador" in capsys.readouterr().out


@pytest.mark.parametrize("fila", [
    {}, {"titulo": "Equipo", "legado": True, "juicio_relevante": False},
    {"titulo": "Equipo", "legado": 1, "juicio_relevante": "false"},
    {"titulo": "Equipo", "legado": 2, "juicio_relevante": False},
    {"titulo": "Equipo", "legado": 1, "juicio_relevante": False, "institucion": 3},
])
def test_rechaza_juicios_invalidos(tmp_path, fila):
    with pytest.raises(ValueError, match="Línea 1"):
        evaluar_archivo(_datos(tmp_path, [fila]))


def test_json_invalido_y_error_cli(tmp_path):
    ruta = tmp_path / "invalido.jsonl"
    ruta.write_text("{", encoding="utf-8")
    with pytest.raises(ValueError, match="JSON inválido"):
        evaluar_archivo(ruta)
    with pytest.raises(SystemExit) as exc:
        main(["--datos", str(ruta)])
    assert exc.value.code == 2
