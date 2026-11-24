"""Exportador de la muestra ciega de CR con filas simuladas: sin ADB, sin red."""
import contextlib
import csv
import json
from datetime import datetime

import pytest

from scripts import exportar_muestra_cr as ex

PATRON = ex.compilar_patron(["computadoras", "licencia", "servidor", "firewall"])


def _cruda(i, *, rel=0, mt=0.0, mc=0.0, fa=0.0, titulo="Compra de alimentos", unspsc=None, estado="DESCUBIERTO",
           marcas=None, modalidad="LICITACIÓN REDUCIDA"):
    fila = {
        "proceso_gid": f"sicop_cr:2026LD-{i:06d}-0001", "numero_referencia": f"2026LD-{i:06d}-0001",
        "institucion": f"Institución {i % 7}", "titulo": titulo, "modalidad_raw": modalidad,
        "fecha_pub_utc": datetime(2026, 11, 20, 15), "monto_original": 1000.0 + i, "moneda": "crc",
        "es_relevante": rel, "triaje": None, "estado_cp": estado, "match_tecnico": mt, "match_categoria": mc,
        "factibilidad": fa, "categoria_detectada": "Ciberseguridad" if rel else "",
        "marcas": json.dumps(marcas or []), "clasificado_en": datetime(2026, 11, 20, 16),
    }
    if unspsc is not None:
        fila["unspsc"] = unspsc
    return fila


def poblacion():
    filas = []
    for i in range(100):    # relevantes con match 90
        filas.append(_cruda(i, rel=1, mt=0.9, mc=0.9, fa=0.9, titulo="Licencias de firewall",
                            marcas=[{"marca": "Fortinet", "veredicto": "confirmada", "score": 0.9}]))
    for i in range(100, 160):  # borde: relevantes con match 60
        filas.append(_cruda(i, rel=1, mt=0.6, mc=0.6, fa=0.6, titulo="Equipo de cómputo"))
    for i in range(160, 200):  # no relevantes con señal de TI (UNSPSC 43)
        filas.append(_cruda(i, unspsc=["43211503"]))
    for i in range(200, 230):  # no relevantes con señal de TI (palabra)
        filas.append(_cruda(i, titulo="Compra de COMPUTADORAS portátiles"))
    for i in range(230, 400):  # no relevantes sin señal
        filas.append(_cruda(i, unspsc=["50192100"]))
    filas.append(_cruda(999, rel=None))  # sin clasificar
    filas[-1]["clasificado_en"] = None
    return [ex.normalizar_fila(f) for f in filas]


def test_estratos_y_semilla_reproducible():
    muestra1, resumen = ex.muestrear(poblacion(), ex.OBJETIVOS_DEFAULT, 7, PATRON)
    muestra2, _ = ex.muestrear(poblacion(), ex.OBJETIVOS_DEFAULT, 7, PATRON)
    muestra3, _ = ex.muestrear(poblacion(), ex.OBJETIVOS_DEFAULT, 8, PATRON)
    assert [f["proceso_gid"] for f in muestra1] == [f["proceso_gid"] for f in muestra2]
    assert [f["proceso_gid"] for f in muestra1] != [f["proceso_gid"] for f in muestra3]
    e = resumen["estratos"]
    assert e[ex.RELEVANTE_ALTO] == {"poblacion": 100, "objetivo": 60, "muestra": 60, "faltante": 0}
    assert e[ex.BORDE]["muestra"] == 40 and e[ex.BORDE]["poblacion"] == 60
    assert e[ex.NO_RELEVANTE_SENAL_TI]["poblacion"] == 70 and e[ex.NO_RELEVANTE_SENAL_TI]["muestra"] == 30
    assert e[ex.NO_RELEVANTE_AZAR]["poblacion"] == 170 and e[ex.NO_RELEVANTE_AZAR]["muestra"] == 50
    assert resumen["total"] == 180 and resumen["sin_clasificar_excluidos"] == 1
    assert len({f["proceso_gid"] for f in muestra1}) == 180
    # Los estratos quedan mezclados en el orden final (no en bloques).
    primeros = {f["estrato"] for f in muestra1[:20]}
    assert len(primeros) > 1
    alto = next(f for f in muestra1 if f["estrato"] == ex.RELEVANTE_ALTO)
    assert alto["peso"] == pytest.approx(100 / 60)


