"""Histórico sintético; ninguna prueba abre conexiones ni llama modelos."""
from contextlib import nullcontext
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.aprendizaje import recalcular as r
from src.aprendizaje.__main__ import main


def fila(titulo="Renovación licenciamiento de productos Oracle", **kwargs):
    return dict(proceso_gid="sintetico:001", titulo=titulo,
                marcas=[dict(marca="Oracle", score=0.99, evidencia="inventada", role="Recomendada")],
                **kwargs)


@pytest.mark.parametrize(("titulo", "nivel"), [
    ("Renovación y soporte de licencias COGNOS", "fuera"),
    ("Renovación licenciamiento de productos Oracle", "confirmada"),
    ("Sun V880", "confirmada"),
    ("Sistema de nóminas", "por_confirmar"),
    ("Migración fuera de Oracle", "por_confirmar"),
])
def test_determinista(titulo, nivel):
    # Sun pertenece a EQUIVALENCIAS del veredicto: confirma sin diccionario activo.
    # Nóminas conserva la duda porque la fila trae score/evidencia v2 (regla A).
    assert r.calcular(fila(titulo), [])["oracle"].nivel == nivel


def preparar(monkeypatch, filas):
    monkeypatch.setattr(r, "leer_procesos", lambda *_: filas)
    monkeypatch.setattr(r.repo, "listar_terminos_activos", lambda *_: [])
    escribir = MagicMock()
    monkeypatch.setattr(r.memoria_repo, "actualizar_marcas_v2", escribir)
    return escribir


def test_dry_run_y_aplicacion_idempotente(monkeypatch):
    p = fila()
    vieja = deepcopy(p)
    fija = dict(marca="IBM", veredicto="fuera", evidencia="conservar", otro=42)
    p["marcas"].append(fija)
    escribir = preparar(monkeypatch, [p])
    conn = MagicMock()
    args = SimpleNamespace(cliente="sintetico", aplicar=False)
    r.ejecutar(conn, args, lambda _: None)
    escribir.assert_not_called()
    conn.commit.assert_not_called()
    assert p["marcas"][0] == vieja["marcas"][0]
    args.aplicar = True
    r.ejecutar(conn, args, lambda _: None)
    nuevas = escribir.call_args.args[3]
    assert nuevas[0] == dict(vieja["marcas"][0], veredicto="confirmada",
                             por_que="Término fuerte de la marca en el título",
                             evidencia=p["titulo"], origen="diccionario")
    assert nuevas[1] == fija
    p["marcas"] = nuevas
    escribir.reset_mock()
    conn.reset_mock()
    r.ejecutar(conn, args, lambda _: None)
    escribir.assert_not_called()
    conn.commit.assert_not_called()


def test_fuera_no_se_borra_y_lotes(monkeypatch):
    escribir = preparar(monkeypatch, [fila("Licencias COGNOS"), fila()])
    conn = MagicMock()
    r.ejecutar(conn, SimpleNamespace(cliente="sintetico", aplicar=True), lambda _: None, lote=1)
    assert escribir.call_args_list[0].args[3][0]["veredicto"] == "fuera"
    assert conn.commit.call_count == 2


def test_cli_y_adb_caida(monkeypatch, capsys):
    escribir = preparar(monkeypatch, [fila()])
    assert main(["recalcular-veredicto", "--aplicar"], conectar=lambda: nullcontext(MagicMock())) == 0
    escribir.assert_called_once()

    def caida():
        raise OSError("DSN inválido: detalle privado")
    assert main(["recalcular-veredicto"], conectar=caida) == 1
    assert "detalle privado" not in capsys.readouterr().err


def test_lectura_todo_historico_y_clob():
    conn = MagicMock()
    conn.cursor.return_value.fetchall.return_value = [
        ("portal:001", "Título sintético", '[{"marca":"Oracle"}]', "razón", "", 1, "NUEVO", None)]
    filas = r.leer_procesos(conn, "sintetico")
    assert filas[0]["es_relevante"] is True
    sql, binds = conn.cursor.return_value.execute.call_args.args
    assert "FETCH FIRST" not in sql
    assert binds == {"cliente_id": "sintetico"}
    conn.cursor.return_value.close.assert_called_once()


def test_salida_recortada_sin_contactos(monkeypatch):
    preparar(monkeypatch, [fila("COGNOS contacto@example.com " + "a" * 100)])
    salida = []
    r.ejecutar(MagicMock(), SimpleNamespace(cliente="sintetico", aplicar=False), salida.append)
    cambios = [s for s in salida if "→" in s]
    assert len(cambios[0].split(" | ")[0]) == 70
    assert "@" not in cambios[0]


