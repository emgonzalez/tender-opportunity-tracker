"""Cosecha sintética sin conexiones externas."""
from src.aprendizaje import cosecha


def senal(fuente="resultado_comercial", **campos):
    return dict(proceso_gid="portal:demo", marca_id="oracle", titulo_texto="Licencias Oracle",
                etiqueta="positivo", fuente=fuente, **campos)


def test_fuentes_distintas_y_validacion():
    voto = senal("voto_usuario")
    assert cosecha.preparar_senales([voto, voto]) == []
    assert cosecha.preparar_senales([voto, senal("pliego", validacion=True)]) == []
    assert len(cosecha.preparar_senales([voto, senal("participacion")])) == 2


def test_humanos_prueba_criticos_inmutables():
    for campos in ({"fuente": "humana"}, {"split": "critico"}, {"split": "prueba"}):
        previo = {**senal(), **campos}
        assert cosecha.preparar_senales([senal()], [previo]) == []


def test_idempotencia_incluso_desactivados():
    previo = {**senal(), "activo": False}
    assert cosecha.preparar_senales([senal(), senal()], [previo]) == []


def test_contradicciones_omiten_todo():
    negativo = {**senal("voto_usuario"), "etiqueta": "ajena"}
    assert cosecha.preparar_senales([senal(), negativo]) == []


def test_dry_run_y_aplicar_vectoriza(monkeypatch):
    monkeypatch.setattr(cosecha.repo, "_todos", lambda *a: [])
    monkeypatch.setattr(cosecha, "leer_senales", lambda *a: [senal()])
    llamadas = []

    def agregar(*args, **kwargs):
        llamadas.append((args, kwargs))
        return {"nuevo": True, "ejemplo_id": 1}

    monkeypatch.setattr(cosecha.repo, "_auditar", lambda *a: None)
    monkeypatch.setattr(cosecha.repo, "agregar_ejemplo", agregar)
    assert cosecha.ejecutar(object(), "demo")["nuevos"] == 0
    assert llamadas == []
    assert cosecha.ejecutar(object(), "demo", aplicar=True)["nuevos"] == 1
    assert llamadas[0][1]["split"] == "entrenamiento"
    assert "calcular_embedding" not in llamadas[0][1]  # valor por defecto del repo: True


def test_motivos_comerciales_no_niegan_marca(monkeypatch):
    monkeypatch.setattr(cosecha.repo, "listar_terminos_activos", lambda *a: [
        {"marca_id": "oracle", "termino": "Oracle", "tipo": "fuerte"}])
    filas = iter([
        [("p:1", "Oracle", "IRRELEVANTE", "plazo_insuficiente", None),
         ("p:2", "Oracle", "IRRELEVANTE", "marca_no_representada", None)],
        [], [], [],
    ])
    monkeypatch.setattr(cosecha.repo, "_todos", lambda *a: next(filas))
    señales = cosecha.leer_senales(object(), "demo")
    assert len(señales) == 1
    assert señales[0]["etiqueta"] == "ajena"
    assert cosecha.preparar_senales(señales) == []  # voto sin fuente distinta


def test_pliego_exige_cita_en_pagina(monkeypatch):
    monkeypatch.setattr(cosecha.repo, "listar_terminos_activos", lambda *a: [
        {"marca_id": "oracle", "termino": "Oracle", "tipo": "fuerte"}])
    exigida = {"marca_exigida": {"marca": "oracle", "cita": "Oracle Database", "pagina": 2}}
    filas = iter([[], [], [], [
        ("p:1", "Licencias", exigida, 2, "Oracle Database requerido"),
        ("p:2", "Licencias", exigida, 2, "Otro producto"),
    ]])
    monkeypatch.setattr(cosecha.repo, "_todos", lambda *a: next(filas))
    señales = cosecha.leer_senales(object(), "demo")
    assert len(señales) == 1
    assert señales[0]["fuente"] == "pliego"
    assert cosecha.preparar_senales(señales) == []
