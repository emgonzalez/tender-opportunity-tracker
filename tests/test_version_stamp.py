"""Tests del sello de versión del artefacto (Fase 2 del plan de adopción).

Garantía que se prueba: toda corrida deja registrado QUÉ código la produjo.
Sin esto, con el despliegue en modo pull no hay forma de distinguir "la VM
aplicó la imagen nueva" de "la VM sigue corriendo la anterior y funciona igual".
"""
import json
import os
import types
from unittest.mock import patch


def _cfg(version: str, data_dir: str):
    return types.SimpleNamespace(app_version=version, data_dir=data_dir,
                                 last_run_path=os.path.join(data_dir, "last_run.json"))


class TestConfigAppVersion:
    def test_sin_APP_VERSION_vale_dev(self):
        """Correr fuera de una imagen de CI no debe fallar: degrada a 'dev'."""
        from src.config import Config

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("APP_VERSION", None)
            campo = Config.__dataclass_fields__["app_version"]
            assert campo.default_factory() == "dev"

    def test_lee_APP_VERSION_del_entorno(self):
        """Es lo que hornean los Dockerfiles vía ARG GIT_SHA."""
        from src.config import Config

        with patch.dict(os.environ, {"APP_VERSION": "abc123"}):
            campo = Config.__dataclass_fields__["app_version"]
            assert campo.default_factory() == "abc123"


class TestLastRunLlevaLaVersion:
    def test_la_corrida_exitosa_registra_la_version(self, tmp_path):
        from src.main import _save_last_run

        _save_last_run(_cfg("abc123", str(tmp_path)), success=True,
                       procesos_count=49, clients_processed=1)

        datos = json.loads((tmp_path / "last_run.json").read_text())
        assert datos["version"] == "abc123"
        # Los campos que ya existían siguen ahí: esto es aditivo, no un reemplazo.
        assert datos["success"] is True
        assert datos["procesos_revisados"] == 49

    def test_la_corrida_fallida_TAMBIEN_registra_la_version(self, tmp_path):
        """El caso que más importa: para diagnosticar un fallo hay que saber qué
        versión falló. Si el sello solo se escribiera en el camino feliz, faltaría
        justo cuando se necesita."""
        from src.main import _save_last_run

        _save_last_run(_cfg("def456", str(tmp_path)), success=False,
                       procesos_count=0, clients_processed=0)

        datos = json.loads((tmp_path / "last_run.json").read_text())
        assert datos["version"] == "def456"
        assert datos["success"] is False
