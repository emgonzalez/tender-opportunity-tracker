"""Evaluador de precisión de CR con CSV simulados: casos de métrica conocidos, filas sin etiquetar y CSV inválidos."""
import csv
import json

import pytest

from scripts import evaluar_precision_cr as evp
from scripts import exportar_muestra_cr as ex


def _clave(i, *, amalia, estrato=ex.RELEVANTE_ALTO, peso=1.0, marcas=None, modalidad="LD", unspsc="43211503"):
    return {"id": f"sicop_cr:{i}", "estrato": estrato, "poblacion_estrato": "1", "muestra_estrato": "1",
            "peso": str(peso), "es_relevante_amalia": "sí" if amalia else "no", "match": "90",
            "es_relevante_col": "1" if amalia else "0", "triaje": "", "estado": "DESCUBIERTO",
            "categoria_detectada": "", "marcas_amalia": json.dumps(marcas or []), "modalidad_codigo": modalidad,
            "competibilidad_codigo": "", "unspsc": unspsc, "senal_ti": ""}


def _ciego(i, relevante, *, marcas="", ofertable="", institucion="MEP", descripcion="Compra de computadoras"):
    fila = {c: "" for c in ex.COLUMNAS_CIEGO}
    fila.update({"id": f"sicop_cr:{i}", "institucion": institucion, "descripcion": descripcion,
                 ex.COL_RELEVANTE: relevante, ex.COL_MARCAS: marcas, ex.COL_OFERTABLE: ofertable})
    return fila


def caso_conocido():
    """TP 6, FP 2, FN 3, TN 9 → precisión 0,75, recobro 6/9."""
    ciego, clave, i = [], [], 0
    for amalia, humano, n in ((True, "sí", 6), (True, "no", 2), (False, "Sí", 3), (False, "NO", 9)):
        for _ in range(n):
            ciego.append(_ciego(i, humano))
            clave.append(_clave(i, amalia=amalia, estrato=ex.RELEVANTE_ALTO if amalia else ex.NO_RELEVANTE_AZAR))
            i += 1
    return ciego, clave


def test_metricas_relevante_caso_conocido():
    ciego, clave = caso_conocido()
    r = evp.evaluar(ciego, clave, minimo=10)
    m = r["relevante"]["muestra"]
    assert m["matriz"] == {"tp": 6, "fp": 2, "fn": 3, "tn": 9}
    assert m["precision"] == pytest.approx(0.75)
    assert m["recobro"] == pytest.approx(6 / 9)
    assert m["wilson_precision"] == pytest.approx(list(evp.ev.wilson(6, 8)))
    assert m["wilson_recobro"] == pytest.approx(list(evp.ev.wilson(6, 9)))
    assert m["acuerdo_bruto"] == pytest.approx(15 / 20)
    # Con pesos 1 la estimación ponderada coincide con la de la muestra.
    assert r["relevante"]["ponderado"]["precision"] == pytest.approx(0.75)
    assert r["decision_relevante"] == {"estado": "no_cumple", "motivos": ["recobro ponderado 0.667 < 0.85"]}
    assert r["errores"]["falsos_negativos"] == sorted(f"sicop_cr:{i}" for i in range(8, 11))


def test_ponderacion_por_estrato_cambia_el_recobro():
    # Muestra: 1 TP en relevante_alto (peso 1) y 1 FN en no_relevante_senal_ti (peso 10).
    ciego = [_ciego(0, "sí"), _ciego(1, "sí")]
    clave = [_clave(0, amalia=True), _clave(1, amalia=False, estrato=ex.NO_RELEVANTE_SENAL_TI, peso=10)]
    r = evp.evaluar(ciego, clave, minimo=1)
    assert r["relevante"]["muestra"]["recobro"] == pytest.approx(0.5)
    assert r["relevante"]["ponderado"]["recobro"] == pytest.approx(1 / 11)


