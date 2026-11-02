"""El fallback del pipeline recupera resultados sin matriz del JSONL y del spool."""
from dataclasses import asdict
from types import SimpleNamespace

import pytest

from src import history
from src.drilldown import pipeline
from src.notifier import agrupamiento
from src.persistencia.spool import Spool
from src.scraper.models import MOTIVO_PROVEEDOR_UNICO, Proceso, ResultadoAnalisis


@pytest.mark.parametrize("fuente", ["archivo", "adb"])
def test_rehidratar_notificado_sin_matriz_conserva_bloqueo(tmp_path, monkeypatch, fuente):
    monkeypatch.setenv("ESTADO_FUENTE", "archivo")
    ref = "0000-2099-P0099"
    anterior = ResultadoAnalisis(ref, "NOTIFICADO", competible=False,
                                 motivo_bloqueo=MOTIVO_PROVEEDOR_UNICO)
    if fuente == "archivo":
        history.save_analisis("cli", anterior, str(tmp_path))
    else:
        # Una marca anterior en JSONL no debe ocultar la más reciente del spool.
        history.save_analisis("cli", ResultadoAnalisis(ref, "NOTIFICADO"), str(tmp_path))
        Spool(tmp_path / "history" / "spool").agregar("analisis.cli", asdict(anterior))
    monkeypatch.setenv("ESTADO_FUENTE", fuente)
    monkeypatch.setattr(history, "load_analisis", lambda *a, **kw: {})
    lector = history.ultimo_resultado
    llamadas = []

    def leer(*args):
        llamadas.append(args)
        return lector(*args)

    monkeypatch.setattr(history, "ultimo_resultado", leer)
    proceso = Proceso(institucion="Institución de prueba", descripcion="Software",
                      fecha_publicacion=None, url_directa="https://example.com/p/1",
                      numero_referencia=ref)
    resultado = pipeline._rehidratar_terminal(
        SimpleNamespace(data_dir=str(tmp_path)), "cli", proceso, "NOTIFICADO", ())

    assert llamadas == [("cli", ref, "NOTIFICADO", str(tmp_path))]
    assert resultado.estado == "NOTIFICADO"
    assert resultado.competible is False
    assert resultado.motivo_bloqueo == MOTIVO_PROVEEDOR_UNICO
    assert agrupamiento.grupo_de(95, resultado) == agrupamiento.GRUPO_NO_COMPETIBLE
