"""Replay sin red, navegador, proveedor LLM ni base real."""
import argparse
import json
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src import llm
from src.drilldown import replay
from src.scraper.models import ProcesoClasificado


@pytest.fixture(autouse=True)
def _env_minimo(monkeypatch):
    for nombre in ("COMPRASAL_EMAIL", "COMPRASAL_PASSWORD", "SMTP_USER", "SMTP_PASSWORD"):
        monkeypatch.setenv(nombre, "x")
    monkeypatch.setattr(replay, "ventana_prohibida", lambda: False)
    monkeypatch.setattr(replay.llm, "modelo_de", lambda cfg, etapa: "claude-haiku-4-5")


@pytest.fixture
def cfg():
    return SimpleNamespace(pliego_max_mb=1, intel_c0="off", intel_c1="sombra", llm_timeout_s=10, replay_observaciones=[])


def fila(ref="REF-1"):
    return dict(gid="portal:" + ref, referencia=ref, paginas=12, marcas=[],
        proceso=ProcesoClasificado(institucion="Institución ficticia", descripcion="Licencias",
            fecha_publicacion=datetime(2026, 10, 1), url_directa="", numero_referencia=ref),
        anterior=dict(estado="ANALIZADO", triaje="NO-GO", confianza=0,
                      parse_error=True, error_llm=False, tiene_analisis=True, competible=False))


def nuevo():
    return dict(triaje="GO", confianza=.9, estado="ANALIZADO", parse_error=False,
                competible=None, marca_exigida=True, marca_cita_valida=True,
                pagina_marca=12, paginas=[1, 12], proveedor_unico=True,
                carta_fabricante=True, equivalente_admitido=None,
                verificacion=dict(soporta="insuficiente", cita_valida=False, solo_vision=True,
                                  pagina=12, tiene_cita=True))


def args(tmp_path, **kwargs):
    valores = dict(cliente="cliente_demo", capas="c0,c2", checkpoint=tmp_path / "checkpoint.json",
                   reanudar=None, presupuesto_usd=3., sin_descarga=False,
                   forzar_ventana=False, tiempo_max_s=10)
    valores.update(kwargs)
    return argparse.Namespace(**valores)


async def descargar(procesos, cfg):
    salida = {}
    for proceso in procesos:
        ruta = Path(cfg.drilldown_tmp_dir) / (proceso.numero_referencia + ".pdf")
        ruta.write_bytes(b"pdf falso")
        salida[proceso.numero_referencia] = str(ruta)
    return salida


def test_seleccion_fechas_y_refs():
    conn = MagicMock()
    cur = conn.cursor.return_value
    cur.fetchall.return_value = [("portal:REF-1", "REF-1", "Licencias", "Demo",
        datetime(2026, 10, 1), "", "", 1, "ANALIZADO", "GO", .9, "parse_error", 12, None, "", "[]")]
    filas = replay.seleccionar(conn, "demo", desde=datetime(2026, 10, 1),
                               hasta=datetime(2026, 10, 31), solo_relevantes=True, maximo=1)
    sql, binds = cur.execute.call_args.args
    assert "cp.es_relevante = 1" in sql
    assert binds["hasta"] == datetime(2026, 11, 1)
    assert filas[0]["anterior"]["parse_error"]
    replay.seleccionar(conn, "demo", refs=["REF-1", "REF-2"])
    sql, binds = cur.execute.call_args.args
    assert ":ref0" in sql and binds["ref1"] == "REF-2"
    assert "fecha_pub_utc >=" not in sql
    cur.close.assert_called()


def test_override_aislamiento_y_limpieza(tmp_path, cfg, monkeypatch):
    prohibida = MagicMock(side_effect=AssertionError("Escritura diaria"))
    from src import history
    from src.memoria import service
    from src.notifier import email_sender
    monkeypatch.setattr(service, "guardar_estado", prohibida)
    monkeypatch.setattr(service, "persistir_memoria", prohibida)
    monkeypatch.setattr(pipeline := replay.pipeline, "_persistir", prohibida)
    monkeypatch.setattr(pipeline, "procesar_drilldown", prohibida)
    monkeypatch.setattr(email_sender, "send_report", prohibida)
    for nombre in ("guardar_last_run", "save_analisis", "save_run"):
        if hasattr(history, nombre):
            monkeypatch.setattr(history, nombre, prohibida)
    def inferir(f, ruta, local, perfil):
        assert local.intel_c0 == local.intel_c2 == "activo"
        assert local.intel_c1 == local.intel_c3 == "off"
        assert not local.enable_memoria and not local.llm_registro_habilitado
        return nuevo()
    estado = replay.ejecutar([fila()], cfg, None, args(tmp_path), descargar=descargar, inferir=inferir, decir=lambda _: None)
    assert estado["completo"]
    assert not Path(estado["temporal"]).exists()
    assert cfg.intel_c0 == "off" and cfg.intel_c1 == "sombra"
    prohibida.assert_not_called()