def test_filas_sin_etiquetar_se_cuentan_y_no_fallan():
    ciego, clave = caso_conocido()
    ciego[0][ex.COL_RELEVANTE] = ""
    ciego[1][ex.COL_RELEVANTE] = ""
    ciego[1][ex.COL_MARCAS] = "Oracle"
    r = evp.evaluar(ciego, clave, minimo=100)
    assert r["conteos"]["etiquetadas"] == 18 and r["conteos"]["sin_etiquetar"] == 2
    assert any("sicop_cr:1" in a for a in r["avisos"])
    assert r["decision_relevante"]["estado"] == "insuficiente"
    vacio = evp.evaluar([_ciego(0, "")], [_clave(0, amalia=True)])
    assert vacio["conteos"]["etiquetadas"] == 0 and vacio["relevante"]["ponderado"] is None


def test_cumple_umbrales():
    ciego, clave = [], []
    for i in range(10):
        ciego.append(_ciego(i, "si" if i < 8 else "no"))
        clave.append(_clave(i, amalia=i < 9))
    r = evp.evaluar(ciego, clave, minimo=10)
    assert r["relevante"]["muestra"]["precision"] == pytest.approx(8 / 9)
    assert r["decision_relevante"]["estado"] == "cumple"


def test_por_marca_reutiliza_la_puerta():
    ciego = [_ciego(0, "sí", marcas="Oracle"), _ciego(1, "sí", marcas="oracle netsuite; HPE"),
             _ciego(2, "no", marcas="ninguna"), _ciego(3, "sí", marcas="Fortinet")]
    clave = [
        _clave(0, amalia=True, marcas=[{"marca": "Oracle", "veredicto": "confirmada", "score": 0.9}]),
        _clave(1, amalia=True, marcas=[{"marca": "Oracle NetSuite", "veredicto": "por_confirmar", "score": 0.6}]),
        _clave(2, amalia=True, marcas=[{"marca": "Oracle", "veredicto": "fuera", "score": 0.2},
                                       {"marca": "HPE", "veredicto": "por_confirmar", "score": None}]),
        _clave(3, amalia=True, marcas=[]),
    ]
    marcas = evp.evaluar(ciego, clave, minimo=1)["por_marca"]
    oracle = marcas["oracle"]
    assert oracle["matriz"] == {"tp": 2, "fp": 0, "fn": 0, "tn": 2}
    assert oracle["recobro"] == 1.0 and oracle["precision_confirmada"] == 1.0
    assert not oracle["puerta"]["cumple"] and any("Wilson Oracle" in m for m in oracle["puerta"]["motivos"])
    assert marcas["hpe"]["matriz"] == {"tp": 0, "fp": 1, "fn": 1, "tn": 2}
    assert marcas["fortinet"]["matriz"]["fn"] == 1  # AMALIA no la puso: «fuera», no faltante
    assert set(marcas) == {"oracle", "hpe", "fortinet"}


def test_modalidades_contradicciones():
    respuestas = [{"codigo": "PX", ex.COL_MOD_OFERTABLE: "sí"}, {"codigo": "LD", ex.COL_MOD_OFERTABLE: "no"},
                  {"codigo": "CD", ex.COL_MOD_OFERTABLE: "depende"}, {"codigo": "RE", ex.COL_MOD_OFERTABLE: "no"},
                  {"codigo": "PE", ex.COL_MOD_OFERTABLE: ""}]
    ciego = [_ciego(0, "sí", ofertable="sí"), _ciego(1, "sí", ofertable="sí"), _ciego(2, "no", ofertable="no")]
    clave = [_clave(0, amalia=True, modalidad="PP"), _clave(1, amalia=True, modalidad="LD"),
             _clave(2, amalia=False, modalidad="LD")]
    mod = evp.evaluar(ciego, clave, respuestas, minimo=1)["modalidades"]
    resultado = {e["codigo"]: e["resultado"] for e in mod["equipo"]}
    assert resultado == {"PX": "contradice", "LD": "contradice", "CD": "coincide", "RE": "coincide"}
    por_codigo = {m["codigo"]: m for m in mod["muestra"]}
    assert por_codigo["PP"]["contradicen"] == 1 and por_codigo["LD"]["contradicen"] == 1
    assert mod["contradicciones"] == ["LD", "PP", "PX"]


