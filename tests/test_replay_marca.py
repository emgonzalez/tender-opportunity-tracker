"""Replay sintético: solo lectura y presupuesto antes de cada llamada."""
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from scripts import replay_marca as replay
from src import llm
from src.classifier import engine, rules
from src.drilldown import quota
from src.notifier.veredicto_marca import Veredicto


@pytest.fixture
def filas():
    return [dict(proceso_gid="sintetico:1", titulo="Licencias Oracle", marcas=[{"marca": "Oracle"}],
                 razon="", analisis=None, es_relevante=True)]


def test_etapa_a_metricas_preliminares(monkeypatch, filas):
    monkeypatch.setattr(replay.recalcular, "calcular", lambda *a, **k: {
        "oracle": Veredicto("confirmada", "título", "Oracle", "diccionario", None)})
    monkeypatch.setattr(replay.recalcular, "legado", lambda *a: True)
    ejemplos = [dict(proceso_gid="sintetico:1", marca_id="oracle", etiqueta="positivo"),
                dict(proceso_gid="ausente", marca_id="oracle", etiqueta="negativo")]
    resultado = replay.etapa_a(filas, [], ejemplos)
    assert resultado["metricas"]["oracle"]["nueva"]["matriz"]["tp"] == 1
    assert resultado["ejemplos_omitidos"] == 1
    assert "BORRADORES" in resultado["etiquetas"]


def test_sin_llm_no_red_ni_escrituras(monkeypatch, filas, tmp_path):
    sql = []

    class Cursor:
        def execute(self, sentencia):
            sql.append(sentencia)

        def close(self):
            pass

    @contextmanager
    def conectar():
        yield SimpleNamespace(cursor=Cursor)

    monkeypatch.setattr(replay.recalcular, "leer_procesos", lambda *a: filas)
    monkeypatch.setattr(replay.repo, "listar_terminos_activos", lambda *a: [])
    monkeypatch.setattr(replay, "leer_borradores", lambda *a: [])
    monkeypatch.setattr(llm, "completar", lambda *a: pytest.fail("No debe usar red"))
    ruta = tmp_path / "resultado.json"
    assert replay.main(["--json", str(ruta)], conectar=conectar, decir=lambda x: None) == 0
    assert ruta.exists()
    assert sql == ["SET TRANSACTION READ ONLY"]


def test_adb_caida_sin_traza():
    @contextmanager
    def caida():
        raise ConnectionError("DSN inválido sintético")
        yield

    mensajes = []
    assert replay.main([], conectar=caida, decir=mensajes.append) == 1
    assert "DSN" not in "".join(mensajes)


@pytest.mark.parametrize("presupuesto,llamadas", [(0, 0), (0.49, 0), (0.50, 1), (1, 2)])
def test_llm_reserva_dura(monkeypatch, filas, presupuesto, llamadas):
    monkeypatch.setattr(engine, "_batch_size", lambda cfg: 1)
    monkeypatch.setattr(llm, "modelo_de", lambda *a: "modelo-sintetico")
    monkeypatch.setattr(llm, "soporta_esquema", lambda *a: True)
    monkeypatch.setattr(llm, "temperatura_configurada", lambda *a: 0)
    monkeypatch.setattr(rules, "get_system_prompt_v2", lambda *a: "sistema")
    monkeypatch.setattr(rules, "get_user_prompt_v2", lambda *a: "usuario")
    monkeypatch.setattr(quota, "costo_usd", lambda *a: 0.50)
    pedidos = []

    def completar(cfg, pedido):
        assert cfg.llm_registro_habilitado is False
        pedidos.append(pedido)
        return SimpleNamespace(texto="{}", uso=SimpleNamespace(input_tokens=10, output_tokens=10))

    monkeypatch.setattr(llm, "completar", completar)
    monkeypatch.setattr(engine, "_parsear_v2", lambda *a: [SimpleNamespace(
        es_relevante=False, marcas_recomendadas=[])])
    resultado = replay.etapa_b([dict(filas[0], proceso_gid=f"sintetico:{i}") for i in range(3)], [], object(), object(), presupuesto, parcial=True)
    assert len(pedidos) == llamadas
    assert resultado["presupuesto"]["gastado"] <= presupuesto
    assert resultado["comparados"] == llamadas
    if pedidos:
        assert pedidos[0].esquema
        assert pedidos[0].temperatura == 0


def test_llm_fallo_no_libera_reserva(monkeypatch, filas):
    monkeypatch.setattr(engine, "_batch_size", lambda cfg: 1)
    monkeypatch.setattr(llm, "modelo_de", lambda *a: "modelo-sintetico")
    monkeypatch.setattr(llm, "soporta_esquema", lambda *a: False)
    monkeypatch.setattr(llm, "temperatura_configurada", lambda *a: None)
    monkeypatch.setattr(rules, "get_system_prompt_v2", lambda *a: "sistema")
    monkeypatch.setattr(rules, "get_user_prompt_v2", lambda *a: "usuario")
    monkeypatch.setattr(quota, "costo_usd", lambda *a: 0.50)
    intentos = []

    def fallo(*args):
        intentos.append(1)
        raise RuntimeError("fallo sintético")

    monkeypatch.setattr(llm, "completar", fallo)
    resultado = replay.etapa_b([dict(filas[0], proceso_gid=f"sintetico:{i}") for i in range(3)], [], object(), object(), 0.50, parcial=True)
    assert len(intentos) == 1
    assert resultado["errores"] == 1
    assert resultado["comparados"] == 0