def test_presupuesto_reanuda_sin_repagar(tmp_path, cfg, monkeypatch):
    completar = MagicMock(return_value=llm.Respuesta("{}", llm.Uso(1, 1), "claude-haiku-4-5", "fake"))
    monkeypatch.setattr(llm, "completar", completar)
    def inferir(f, ruta, local, perfil):
        llm.completar(local, llm.Pedido(etapa="triaje", contenido=[llm.BloqueTexto("a")]))
        llm.completar(local, llm.Pedido(etapa="analisis", contenido=[llm.BloqueTexto("b" * 10000)]))
        return nuevo()
    # Tope entre las reservas de las dos llamadas: cabe la 1.ª y la 2.ª (con más texto) ya no. No depende del valor
    # exacto de la reserva (antes fijaba US$0,011 a ojo y dependía de que se contaran los bytes como tokens).
    p1 = llm.Pedido(etapa="triaje", contenido=[llm.BloqueTexto("a")])
    p2 = llm.Pedido(etapa="analisis", contenido=[llm.BloqueTexto("b" * 10000)])
    r1, r2 = (replay.quota.costo_usd("claude-haiku-4-5", replay.tokens_de_reserva(p), p.max_tokens) for p in (p1, p2))
    assert r1 < r2
    parametros = args(tmp_path, presupuesto_usd=(r1 + r2) / 2)
    estado = replay.ejecutar([fila()], cfg, None, parametros, descargar=descargar, inferir=inferir, decir=lambda _: None)
    assert not estado["completo"] and completar.call_count == 1
    gastado = estado["gastado"]
    antiguo = json.loads(parametros.checkpoint.read_text())
    antiguo.pop("prompts", None)
    parametros.checkpoint.write_text(json.dumps(antiguo))
    parametros.reanudar = parametros.checkpoint
    parametros.sin_descarga = True
    parametros.presupuesto_usd = 3
    estado = replay.ejecutar([fila()], cfg, None, parametros, descargar=descargar, inferir=inferir, decir=lambda _: None)
    assert completar.call_count == 2 and estado["completo"]
    assert estado["gastado"] == pytest.approx(gastado * 2)
    assert not Path(estado["temporal"]).exists()


def test_reserva_persistida_ante_fallo(tmp_path, cfg):
    raiz = tmp_path / "privado"
    raiz.mkdir()
    estado = dict(llamadas={}, gastado=0, forzar_ventana=True)
    completar = MagicMock(side_effect=RuntimeError("fake"))
    guardia = replay.Guardia(estado, tmp_path / "checkpoint", raiz, 3, completar)
    pedido = llm.Pedido("triaje", [llm.BloqueTexto("demo")])
    with pytest.raises(RuntimeError):
        guardia(cfg, pedido)
    assert json.loads((tmp_path / "checkpoint").read_text())["gastado"] > 0
    with pytest.raises(replay.Detener):
        guardia(cfg, pedido)
    assert completar.call_count == 1


def test_metricas_de_acuerdo():
    primero = dict(anterior=fila()["anterior"], nuevo=nuevo())
    segundo = dict(anterior=dict(fila()["anterior"], triaje="GO", parse_error=False, competible=True),
                   nuevo=dict(nuevo(), competible=True, estado="ERROR_LLM", parse_error=True))
    tercero = dict(anterior=dict(fila()["anterior"], triaje="NO-GO", parse_error=False),
                   nuevo=dict(nuevo(), triaje="NO-GO", competible=False))
    m = replay.metricas({"1": primero, "2": segundo, "3": tercero, "4": {"no_disponible": True}})
    assert m["comparados"] == 3 and m["no_go_parse_corregidos"] == 1
    assert m["triaje_igual"] == 2 and m["triaje_diferente"] == 1
    assert m["false_a_none"] == 1 and m["competible_igual"] == 2
    assert m["tasa_reparacion"] == 1 and m["error_llm"] == m["parse_error"] == 1
    assert m["marca_en_selector"] == m["marca_despues_10"] == 3
    assert m["tasa_citas_invalidas"] == .5