def test_tabla_errores_por_familia():
    ciego = [_ciego(0, "no", institucion="CCSS", descripcion="Mantenimiento de ascensores"),
             _ciego(1, "sí", institucion="ICE", descripcion="Licencias de firewall"),
             _ciego(2, "sí", institucion="ICE")]
    clave = [_clave(0, amalia=True, unspsc="72101500"), _clave(1, amalia=False, unspsc="43233200"),
             _clave(2, amalia=True)]
    e = evp.evaluar(ciego, clave, minimo=1)["errores"]
    familias = {g["grupo"]: g for g in e["unspsc_familia"]}
    assert familias["7210"]["fp"] == 1 and familias["4323"]["fn"] == 1
    assert {g["grupo"] for g in e["institucion"]} == {"CCSS", "ICE"}
    assert any(g["grupo"] == "ascensores" for g in e["palabra"])


@pytest.mark.parametrize("valor", ["quizás", "2", "talvez"])
def test_valor_de_etiqueta_invalido(valor):
    ciego, clave = caso_conocido()
    ciego[4][ex.COL_RELEVANTE] = valor
    with pytest.raises(evp.CsvInvalido, match=r"fila 6, columna «relevante \(sí/no\)»"):
        evp.evaluar(ciego, clave)


def test_ids_inconsistentes():
    ciego, clave = caso_conocido()
    with pytest.raises(evp.CsvInvalido, match="no está en clave_veredictos"):
        evp.evaluar(ciego + [_ciego(99, "sí")], clave)
    with pytest.raises(evp.CsvInvalido, match="id repetido"):
        evp.evaluar(ciego + [ciego[0]], clave)
    r = evp.evaluar(ciego[:-1], clave, minimo=1)
    assert r["conteos"]["faltan_en_ciego"] == 1
    clave[0]["peso"] = "abc"
    with pytest.raises(evp.CsvInvalido, match="peso inválido"):
        evp.evaluar(ciego, clave)


def _escribir(ruta, columnas, filas, delimitador=","):
    with open(ruta, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=columnas, delimiter=delimitador)
        w.writeheader()
        w.writerows(filas)


def test_leer_csv_valida_columnas_y_celdas(tmp_path):
    ruta = tmp_path / "ciego.csv"
    _escribir(ruta, ["id", "descripcion"], [{"id": "x", "descripcion": "y"}])
    with pytest.raises(evp.CsvInvalido, match="faltan columnas"):
        evp.leer_csv(ruta, ex.COLUMNAS_CIEGO)
    ruta.write_text("id,relevante (sí/no)\nsicop_cr:1,sí,de más\n", encoding="utf-8")
    with pytest.raises(evp.CsvInvalido, match="más celdas que columnas"):
        evp.leer_csv(ruta, ("id",))
    with pytest.raises(evp.CsvInvalido, match="no existe"):
        evp.leer_csv(tmp_path / "nada.csv", ("id",))
    (tmp_path / "vacio.csv").write_text("", encoding="utf-8")
    with pytest.raises(evp.CsvInvalido, match="vacío"):
        evp.leer_csv(tmp_path / "vacio.csv", ("id",))