def test_estrato_chico_se_censa_sin_redistribuir():
    filas = [ex.normalizar_fila(_cruda(i, rel=1, mt=1, mc=1, fa=1)) for i in range(5)]
    muestra, resumen = ex.muestrear(filas, ex.OBJETIVOS_DEFAULT, 1, PATRON)
    assert len(muestra) == 5
    assert resumen["estratos"][ex.RELEVANTE_ALTO]["faltante"] == 55
    assert all(f["peso"] == 1 for f in muestra)


def test_estrato_de_borde_y_senal():
    nr_borde = ex.normalizar_fila(_cruda(1, mt=0.6, mc=0.6, fa=0.6))
    assert ex.estrato_de(nr_borde, PATRON) == ex.BORDE
    rel_bajo = ex.normalizar_fila(_cruda(2, rel=1, mt=0.1))
    assert ex.estrato_de(rel_bajo, PATRON) == ex.BORDE
    analizado = ex.normalizar_fila(_cruda(3, rel=None, estado="ANALIZADO", mt=1, mc=1, fa=1))
    assert ex.estrato_de(analizado, PATRON) == ex.RELEVANTE_ALTO
    servicio = ex.normalizar_fila(_cruda(4, unspsc=["81112200"]))
    assert ex.senal_ti(servicio, None) == "unspsc:81112200"
    assert ex.senal_ti(ex.normalizar_fila(_cruda(5, titulo="Licencia de antivirus")), PATRON) == "palabra:licencia"
    # Palabra parcial no cuenta: «servidores» ≠ «servidor» con límites de palabra, «licenciatura» tampoco.
    assert ex.senal_ti(ex.normalizar_fila(_cruda(6, titulo="Beca de licenciatura")), PATRON) == ""


def test_palabras_clave_del_perfil_cr_sin_ambiguas():
    perfil = ex.cargar_perfil("multicomputos_cr", ex.RAIZ / "clients")
    palabras = ex.palabras_clave(perfil)
    assert "firewall" in palabras and "oracle" in palabras
    assert "san" not in palabras and "red" not in palabras  # «San José», «red vial»
    patron = ex.compilar_patron(palabras)
    assert ex.senal_ti({"titulo": "Mantenimiento de calles en San José"}, patron) == ""


def test_ciego_no_filtra_el_veredicto():
    muestra, _ = ex.muestrear(poblacion(), ex.OBJETIVOS_DEFAULT, 3, PATRON)
    prohibidas = {"estrato", "peso", "match", "es_relevante", "es_relevante_amalia", "triaje", "estado",
                  "categoria_detectada", "marcas_amalia", "razon", "senal_ti", "competibilidad_codigo", "tags"}
    assert not prohibidas & set(ex.COLUMNAS_CIEGO)
    for f in muestra:
        ciega = ex.fila_ciega(f)
        assert tuple(ciega) == ex.COLUMNAS_CIEGO
        assert all(ciega[c] == "" for c in ex.COLUMNAS_ETIQUETA)
        valores = " ".join(str(v) for v in ciega.values())
        assert "Ciberseguridad" not in valores and "Fortinet" not in valores
        assert "confirmada" not in valores and f["estrato"] not in valores