def test_no_disponible_y_fallo_no_detienen(tmp_path, cfg):
    async def parcial(procesos, local):
        return await descargar(procesos[:1], local)
    def falla(*_):
        raise ValueError("fallo ficticio")
    estado = replay.ejecutar([fila(), fila("REF-2")], cfg, None, args(tmp_path),
                             descargar=parcial, inferir=falla, decir=lambda _: None)
    assert estado["completo"]
    assert estado["resultados"]["REF-1"]["error"]
    assert estado["resultados"]["REF-2"]["no_disponible"]


@pytest.mark.parametrize("hh,mm,esperado", [(6, 44, False), (6, 45, True), (7, 29, True), (7, 30, False)])
def test_ventana(hh, mm, esperado, monkeypatch):
    # Recupera la función real cubierta por el fixture global.
    from importlib import util
    spec = util.spec_from_file_location("replay_ventana", replay.__file__)
    modulo = util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    ahora = datetime(2026, 10, 1, hh + 6, mm, tzinfo=timezone.utc)
    assert modulo.ventana_prohibida(ahora) is esperado


def test_estimar_y_default_sin_llm(tmp_path, cfg, monkeypatch):
    conn = MagicMock()
    @contextmanager
    def conectar():
        yield conn
    monkeypatch.setattr(replay, "seleccionar", lambda *a, **kw: [fila()])
    monkeypatch.setattr(llm, "completar", MagicMock(side_effect=AssertionError("LLM")))
    salida = tmp_path / "salida.json"
    mensajes = []
    assert replay.main(["--cliente", "cliente_demo", "--refs", "REF-1", "--estimar", "--json", str(salida)],
                       conectar=conectar, cfg=cfg, decir=mensajes.append) == 0
    assert json.loads(salida.read_text())["estimacion"]["analisis_previos"] == 1
    assert any("estimado conservador" in m and "2,3" in m for m in mensajes)
    conn.cursor.return_value.execute.assert_called_once_with("SET TRANSACTION READ ONLY")
    from src.clientes import repo
    monkeypatch.setattr(repo, "activo", lambda *a: (1, {}))
    from src.classifier import client_profile
    monkeypatch.setattr(client_profile, "_parse_profile", lambda *a: None)
    def ejecutar(filas, cfg, perfil, args, **kw):
        assert args.presupuesto_usd == 3.00
        return dict(resultados={}, gastado=0, completo=False, metricas={}, medias_por_capa={})
    monkeypatch.setattr(replay, "ejecutar", ejecutar)
    assert replay.main(["--cliente", "cliente_demo", "--refs", "REF-1"], conectar=conectar,
                       cfg=cfg, decir=lambda _: None) == 3


def test_validaciones_y_ventana_no_conectan(cfg, monkeypatch):
    conectar = MagicMock(side_effect=AssertionError("ADB"))
    for opciones in (["--presupuesto-usd", "nan"], ["--max", "101"], ["--capas", "c9"]):
        with pytest.raises(SystemExit):
            replay.main(["--cliente", "demo", "--refs", "R", *opciones], conectar=conectar, cfg=cfg)
    monkeypatch.setattr(replay, "ventana_prohibida", lambda: True)
    assert replay.main(["--cliente", "demo", "--refs", "R"], conectar=conectar, cfg=cfg, decir=lambda _: None) == 2
    conectar.assert_not_called()


def test_limpieza_no_sigue_enlaces(tmp_path):
    propio = tmp_path / "propio"
    propio.mkdir()
    ajeno = tmp_path / "ajeno"
    ajeno.write_text("conservar")
    (propio / "enlace").symlink_to(ajeno)
    replay.limpiar(propio)
    assert ajeno.read_text() == "conservar"


