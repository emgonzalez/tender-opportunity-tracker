"""Las definiciones SQL de relevante/apto tienen un único hogar y no divergen del correo (E11.1)."""
import itertools

from src.analytics import pais
from src.memoria import relevancia
from src.notifier import agrupamiento


def test_pais_usa_la_constante_unica():
    assert pais._SQL_ES_RELEVANTE is relevancia.SQL_ES_RELEVANTE


def test_el_umbral_del_apto_es_el_del_correo():
    assert f">= {agrupamiento.UMBRAL_MATCH_DECIDIR}" in relevancia.SQL_ES_APTO
    assert "NO-GO" in relevancia.SQL_ES_APTO and relevancia.SQL_ES_RELEVANTE in relevancia.SQL_ES_APTO


def test_la_formula_del_match_sql_coincide_con_la_del_correo():
    """Mismos pesos que `agrupamiento.match_de_valores`: se verifica con la aritmética de la expresión SQL."""
    for t, c, f in itertools.product((0.0, 0.5, 0.8, 1.0), repeat=3):
        esperado = agrupamiento.match_de_valores(t, c, f)
        sql = relevancia.SQL_MATCH
        # sustituye las columnas por los valores y evalúa con Python (los pesos son literales numéricos)
        expr = (sql.replace("NVL(cp.match_tecnico, 0)", str(t)).replace("NVL(cp.match_categoria, 0)", str(c))
                .replace("NVL(cp.factibilidad, 0)", str(f)))
        assert round(eval(expr)) == esperado, (t, c, f)  # noqa: S307 — expresión propia, sin entrada externa
