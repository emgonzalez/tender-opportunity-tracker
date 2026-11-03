"""Timer opt-in: solo archivos locales y comandos remotos simulados."""
import os
import subprocess
from pathlib import Path

import pytest
import yaml

RAIZ = Path(__file__).resolve().parents[1]
ETAPA = RAIZ / "oci/lib/10-aprendizaje.sh"


def test_banco_bash_aprendizaje():
    resultado = subprocess.run(
        ["bash", str(RAIZ / "scripts/test_aprendizaje_timer.sh")],
        capture_output=True, text=True, check=True,
    )
    assert "todos los escenarios verdes" in resultado.stdout


@pytest.mark.parametrize("existentes", [(), ("timer",), ("service",), ("timer", "service")])
def test_apagado_retira_units_sin_tocar_diario(tmp_path, existentes):
    # Ejecuta el cuerpo remoto contra un /etc ficticio; sudo/systemctl son stubs.
    units = tmp_path / "units"
    units.mkdir()
    for tipo in existentes:
        (units / f"amalia-aprendizaje.{tipo}").touch()
    diario = units / "amalia.timer"
    diario.write_text("intacto")
    llamadas = tmp_path / "llamadas"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for nombre, cuerpo in {
        "sudo": 'exec "$@"',
        "systemctl": 'echo "$*" >> "$LLAMADAS"',
    }.items():
        archivo = bin_dir / nombre
        archivo.write_text(f"#!/bin/sh\n{cuerpo}\n")
        archivo.chmod(0o755)
    remoto = ETAPA.read_text().split("<<'REMOTE'\n", 1)[1].split("\nREMOTE", 1)[0]
    remoto = remoto.replace("/etc/systemd/system", str(units))
    subprocess.run(
        ["bash", "-c", remoto], check=True, capture_output=True,
        env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}",
             "LLAMADAS": str(llamadas)},
    )
    assert list(units.iterdir()) == [diario]
    assert diario.read_text() == "intacto"
    if existentes:
        assert "daemon-reload" in llamadas.read_text()
    else:
        assert not llamadas.exists()


def test_tenants_reales_no_habilitan_aprendizaje():
    tenants = yaml.safe_load((RAIZ / "oci/tenants.yaml").read_text())["tenants"]
    for nombre, tenant in tenants.items():
        flags = tenant.get("features", {})
        assert flags.get("enable_aprendizaje_cron", False) is False, nombre
        assert flags.get("aprendizaje_aplicar", False) is False, nombre


def test_cableado_separado_del_diario_y_defaults():
    deploy = (RAIZ / "oci/deploy.sh").read_text()
    assert 'source "${OCI_DIR}/lib/10-aprendizaje.sh"' in deploy
    assert deploy.index('app_deploy     "$TENANT"') < deploy.index('aprendizaje_deploy "$TENANT"')
    app = (RAIZ / "oci/lib/04-app.sh").read_text()
    for flag in ("ENABLE_APRENDIZAJE_CRON", "APRENDIZAJE_APLICAR"):
        assert f"features.{flag.lower()} false" in app
    for ruta in (RAIZ / "oci/lib/aprendizaje").iterdir():
        texto = ruta.read_text()
        assert "amalia.timer" not in texto
        assert "amalia.service" not in texto
        assert "Conflicts=" not in texto
    assert "trap " not in ETAPA.read_text()