@pytest.mark.parametrize("escaso", [False, True])
def test_analisis_real_orquestacion_con_capas_falsas(tmp_path, cfg, monkeypatch, escaso):
    """Ejercita la función real aislada, sin reemplazarla por el fake del lote."""
    import fitz

    from src.scraper.models import ResultadoAnalisis
    ruta = tmp_path / "pliego.pdf"
    documento = fitz.open()
    cita = "Se exige licencia de la marca Ejemplo para este contrato"
    for n in range(12):
        pagina = documento.new_page()
        if not escaso or n < 3 or n == 11:
            pagina.insert_text((50, 50), cita)
    documento.save(ruta)
    documento.close()
    local = replay.ConfigReplay(cfg, ["c0", "c1", "c2", "c3"], tmp_path)
    monkeypatch.setattr(replay.pipeline, "contexto_cliente", lambda _: "Portafolio ficticio")
    monkeypatch.setattr(replay.pipeline, "marcas_del_portafolio", lambda _: ("Ejemplo",))
    def triaje(*a, **kw):
        assert "<proceso>" in kw["contexto_proceso"]
        return replay.triage.ResultadoTriaje("GO", .8, "INCIERTO: falta confirmar", None, {})
    monkeypatch.setattr(replay.triage, "triage", triaje)
    monkeypatch.setattr(replay.lectura, "seleccionar_paginas", lambda *a, **kw: [1, 12])
    monkeypatch.setattr(replay.analysis, "analizar_pliego", lambda *a, **kw: ResultadoAnalisis(
        proceso_uuid="REF-1", estado="ANALIZADO", marca_exigida=dict(marca="Ejemplo", cita=cita, pagina=12)))
    monkeypatch.setattr(replay.analysis, "aplicar_competibilidad", lambda r, **kw: setattr(r, "competible", False))
    monkeypatch.setattr(replay.verificacion, "verificar_afirmacion", lambda *a, **kw:
        replay.verificacion.Verificacion(soporta="insuficiente", cita=cita, pagina=12, solo_vision=True))
    salida = replay.analizar(fila(), str(ruta), local, None)
    assert salida["triaje"] == "INCIERTO"
    assert salida["marca_cita_valida"] and salida["pagina_marca"] == 12
    assert salida["competible"] is None
    assert salida["verificacion"]["cita_valida"]
    assert salida["diagnostico"]["triaje_motivo"] == "falta confirmar"
    assert salida["diagnostico"]["formato"] == ("NATIVO_ESCASO" if escaso else "NATIVO")
    assert salida["diagnostico"]["paginas_sin_texto"] == (7 if escaso else 0)
    assert salida["diagnostico"]["n_paginas"] == 10
    assert salida["diagnostico"]["caracteres_texto"] > 0


def test_decision_solo_variante_replay_y_adb_caida(tmp_path, cfg, monkeypatch):
    from src.aprendizaje import repo
    monkeypatch.setattr(repo, "VARIANTES", frozenset({"activa", "sombra", "replay"}))
    guardar = MagicMock()
    monkeypatch.setattr(repo, "guardar_decision", guardar)
    conn = MagicMock()
    @contextmanager
    def conectar():
        yield conn
    replay.ejecutar([fila()], cfg, None, args(tmp_path), descargar=descargar,
        inferir=lambda *a: nuevo(), conectar_decisiones=conectar, decir=lambda _: None)
    assert guardar.call_count == 2
    assert {c.args[3] for c in guardar.call_args_list} == {"C0", "C2"}
    assert all(c.kwargs["variante"] == "replay" for c in guardar.call_args_list)
    @contextmanager
    def caida():
        # DSN ficticio: nunca se intenta conectar ni se usan claves erróneas.
        raise ConnectionError("DSN inválido de prueba")
        yield
    estado = replay.ejecutar([fila()], cfg, None, args(tmp_path), descargar=descargar,
        inferir=lambda *a: nuevo(), conectar_decisiones=caida, decir=lambda _: None)
    assert estado["completo"] and estado["fallos_decision"] == 2
    assert estado["resultados"]["REF-1"]["nuevo"] == nuevo()


def test_limite_tiempo_interrumpe():
    with pytest.raises(replay.Detener, match="tiempo"):
        with replay.limite_tiempo(.01):
            while True:
                pass


def test_firma_incompatible_no_repite_llm(tmp_path, cfg, monkeypatch):
    def parar(*_):
        raise replay.Detener("presupuesto")
    parametros = args(tmp_path)
    estado = replay.ejecutar([fila()], cfg, None, parametros, descargar=descargar,
        inferir=parar, decir=lambda _: None)
    parametros.reanudar = parametros.checkpoint
    parametros.capas = "c0"
    try:
        with pytest.raises(ValueError, match="incompatible"):
            replay.ejecutar([fila()], cfg, None, parametros, inferir=parar)
    finally:
        replay.limpiar(Path(estado["temporal"]))