def test_leer_poblacion_solo_lectura():
    class Cursor:
        def __init__(self):
            self.sql = []

        def execute(self, sql, binds=None):
            self.sql.append((sql, binds))

        def fetchall(self):
            cols = ex._COLUMNAS_SQL
            cruda = _cruda(1, rel=1, mt=1, mc=1, fa=1)
            return [tuple(cruda[c] for c in cols)]

        def close(self):
            pass

    cursor = Cursor()
    conn = type("C", (), {"cursor": lambda self: cursor})()
    filas = ex.leer_poblacion(conn, "multicomputos_cr", "sicop_cr", hasta=datetime(2026, 11, 22).date())
    assert cursor.sql[0][0] == "SET TRANSACTION READ ONLY"
    sql, binds = cursor.sql[1]
    assert binds["cliente_id"] == "multicomputos_cr" and binds["portal_id"] == "sicop_cr"
    assert binds["hasta"] == datetime(2026, 11, 23)
    for verbo in ("INSERT", "UPDATE", "DELETE", "MERGE"):
        assert verbo not in sql.upper()
    assert filas[0]["relevante"] and filas[0]["match"] == 100 and filas[0]["moneda"] == "CRC"
    assert filas[0]["modalidad_codigo"] == "LD"


def test_marcas_sin_veredicto_son_por_confirmar():
    f = ex.normalizar_fila(_cruda(1, marcas=[{"marca": "Oracle"}, "HPE", {"marca": ""}]))
    assert f["marcas"] == [{"marca": "Oracle", "veredicto": "por_confirmar", "score": None},
                           {"marca": "HPE", "veredicto": "por_confirmar", "score": None}]


def test_historial_enriquece_unspsc(tmp_path):
    ruta = tmp_path / "multicomputos_cr.jsonl"
    vieja = {"procesos": [{"numero_referencia": "2026LD-000001-0001", "datos_portal": {"unspsc": ["50000000"]}}]}
    nueva = {"procesos": [{"numero_referencia": "2026LD-000001-0001", "detalle": "Portátiles",
                           "modalidad_codigo": "px", "datos_portal": {"unspsc": ["43211503"]}}]}
    ruta.write_text(json.dumps(vieja) + "\n\n" + json.dumps(nueva) + "\n", encoding="utf-8")
    filas = [ex.normalizar_fila(_cruda(1)), ex.normalizar_fila(_cruda(2))]
    assert ex.enriquecer(filas, ex.leer_historial(ruta)) == 1
    assert filas[0]["unspsc"] == ["43211503"] and filas[0]["detalle"] == "Portátiles"
    assert filas[0]["modalidad_codigo"] == "PX"
    ruta.write_text("{roto\n", encoding="utf-8")
    with pytest.raises(ValueError, match="línea JSON inválida"):
        ex.leer_historial(ruta)


def test_main_escribe_tres_csv(tmp_path):
    crudas = [_cruda(i, rel=1, mt=0.9, mc=0.9, fa=0.9) for i in range(10)] + [_cruda(i) for i in range(10, 30)]

    class Cursor:
        def execute(self, sql, binds=None):
            pass

        def fetchall(self):
            return [tuple(c[k] for k in ex._COLUMNAS_SQL) for c in crudas]

        def close(self):
            pass

    @contextlib.contextmanager
    def conectar():
        yield type("C", (), {"cursor": lambda self: Cursor()})()

    salida = tmp_path / "cr"
    mensajes = []
    assert ex.main(["--salida", str(salida), "--n-relevantes", "5"], conectar=conectar,
                   decir=mensajes.append) == 0
    with open(salida / "muestra_ciega.csv", encoding="utf-8-sig") as f:
        ciego = list(csv.DictReader(f))
    with open(salida / "clave_veredictos.csv", encoding="utf-8-sig") as f:
        clave = list(csv.DictReader(f))
    with open(salida / "modalidades.csv", encoding="utf-8-sig") as f:
        modalidades = list(csv.DictReader(f))
    assert len(ciego) == len(clave) == 25
    assert tuple(ciego[0]) == ex.COLUMNAS_CIEGO and tuple(clave[0]) == ex.COLUMNAS_CLAVE
    assert {c["id"] for c in ciego} == {c["id"] for c in clave}
    assert {"PE", "CD", "CE", "PX", "PP", "RE"} <= {m["codigo"] for m in modalidades}
    assert "competib" not in " ".join(modalidades[0]).lower()  # la hoja del equipo también es ciega
    assert any("menos de 150" in m for m in mensajes)
    assert json.loads((salida / "muestra_resumen.json").read_text())["total"] == 25