@pytest.fixture
def llm_falso(monkeypatch):
    monkeypatch.setattr(engine, "_batch_size", lambda cfg: 1)
    monkeypatch.setattr(llm, "modelo_de", lambda *a: "modelo-sintetico")
    monkeypatch.setattr(llm, "soporta_esquema", lambda *a: False)
    monkeypatch.setattr(llm, "temperatura_configurada", lambda *a: None)
    monkeypatch.setattr(rules, "get_system_prompt_v2", lambda *a: "sistema")
    monkeypatch.setattr(rules, "get_user_prompt_v2", lambda *a: "usuario")
    monkeypatch.setattr(quota, "costo_usd", lambda modelo, entrada, salida: 0.25 if entrada == 10 else 0.50)
    llamadas = []

    def completar(*args):
        llamadas.append(1)
        return SimpleNamespace(texto="{}", uso=SimpleNamespace(input_tokens=10, output_tokens=10))

    monkeypatch.setattr(llm, "completar", completar)
    monkeypatch.setattr(engine, "_parsear_v2", lambda *a: [SimpleNamespace(
        es_relevante=False, marcas_recomendadas=[])])
    return llamadas


def test_prevuelo_corte_y_reanudacion(llm_falso, filas, tmp_path, capsys):
    import json

    filas = [dict(filas[0], proceso_gid=f"sintetico:{i}",
                  titulo="Consulta contacto@example.com") for i in range(3)]
    ruta = tmp_path / "checkpoint.json"
    mensajes = []
    def ejecutar(tope, **kw):
        return replay.etapa_b(filas, [], object(), object(), tope,
                              decir=mensajes.append, checkpoint=ruta, **kw)
    assert ejecutar(1, estimar=True)["presupuesto"]["procesados"] == 0
    assert not llm_falso
    assert not ruta.exists()
    assert ejecutar(1)["presupuesto"]["procesados"] == 0
    assert not llm_falso
    primero = ejecutar(0.50, parcial=True)["presupuesto"]
    assert primero["gastado"] == 0.25
    assert primero["pendientes"] == 2
    assert not primero["completo"]
    estado = json.loads(ruta.read_text())
    assert "@" not in ruta.read_text()
    assert "titulo" not in ruta.read_text()
    segundo = ejecutar(1.5, estado=estado)["presupuesto"]
    assert segundo["completo"]
    assert segundo["gastado"] == 0.75
    assert len(llm_falso) == 3
    assert "Procesados 1 de 3" in capsys.readouterr().err
    assert any("REPORTE FINAL" in m and "Faltan 2 títulos" in m for m in mensajes)


@pytest.mark.parametrize("tope,parcial,codigo", [(0.5, True, 3), (2, False, 0)])
def test_main_presupuesto(llm_falso, monkeypatch, filas, tmp_path, tope, parcial, codigo):
    import json

    from src.classifier import client_profile
    from src.clientes import repo as clientes_repo

    class Cursor:
        def execute(self, *args):
            pass

        def close(self):
            pass

    @contextmanager
    def conectar():
        yield SimpleNamespace(cursor=Cursor)

    monkeypatch.setattr(replay.recalcular, "leer_procesos", lambda *a: [
        dict(filas[0], proceso_gid=f"sintetico:{i}") for i in range(3)])
    monkeypatch.setattr(replay.repo, "listar_terminos_activos", lambda *a: [])
    monkeypatch.setattr(replay, "leer_borradores", lambda *a: [])
    monkeypatch.setattr(clientes_repo, "activo", lambda *a: (1, {}))
    monkeypatch.setattr(client_profile, "_parse_profile", lambda *a: object())
    ruta = tmp_path / "salida.json"
    args = ["--llm", "--presupuesto-usd", str(tope), "--json", str(ruta),
            "--checkpoint", str(tmp_path / "checkpoint.json")]
    if parcial:
        args.append("--parcial")
    assert replay.main(args, conectar=conectar, cfg=object(), decir=lambda x: None) == codigo
    assert json.loads(ruta.read_text())["presupuesto"]["completo"] == (codigo == 0)
    if codigo == 3:
        llamadas_antes = len(llm_falso)
        assert llamadas_antes == 1
        assert replay.main([
            "--reanudar", str(tmp_path / "checkpoint.json"), "--presupuesto-usd", "2",
            "--json", str(ruta)], conectar=conectar, cfg=object(), decir=lambda x: None) == 0
        assert len(llm_falso) == 3
        assert json.loads(ruta.read_text())["presupuesto"]["gastado"] == 0.75