def test_archivo_refs_y_reanudacion_recupera_seleccion(tmp_path, cfg, monkeypatch):
    conn = MagicMock()
    @contextmanager
    def conectar():
        yield conn
    seleccionar = MagicMock(return_value=[fila()])
    monkeypatch.setattr(replay, "seleccionar", seleccionar)
    archivo = tmp_path / "refs.txt"
    archivo.write_text("REF-1\nREF-2\n")
    assert replay.main(["--cliente", "demo", "--archivo", str(archivo), "--estimar"],
                       conectar=conectar, cfg=cfg, decir=lambda _: None) == 0
    assert seleccionar.call_args.kwargs["refs"] == ["REF-1", "REF-2"]
    checkpoint = tmp_path / "checkpoint.json"
    checkpoint.write_text(json.dumps(dict(referencias=["REF-1"], capas=["c0", "c2"])))
    assert replay.main(["--cliente", "demo", "--reanudar", str(checkpoint), "--estimar"],
                       conectar=conectar, cfg=cfg, decir=lambda _: None) == 0
    assert seleccionar.call_args.kwargs["refs"] == ["REF-1"]


def test_documento_reserva_antes_y_debug_deshabilitado(tmp_path, cfg, monkeypatch):
    ruta = tmp_path / "doc.pdf"
    ruta.write_bytes(b"x" * 100000)
    estado = dict(llamadas={}, gastado=0, forzar_ventana=True)
    completar = MagicMock()
    guardia = replay.Guardia(estado, tmp_path / "checkpoint", tmp_path, .01, completar)
    with pytest.raises(replay.Detener, match="presupuesto"):
        guardia(cfg, llm.Pedido("analisis", [llm.BloqueDocumento(str(ruta), paginas=12)]))
    completar.assert_not_called()
    assert replay.ConfigReplay(cfg, ["c0"], tmp_path).debug_dir is None


def test_citas_rechazadas_y_reparacion_json_no_se_pierden():
    cita = "Se exige licencia de la marca Ejemplo para este contrato"
    observaciones = [("triaje_c0", "{json roto"), ("triaje_c0", json.dumps(dict(
        marca_exigida=dict(marca="Ejemplo", cita=cita, pagina=12)))),
        ("analisis_marcas", json.dumps(dict(marca_exigida=dict(
            marca="Otra", cita="Esta frase inventada no figura en la página", pagina=12))))]
    resultado = replay.diagnosticar_respuestas(observaciones, {12: {"texto": cita}})
    assert {k: v for k, v in resultado.items() if k != "diagnostico"} == dict(citas_emitidas=2, citas_emitidas_invalidas=1,
                             fallos_parseo_llamadas=1, reparaciones_llm=1)
    assert len(resultado["diagnostico"]["citas"]) == 2


def test_descarga_tambien_se_detiene_antes_de_ventana():
    ahora = datetime(2026, 10, 1, 12, 44, tzinfo=timezone.utc)
    assert replay.duracion_segura(3600, ahora=ahora) == 55
    assert replay.duracion_segura(3600, forzar=True, ahora=ahora) == 3600
    assert replay.duracion_segura(3600, ahora=ahora.replace(minute=50)) == 0
    assert replay.duracion_segura(3600, ahora=ahora.replace(hour=14)) == 3600


