"""P10: el portal consume el veredicto persistido y conserva E14."""

import pytest
from jinja2 import Environment, FileSystemLoader, select_autoescape

from src.analytics import tablero
from src.memoria import relevancia


@pytest.fixture(autouse=True)
def _env_minimo(monkeypatch):
    for var in ("COMPRASAL_EMAIL", "COMPRASAL_PASSWORD", "SMTP_USER", "SMTP_PASSWORD"):
        monkeypatch.setenv(var, "x")


def test_off_y_sombra_conservan_sql_legado():
    off = relevancia.sql_marca({"oracle"}, modo="off")
    assert off == relevancia.sql_marca({"oracle"}, modo="sombra")
    assert "pm.titulo" in off and "$.veredicto" not in off


def test_activo_solo_mira_veredicto_y_aliases(monkeypatch):
    monkeypatch.setenv("MARCA_VEREDICTO", "activo")
    sql = relevancia.sql_marca({"oracle"}, "x")
    assert "JSON_SERIALIZE(x.marcas RETURNING CLOB)" in sql
    assert "v VARCHAR2(40) PATH '$.veredicto'" in sql
    assert "'confirmada', 'por_confirmar', 'no_competible'" in sql
    assert "jm.v IS NULL" in sql
    assert "pm.titulo" not in sql and "fuera" not in sql
    for alias in ("netsuite", "mysql", "java", "weblogic", "solaris", "sun", "peoplesoft"):
        assert alias in sql
        assert relevancia.es_de_marca(alias, {"oracle"}, modo="activo")
        assert not relevancia.es_de_marca(alias + "xyz", {"oracle"}, modo="activo")


@pytest.mark.parametrize("nivel", ["confirmada", "por_confirmar", "no_competible", None, "fuera"])
def test_chips_no_muestran_marcas_ajenas_y_fuera(nivel):
    filas = [{"marca": "Oracle", "veredicto": nivel, "por_que": "Motivo"},
             {"marca": "IBM", "veredicto": "confirmada"}]
    marcas = tablero._marcas_veredicto(filas, {"oracle"})
    assert len(marcas) == (0 if nivel == "fuera" else 1)
    if marcas:
        assert marcas[0]["veredicto"] == (nivel or "por_confirmar")
        assert marcas[0]["marca"] == "Oracle"


def test_plantilla_escapa_citas_y_muestra_veredicto():
    env = Environment(loader=FileSystemLoader("templates"), autoescape=select_autoescape(["html"]))
    tarjeta = {"gid": "sintetico", "match": 86, "marca_veredictos": [
        {"marca": "Oracle", "veredicto": "por_confirmar", "por_que": '<script>"x"</script>',
         "evidencia": "<img>", "origen": "pliego"}]}
    html = env.get_template("portal_pais_tablero.html").render(
        tablero={"columnas": [{"clave": "nuevo", "tarjetas": [tarjeta]}]})
    assert "Oracle: por confirmar" in html and "Match del proceso 86" in html
    assert "<script>" not in html and "&lt;script&gt;" in html
    assert "<img>" not in html


def test_el_modo_se_lee_del_entorno_sin_construir_config(monkeypatch):
    """Regresión del CI: `sql_marca` se importa y se usa donde no hay credenciales de COMPRASAL; leer el flag no debe
    exigir `Config()` (KeyError: 'COMPRASAL_EMAIL')."""
    for var in ("COMPRASAL_EMAIL", "COMPRASAL_PASSWORD", "SMTP_USER", "SMTP_PASSWORD"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.delenv("MARCA_VEREDICTO", raising=False)
    assert relevancia.modo_veredicto() == "off"
    assert "pm.titulo" in relevancia.sql_marca({"oracle"}, "x")          # sin variable ⇒ regla legada
    for valor, esperado in (("activo", "activo"), ("SOMBRA", "sombra"), ("raro", "off"), ("", "off")):
        monkeypatch.setenv("MARCA_VEREDICTO", valor)
        assert relevancia.modo_veredicto() == esperado
    assert relevancia.modo_veredicto("activo") == "activo"               # el parámetro explícito manda