def test_termino_activo_adicional_y_rechazado():
    p = fila("Renovación producto sintético")
    termino = dict(marca_id="oracle", termino="producto sintético", tipo="fuerte", estado="activo")
    assert r.calcular(p, [termino])["oracle"].nivel == "confirmada"
    assert r.calcular(p, [dict(termino, estado="rechazado")])["oracle"].nivel == "por_confirmar"


def test_analisis_guardado_deriva_competibilidad():
    p = fila("Renovación Oracle", analisis={
        "estado": "ANALIZADO", "matriz": {"items": [
            {"categoria": "legal", "descripcion": "Requiere marca específica IBM y carta de autorización del fabricante",
             "severidad": "ALTO", "clausula_ref": "1"}]},
        "marca_exigida": {"marca": "IBM", "cita": "Requiere marca específica IBM y carta de autorización del fabricante"},
        "proveedor_unico": True, "carta_fabricante": True})
    v = r.calcular(p, [])["oracle"]
    assert v.nivel == "no_competible"
    assert v.evidencia


def derivada(**campos):
    return dict(dict(marca="Oracle", origen="diccionario", veredicto="por_confirmar",
                     por_que="Razón anterior", evidencia=""), **campos)


def test_forzar_derivados_transicion_dry_run_e_idempotencia(monkeypatch):
    p = fila("Sistema empresarial", razon="Oracle es líder en software empresarial")
    p["marcas"] = [derivada()]
    original = deepcopy(p)
    escribir = preparar(monkeypatch, [p])
    conn = MagicMock()
    args = SimpleNamespace(cliente="sintetico", aplicar=False, forzar_derivados=True)
    salida = []
    resultado = r.ejecutar(conn, args, salida.append)
    assert any("por_confirmar → fuera" in s for s in salida)
    assert any("1 marcas se reescribirían" in s for s in salida)
    assert sum(resultado["conteos"].values()) == 1
    assert p == original
    escribir.assert_not_called()
    conn.commit.assert_not_called()
    args.aplicar = True
    r.ejecutar(conn, args, lambda _: None)
    p["marcas"] = escribir.call_args.args[3]
    assert p["marcas"][0]["veredicto"] == "fuera"
    escribir.reset_mock()
    conn.reset_mock()
    resultado = r.ejecutar(conn, args, lambda _: None)
    assert not resultado["conteos"]
    escribir.assert_not_called()
    conn.commit.assert_not_called()


@pytest.mark.parametrize("campos", [
    {"origen": "llm", "evidencia_valida": True, "evidencia": "Cita sintética"},
    {"origen": "pliego"}, {"origen": "humano"}, {"origen": "verificacion"},
    {"score": 0}, {"evidencia_valida": True}, {"evidencia": "Cita sintética"},
    {"fuente": "pliego"}, {"fuente": "v2"}, {"fuente": "humana"},
    {"fuente_texto": "pliego"},
])
def test_forzar_conserva_otras_capas(monkeypatch, campos):
    p = fila("Sistema empresarial")
    p["marcas"] = [derivada(**campos), derivada()]
    escribir = preparar(monkeypatch, [p])
    r.ejecutar(MagicMock(), SimpleNamespace(cliente="sintetico", aplicar=True,
                                          forzar_derivados=True), lambda _: None)
    assert escribir.call_args.args[3][0] == p["marcas"][0]


def test_sin_flag_no_recalcula_derivados(monkeypatch):
    p = fila()
    p["marcas"] = [derivada()]
    escribir = preparar(monkeypatch, [p])
    r.ejecutar(MagicMock(), SimpleNamespace(cliente="sintetico", aplicar=True), lambda _: None)
    escribir.assert_not_called()


def test_cli_forzar_y_help(monkeypatch, capsys):
    p = fila("Sistema empresarial")
    p["marcas"] = [derivada()]
    escribir = preparar(monkeypatch, [p])
    assert main(["recalcular-veredicto", "--forzar-derivados"],
                conectar=lambda: nullcontext(MagicMock())) == 0
    escribir.assert_not_called()
    assert "por_confirmar → fuera" in capsys.readouterr().out
    with pytest.raises(SystemExit) as exc:
        main(["recalcular-veredicto", "--help"])
    assert exc.value.code == 0
    assert "--forzar-derivados" in capsys.readouterr().out


def test_forzar_actualiza_solo_motivo(monkeypatch):
    p = fila("Sistema empresarial")
    p["marcas"] = [derivada(veredicto="fuera")]
    escribir = preparar(monkeypatch, [p])
    r.ejecutar(MagicMock(), SimpleNamespace(cliente="sintetico", aplicar=True,
                                          forzar_derivados=True), lambda _: None)
    nueva = escribir.call_args.args[3][0]
    assert nueva["veredicto"] == "fuera"
    assert nueva["por_que"] != p["marcas"][0]["por_que"]