class TestEstimacionYReservaRealistas:
    """Regresión del 2026-11-03: el pre-vuelo estimó US$40.368 para 30 pliegos porque trataba los 32 MB máximos como
    tokens y multiplicaba por 30; y la reserva por llamada contaba los BYTES del PDF (y 16.000 tokens por página),
    con lo que el repaso se habría detenido en la primera llamada por «presupuesto agotado»."""

    def test_el_preflight_de_30_procesos_es_de_dolares_no_de_miles(self, cfg, monkeypatch):
        monkeypatch.setattr(replay.llm, "modelo_de", lambda c, etapa: "meta.llama-4-scout-17b-16e-instruct")
        est = replay.estimacion([fila(f"R{i}") for i in range(30)], cfg)
        assert 0.5 < est["conservador"] < 6.0           # ≈ US$0,06 por proceso × 30, con margen
        assert est["orientativo"] == pytest.approx(est["conservador"] / 2.3)
        assert est["descargas"] == 30

    def test_reserva_de_un_pdf_grande_cuesta_centavos(self, tmp_path, monkeypatch):
        monkeypatch.setattr(replay.llm, "modelo_de", lambda c, etapa: "meta.llama-4-maverick-17b-128e-instruct-fp8")
        pdf = tmp_path / "grande.pdf"
        pdf.write_bytes(b"0" * (30 * 1024 * 1024))     # 30 MB: el viejo cálculo reservaba decenas de dólares
        pedido = llm.Pedido(etapa="analisis", sistema="s" * 2000, max_tokens=4096,
                            contenido=[llm.BloqueTexto("t" * 6000), llm.BloqueDocumento(str(pdf), paginas=56, escaneado=True)])
        tokens = replay.tokens_de_reserva(pedido)
        assert tokens <= 150_000 + 1024 + 8000 // 3 + 10   # el documento se topa; no crece con los bytes del archivo
        reserva = replay.quota.costo_usd("meta.llama-4-maverick-17b-128e-instruct-fp8", tokens, pedido.max_tokens)
        assert 0 < reserva < 0.25

    def test_las_imagenes_cuentan_por_su_costo_tipico_y_no_por_su_base64(self):
        imagen = llm.BloqueImagen(datos_b64="A" * 5_000_000)
        pedido = llm.Pedido(etapa="triaje", sistema="", max_tokens=512, contenido=[imagen] * 10)
        assert replay.tokens_de_reserva(pedido) == 1024 + 10 * replay.TOKENS_POR_IMAGEN


@pytest.mark.parametrize("pagina,textos,total,motivo", [
    (1, {1: "Se requieren licencias originales del fabricante para participar"}, 2, "texto_no_coincide"),
    (2, {1: "Texto disponible", 2: ""}, 2, "sin_texto_de_pagina"),
    (3, {1: "Texto disponible"}, 2, "pagina_inexistente"),
    (1, {1: "Texto disponible"}, 2, "texto_no_coincide"),
])
def test_diagnostico_motivos(pagina, textos, total, motivo):
    cita = "Licencias del proveedor son necesarias para poder concursar" if motivo != "cita_demasiado_corta" else "Licencias"
    d = replay.diagnosticar_cita("analisis", dict(cita=cita, pagina=pagina), textos, total)
    assert not d["valida"] and d["motivo_invalidez"] == motivo
    assert 0 <= d["similitud_pagina"] <= d["similitud"] <= 1
    assert 0 <= d["similitud_texto"] <= d["similitud"]
    assert len(d["fragmento"]) <= 120


def test_diagnostico_todas_las_emisiones_y_limites():
    cita = "Se exigen licencias originales del fabricante para poder participar " * 5
    observaciones = [(nombre, json.dumps(dict(cita=cita, pagina=1) if nombre == "verificacion_c3"
        else dict(marca_exigida=dict(cita=cita, pagina=1))))
        for nombre in ("triaje_c0", "analisis_marcas", "verificacion_c3", "verificacion_c3")]
    d = replay.diagnosticar_respuestas(observaciones, {1: cita}, n_paginas=1)["diagnostico"]
    assert [c["fuente"] for c in d["citas"]] == ["triaje", "analisis", "verificacion", "verificacion"]
    assert all(c["valida"] and len(c["cita"]) <= 160 and len(c["fragmento"]) <= 120 for c in d["citas"])
    direccion = ".".join(("192", "0", "2", "1"))
    publico = replay.recortar(f"Contacto: persona@example.com https://example.com {direccion} +503 2222 3333", 160)
    assert "@" not in publico and direccion not in publico and "2222" not in publico


def test_resumen_diagnostico_por_formato_y_motivo():
    resultados = {}
    for i, formato in enumerate(("NATIVO", "ESCANEADO", "NATIVO")):
        resultados[str(i)] = dict(anterior=fila()["anterior"], nuevo=dict(nuevo(), triaje="INCIERTO",
            diagnostico=dict(formato=formato, triaje_motivo="Falta   información" if i else "FALTA INFORMACION",
                citas=[dict(valida=False, motivo_invalidez="texto_no_coincide", similitud=i / 4)])))
    d = replay.metricas(resultados)["diagnostico"]
    assert d["invalidas_por_motivo"] == {"texto_no_coincide": 3}
    assert d["invalidas_por_formato"] == {"NATIVO": {"texto_no_coincide": 2}, "ESCANEADO": {"texto_no_coincide": 1}}
    assert d["similitud_mediana_invalidas"] == .25
    assert d["motivos_incierto"] == [dict(motivo="falta informacion", cantidad=3)]