def test_main_de_punta_a_punta_con_punto_y_coma(tmp_path):
    ciego, clave = caso_conocido()
    _escribir(tmp_path / "muestra_ciega.csv", list(ex.COLUMNAS_CIEGO), ciego, delimitador=";")  # Excel en español
    _escribir(tmp_path / "clave_veredictos.csv", list(ex.COLUMNAS_CLAVE), clave)
    _escribir(tmp_path / "modalidades.csv", list(ex.COLUMNAS_MODALIDADES),
              [{"codigo": "PX", ex.COL_MOD_OFERTABLE: "sí"}])
    mensajes = []
    assert evp.main(["--ciego", str(tmp_path / "muestra_ciega.csv"), "--clave",
                     str(tmp_path / "clave_veredictos.csv"), "--modalidades", str(tmp_path / "modalidades.csv"),
                     "--minimo-etiquetadas", "10"], decir=mensajes.append) == 0
    datos = json.loads((tmp_path / "informe_precision_cr.json").read_text(encoding="utf-8"))
    assert datos["relevante"]["muestra"]["matriz"]["tp"] == 6
    assert datos["modalidades"]["contradicciones"] == ["PX"]
    md = (tmp_path / "informe_precision_cr.md").read_text(encoding="utf-8")
    assert "Decisión «relevante»: no_cumple" in md and "| PX | sí | no_competible | contradice |" in md
    # El informe es agregado: ninguna descripción del concurso aparece.
    assert "Compra de computadoras" not in md
    _escribir(tmp_path / "clave_veredictos.csv", ["id"], [{"id": "x"}])
    assert evp.main(["--ciego", str(tmp_path / "muestra_ciega.csv"), "--clave",
                     str(tmp_path / "clave_veredictos.csv")], decir=mensajes.append) == 2
    assert mensajes[-1].startswith("ERROR:") and "faltan columnas" in mensajes[-1]


def test_sombra_contra_activa_con_mcnemar():
    ciego, clave = caso_conocido()  # la activa falla en 5 de 20 (2 FP, 3 FN); esta sombra acierta todo
    sombra = [{"id": c["id"], "es_relevante": "sí" if f[ex.COL_RELEVANTE].lower() == "sí" else "no"}
              for c, f in zip(clave, ciego)]
    sombra.append({"id": "sicop_cr:no-etiquetado", "es_relevante": "sí"})
    s = evp.evaluar(ciego, clave, minimo=1, sombra=sombra)["sombra"]
    assert s["n"] == 20 and s["precision"] == 1.0 and s["recobro"] == 1.0
    assert s["mcnemar"]["sombra_mejor"] == 5 and s["mcnemar"]["activa_mejor"] == 0
    assert s["mcnemar"]["p"] == pytest.approx(0.0625)
    with pytest.raises(evp.CsvInvalido, match="columna «es_relevante»"):
        evp.evaluar(ciego, clave, sombra=[{"id": "sicop_cr:0", "es_relevante": "tal vez"}])


def test_ida_y_vuelta_con_el_exportador(tmp_path):
    filas = [ex.normalizar_fila({
        "proceso_gid": f"sicop_cr:{i}", "numero_referencia": str(i), "institucion": "MEP", "titulo": "Servidores",
        "modalidad_raw": "PROCEDIMIENTO POR EXCEPCIÓN", "fecha_pub_utc": None, "monto_original": 10.0,
        "moneda": "CRC", "es_relevante": int(i % 2 == 0), "triaje": None, "estado_cp": "DESCUBIERTO",
        "match_tecnico": 0.9, "match_categoria": 0.9, "factibilidad": 0.9, "categoria_detectada": "",
        "marcas": "[]", "clasificado_en": None}) for i in range(20)]
    ex.exportar(filas, tmp_path, objetivos=ex.OBJETIVOS_DEFAULT, semilla=1, patron=None)
    ciego = evp.leer_csv(tmp_path / "muestra_ciega.csv", ex.COLUMNAS_CIEGO)
    for fila in ciego:
        fila[ex.COL_RELEVANTE] = "sí"
        fila[ex.COL_OFERTABLE] = "sí"
    clave = evp.leer_csv(tmp_path / "clave_veredictos.csv", ex.COLUMNAS_CLAVE)
    r = evp.evaluar(ciego, clave, minimo=1)
    assert r["conteos"]["etiquetadas"] == 20
    assert r["relevante"]["muestra"]["matriz"] == {"tp": 10, "fp": 0, "fn": 10, "tn": 0}
    assert r["modalidades"]["contradicciones"] == ["PX"]
