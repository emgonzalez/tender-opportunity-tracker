"""P10: el correo consume el veredicto persistido sin volver a clasificar."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.notifier import email_sender
from src.notifier.email_sender import agrupar, componer_correo
from src.scraper.models import MarcaRecomendada, ProcesoClasificado, ResultadoAnalisis

HOY = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)
ORACLE = frozenset({"oracle"})


@pytest.fixture(autouse=True)
def plantillas(monkeypatch):
    monkeypatch.setattr(email_sender, "TEMPLATES_DIR", Path(__file__).resolve().parents[1] / "templates")


def _proc(nivel="confirmada", *, marca="Oracle", ref="P-1", match=.86, titulo="Licencias de software"):
    return ProcesoClasificado(
        institucion="Institución de prueba", descripcion=titulo, numero_referencia=ref,
        fecha_publicacion=HOY, fecha_limite=HOY + timedelta(days=3),
        url_directa=f"https://compras.example.com/{ref}", es_relevante=True,
        match_tecnico=match, match_categoria=match, factibilidad=match,
        marcas_recomendadas=[MarcaRecomendada(
            marca=marca, veredicto=nivel, por_que="Motivo verificado", evidencia="Cita literal", origen="pliego",
        )],
    )


def _componer(procesos, modo="activo", solo_marcas=ORACLE, analisis=None):
    cfg = SimpleNamespace(marca_veredicto=modo, app_name="AMALIA", app_full_name="AMALIA Test", enable_feedback=False)
    perfil = SimpleNamespace(client_id="cliente_prueba", client_name="Cliente de prueba", catalog=None)
    asunto, render = componer_correo(cfg, perfil, procesos, 12, HOY, analisis, HOY, solo_marcas)
    html, texto = render(lambda _: None)
    return asunto, html, texto


@pytest.mark.parametrize("modo", ["off", "sombra", "activo"])
@pytest.mark.parametrize("nivel,grupo", [
    ("confirmada", "decidir"), ("por_confirmar", "por_confirmar"),
    ("no_competible", "no_competible"), ("fuera", None), ("", "por_confirmar"),
])
def test_tabla_veredictos_y_secciones(modo, nivel, grupo):
    p = _proc(nivel)
    esperado = grupo if modo == "activo" else "decidir"
    grupos = agrupar([p], None, HOY, solo_marcas=ORACLE, modo=modo)
    assert [k for k, filas in grupos.items() if filas] == ([esperado] if esperado else [])
    _, html, texto = _componer([p], modo)
    if esperado is None:
        assert p.numero_referencia not in html + texto
    else:
        assert p.numero_referencia in html + texto
    if modo == "activo" and esperado == "por_confirmar":
        assert "Por confirmar" in html and "POR CONFIRMAR" in texto
        assert "Motivo verificado" in html and "Motivo verificado" in texto
    if modo == "activo" and esperado == "no_competible":
        assert "No competibles" in html and "NO COMPETIBLES" in texto
        assert "Cita literal" in html and "Cita literal" in texto


def test_off_y_sombra_producen_el_mismo_correo():
    procesos = [_proc("fuera"), _proc("por_confirmar", ref="P-2")]
    assert _componer(procesos, "off") == _componer(procesos, "sombra")


@pytest.mark.parametrize("marca", ["NetSuite", "MySQL", "Java", "WebLogic", "Solaris", "Sun", "PeopleSoft"])
def test_productos_oracle_se_incluyen_en_activo(marca):
    _, html, texto = _componer([_proc(marca=marca)])
    assert "P-1" in html and "P-1" in texto
    assert "Oracle: confirmado" in html + texto


def test_por_confirmar_no_altera_asunto_contadores_ni_primera_oportunidad():
    confirmada = _proc(ref="P-CONFIRMADA")
    pendiente = _proc("por_confirmar", ref="P-PENDIENTE")
    pendiente.fecha_limite = HOY
    asunto, html, texto = _componer([confirmada, pendiente])
    assert asunto == _componer([confirmada])[0]
    assert "1 relevante = 1 para decidir" in texto
    assert "2 relevantes" not in texto
    assert html.index("P-CONFIRMADA") < html.index("P-PENDIENTE")
    assert "match del proceso 86" in html + texto


def test_cognos_fuera_se_oculta_a_oracle_y_completo_explica_fabricante():
    p = _proc("fuera", titulo="Renovación de licencias COGNOS")
    p.marcas_ajenas = [MarcaRecomendada(marca="IBM")]
    ra = ResultadoAnalisis(
        proceso_uuid=p.numero_referencia, estado="ANALIZADO", competible=False,
        marca_exigida={"marca": "IBM", "cita": "Se exige carta del fabricante IBM"},
    )
    _, html, texto = _componer([p], analisis={p.numero_referencia: ra})
    assert "COGNOS" not in html + texto
    _, html, texto = _componer([p], solo_marcas=frozenset(), analisis={p.numero_referencia: ra})
    assert "IBM exigida · no competible" in html and "IBM exigida · no competible" in texto
    assert "Se exige carta del fabricante IBM" in html + texto


def test_completo_muestra_motivo_cita_y_fuente_de_confirmadas():
    _, html, texto = _componer([_proc()], solo_marcas=frozenset())
    for valor in ("Motivo verificado", "Cita literal", "pliego"):
        assert valor in html and valor in texto


def test_autoescape_y_separacion_de_marcas_entre_perfiles():
    p = _proc()
    p.marcas_recomendadas[0].por_que = '<script>alert("x")</script>'
    p.marcas_recomendadas.append(MarcaRecomendada(marca="Dell", veredicto="confirmada", por_que="SOLO OTRO PERFIL"))
    _, html, texto = _componer([p])
    assert "<script>" not in html and "&lt;script&gt;" in html
    assert "SOLO OTRO PERFIL" not in html + texto
    assert "Dell" not in html + texto


def test_confirmada_manda_sobre_competibilidad_global_y_respeta_triaje():
    p = _proc()
    ra = ResultadoAnalisis(proceso_uuid=p.numero_referencia, estado="ANALIZADO", competible=False)
    grupos = agrupar([p], {p.numero_referencia: ra}, HOY, solo_marcas=ORACLE, modo="activo")
    assert len(grupos["decidir"]) == 1
    ra.triaje = "NO-GO"
    grupos = agrupar([p], {p.numero_referencia: ra}, HOY, solo_marcas=ORACLE, modo="activo")
    assert len(grupos["baja"]) == 1


def test_no_competible_restringida_conserva_fabricante_y_cita_del_pliego():
    p = _proc("no_competible")
    ra = ResultadoAnalisis(
        proceso_uuid=p.numero_referencia, estado="ANALIZADO", competible=False,
        marca_exigida={"marca": "Oracle", "cita": "El fabricante exige proveedor único"},
    )
    _, html, texto = _componer([p], analisis={p.numero_referencia: ra})
    for cuerpo in (html, texto):
        assert "Oracle exigida · no competible" in cuerpo
        assert "El fabricante exige proveedor único" in cuerpo


def test_sombra_registra_diferencias_por_perfil_sin_contenido_personal(tmp_path):
    import json

    from src.notifier.por_marca import registrar_sombra

    cfg = SimpleNamespace(marca_veredicto="sombra", data_dir=tmp_path)
    fuera = _proc("fuera", ref="P-FUERA", titulo="TÍTULO QUE NO SE REGISTRA")
    vieja = _proc("", ref="P-VIEJA")
    confirmada = _proc("confirmada", ref="P-CONFIRMADA")
    fuera.marcas_recomendadas[0].por_que = "persona@example.com"
    registrar_sombra(cfg, "cliente_prueba", [fuera, vieja, confirmada], [ORACLE], HOY)
    ruta = tmp_path / "history" / "cliente_prueba.marca_sombra.jsonl"
    crudo = ruta.read_text()
    filas = [json.loads(linea) for linea in crudo.splitlines()]
    assert "persona@example.com" not in crudo
    assert "TÍTULO QUE NO SE REGISTRA" not in crudo
    assert "Cita literal" not in crudo
    oracle = [f for f in filas if f["perfil"] == ["oracle"]]
    assert {(f["proceso"], f["legado"], f["nuevo"]) for f in oracle} == {
        ("P-FUERA", "decidir", "fuera"), ("P-VIEJA", "decidir", "por_confirmar"),
    }
    completo = [f for f in filas if f["perfil"] == ["completo"]]
    assert any(f["proceso"] == "P-CONFIRMADA" and f["explicaciones_nuevas"] for f in completo)


@pytest.mark.parametrize("modo", ["off", "activo"])
def test_registro_sombra_solo_escribe_en_sombra(tmp_path, modo):
    from src.notifier.por_marca import registrar_sombra

    registrar_sombra(SimpleNamespace(marca_veredicto=modo, data_dir=tmp_path),
                     "cliente_prueba", [_proc("fuera")], [ORACLE], HOY)
    assert not (tmp_path / "history").exists()


def test_registro_sombra_no_propaga_fallo_de_escritura(tmp_path):
    from src.notifier.por_marca import registrar_sombra

    archivo = tmp_path / "archivo"
    archivo.write_text("no es directorio")
    registrar_sombra(SimpleNamespace(marca_veredicto="sombra", data_dir=archivo),
                     "cliente_prueba", [_proc("fuera")], [ORACLE], HOY)
    assert archivo.read_text() == "no es directorio"


def test_cli_sombra_lee_sin_modificar_y_resume_incluso_linea_incompleta(tmp_path, capsys):
    import json

    from scripts.sombra_marca import leer_jsonl, main, resumir

    ruta = tmp_path / "sombra.jsonl"
    fila = {"perfil": ["oracle"], "legado": "decidir", "nuevo": "fuera", "proceso": "P-1"}
    contenido = json.dumps(fila) + "\n" + json.dumps(fila) + '\n{"incompleta":\n'
    ruta.write_text(contenido)
    assert resumir(leer_jsonl(ruta)) == [{"perfil": "oracle", "legado": "decidir", "nuevo": "fuera", "procesos": 2}]
    assert main(["--registro", str(ruta), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["procesos"] == 2
    assert ruta.read_text() == contenido