def test_cita_en_otra_pagina_no_se_valida_por_similitud_global():
    cita = "Se requieren licencias originales del fabricante para participar"
    d = replay.diagnosticar_cita("triaje", dict(cita=cita, pagina=1),
        {1: "Objeto distinto sin relación con lo solicitado", 2: cita}, 2)
    assert not d["valida"] and d["motivo_invalidez"] == "texto_no_coincide"
    assert d["similitud_texto"] == d["similitud"] == 1
    assert d["similitud_pagina"] < 1
    sin_capa = replay.diagnosticar_cita("analisis", dict(cita=cita, pagina=2), {}, 3)
    assert sin_capa["motivo_invalidez"] == "sin_texto_de_pagina"
    assert "fragmento" not in sin_capa


def test_copia_config_replay_y_timeout(tmp_path, cfg):
    import copy
    import time

    local = replay.ConfigReplay(cfg, ["c0", "c2"], tmp_path)
    copia = copy.copy(local)
    assert copia is not local and copia._cfg is cfg
    assert copia.intel_c0 == copia.intel_c2 == "activo"
    assert copia.pliego_max_mb == cfg.pliego_max_mb
    acotada = replay.lectura._config_con_tiempo(local, time.monotonic())
    assert acotada.llm_timeout_s <= local.llm_timeout_s
    assert not hasattr(object.__new__(replay.ConfigReplay), "_cfg")


@pytest.mark.parametrize("pagina,motivo", [(None, "sin_pagina"), (99, "pagina_inexistente"),
                                            (1, "texto_no_coincide"), (3, "sin_texto_de_pagina")])
def test_diagnostico_busca_documento_sin_ocultar_pagina_informada(pagina, motivo):
    cita = "Se exige carta del fabricante original para participar"
    d = replay.diagnosticar_cita("analisis", dict(cita=cita, pagina=pagina),
                                {1: "Otro objeto", 2: cita, 3: ""}, 3)
    assert not d["valida"] and d["motivo_invalidez"] == motivo
    assert d["valida_en_documento"] and d["pagina_real"] == 2


def test_selector_error_visible_y_transcripcion_log(tmp_path, cfg, monkeypatch, caplog):
    local = replay.ConfigReplay(cfg, ["c2"], tmp_path)
    monkeypatch.setattr(replay.pdf, "page_count", MagicMock(side_effect=RuntimeError("PDF ficticio")))
    diagnostico = {}
    assert replay.lectura.seleccionar_paginas("fake.pdf", 2, local, {}, diagnostico=diagnostico) == []
    assert diagnostico == {"c2_selector": "error:RuntimeError"}
    assert replay.lectura.transcribir_paginas("fake.pdf", [1], local) == []
    assert len(caplog.records) == 2 and all(r.exc_info for r in caplog.records)


def test_selector_replay_llega_llm(tmp_path, cfg, monkeypatch):
    local = replay.ConfigReplay(cfg, ["c2"], tmp_path)
    monkeypatch.setattr(replay.pdf, "page_count", lambda _: 2)
    monkeypatch.setattr(replay.lectura, "_contenido", lambda *a: [llm.BloqueTexto("Página física 1")])
    monkeypatch.setattr(replay.lectura.quota, "cabe_lectura", lambda *a, **kw: True)
    monkeypatch.setattr(replay.lectura.quota, "cobrar_lectura", lambda *a, **kw: None)
    completar = MagicMock(return_value=llm.Respuesta('{"paginas": []}', llm.Uso(1, 1), "fake", "fake"))
    monkeypatch.setattr(llm, "completar", completar)
    d = {}
    assert replay.lectura.seleccionar_paginas("fake.pdf", 2, local, {}, diagnostico=d) == [1]
    completar.assert_called_once()
    assert d["c2_selector"] == "ok"


def test_replay_escaneado_cuenta_transcripciones(tmp_path, cfg, monkeypatch):
    import fitz

    from src.scraper.models import ResultadoAnalisis

    ruta = tmp_path / "escaneado.pdf"
    with fitz.open() as doc:
        doc.new_page()
        doc.new_page()
        doc.save(ruta)
    local = replay.ConfigReplay(cfg, ["c0", "c2"], tmp_path)
    cita = "Se exige licencia del fabricante Ejemplo para este contrato"
    monkeypatch.setattr(replay.pipeline, "contexto_cliente", lambda _: "Contexto ficticio")
    monkeypatch.setattr(replay.pipeline, "marcas_del_portafolio", lambda _: ("Ejemplo",))
    monkeypatch.setattr(replay.triage, "triage", lambda *a, **kw:
                        replay.triage.ResultadoTriaje("GO", .8, "TI", None, {}))

    def seleccionar(*a, diagnostico):
        diagnostico["c2_selector"] = "ok"
        return [1, 2]

    monkeypatch.setattr(replay.lectura, "seleccionar_paginas", seleccionar)
    monkeypatch.setattr(replay.lectura, "transcribir_paginas", lambda *a:
                        [dict(pagina=2, texto=cita)])
    monkeypatch.setattr(replay.analysis, "analizar_pliego", lambda *a, **kw: ResultadoAnalisis(
        proceso_uuid="REF-1", estado="ANALIZADO", marca_exigida=dict(marca="Ejemplo", cita=cita, pagina=None)))
    monkeypatch.setattr(replay.analysis, "aplicar_competibilidad", lambda *a, **kw: None)
    salida = replay.analizar(fila(), str(ruta), local, None)
    assert salida["marca_cita_valida"] and salida["pagina_marca"] == 2
    assert salida["diagnostico"]["c2_selector"] == "ok"
    assert salida["diagnostico"]["c2_transcritas"] == 1
    assert salida["diagnostico"]["formato"] == "ESCANEADO"


def test_prompts_checkpoint_viejo_mezcla_y_cache(tmp_path, cfg):
    estado = dict(llamadas={}, gastado=0, forzar_ventana=True)
    respuesta = llm.Respuesta("{}", llm.Uso(1, 1), "claude-haiku-4-5", "fake")
    completar = MagicMock(return_value=respuesta)
    guardia = replay.Guardia(estado, tmp_path / "checkpoint", tmp_path, 3, completar)
    pedido = llm.Pedido("triaje", [llm.BloqueTexto("demo")], sistema="A")
    guardia(cfg, pedido)
    huella = estado["prompts"]["base"][0]
    assert len(huella) == 12
    guardia(cfg, pedido)
    assert completar.call_count == 1
    pedido.sistema = "B"
    guardia(cfg, pedido)
    assert len(estado["prompts"]["base"]) == 2
    assert estado["avisos"] == ["mezcla de versiones de prompt: base"]
    assert json.loads((tmp_path / "checkpoint").read_text())["prompts"] == estado["prompts"]
    assert all(llamada["prompt"]["app"] == "dev" for llamada in estado["llamadas"].values())


def test_persistencia_con_procedencia_y_clave_prompt(monkeypatch):
    from src.aprendizaje import repo
    monkeypatch.setattr(repo, "VARIANTES", repo.VARIANTES | {"replay"})
    guardar = MagicMock()
    monkeypatch.setattr(repo, "guardar_decision", guardar)
    conn = MagicMock()
    @contextmanager
    def conectar():
        yield conn
    prompt = dict(hash="123456abcdef", app="dev", cliente_config=2)
    estado = dict(capas=["c0"], firma="firma", resultados={"REF-1": {"nuevo": nuevo()}},
                  llamadas={"a": dict(gid="portal:REF-1", capa="C0", costo=.1,
                                      latencia_ms=1, prompt=prompt)})
    replay.persistir_decisiones(estado, [fila()], "demo", conectar)
    primera = guardar.call_args.kwargs
    assert primera["salida"]["prompt"] == prompt
    assert primera["salida"]["prompts"] == [prompt]
    estado["decisiones_guardadas"] = []
    estado["llamadas"]["a"]["prompt"] = dict(prompt, hash="abcdef123456")
    replay.persistir_decisiones(estado, [fila()], "demo", conectar)
    assert primera["entrada_hash"] != guardar.call_args.kwargs["entrada_hash"]
