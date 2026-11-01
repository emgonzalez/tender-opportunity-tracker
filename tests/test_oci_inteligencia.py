"""Flags por tenant: sombra solo en DEV evita cambiar correos y portal en PROD.

No ejecuta despliegues: lee la tabla y prueba el lector YAML local, sin OCI ni red.
"""
import re
import subprocess
from pathlib import Path

import yaml

RAIZ = Path(__file__).resolve().parents[1]
FLAGS = [*(f"INTEL_C{i}" for i in range(9)), "INTEL_CLASIF", "MARCA_VEREDICTO"]
DEFAULTS = {"LLM_TEMPERATURA": "0.0", **dict.fromkeys(FLAGS, "off")}


def _tabla():
    app = (RAIZ / "oci/lib/04-app.sh").read_text()
    tabla = app.split("local -a tenant_env=(", 1)[1].split("\n    )", 1)[0]
    return re.findall(r'"(\w+)\s+(features\.\w+)\s+(\S+)"', tabla)


def test_tabla_inteligencia_tiene_defaults_seguros():
    entradas = _tabla()
    for nombre, default in DEFAULTS.items():
        assert [e for e in entradas if e[0] == nombre] == [
            (nombre, f"features.{nombre.lower()}", default),
        ]


def test_solo_dev_tiene_exactamente_c0_clasif_y_c2_en_sombra():
    # El incidente de marcas exige observar primero sin cambiar resultados visibles.
    tenants = yaml.safe_load((RAIZ / "oci/tenants.yaml").read_text())["tenants"]
    assert {"multicomputos_dev", "multicomputos_prod"} <= tenants.keys()
    for nombre, tenant in tenants.items():
        flags = {k: v for k, v in tenant.get("features", {}).items()
                 if k.startswith("intel_") or k == "marca_veredicto"}
        if nombre == "multicomputos_dev":
            assert flags == dict.fromkeys(("intel_c0", "intel_clasif", "intel_c2"), "sombra")
        else:
            assert all(v == "off" for v in flags.values()), nombre


def test_lector_usa_default_si_falta_y_conserva_sombra_y_temperatura_vacia(tmp_path):
    # Datos sintéticos: nunca se lee .env ni state, ni se llama a app_deploy.
    features = {"intel_c0": "sombra", "llm_temperatura": ""}
    (tmp_path / "tenants.yaml").write_text(yaml.safe_dump({
        "tenants": {"ausente": {"features": {}}, "sombra": {"features": features}},
    }))
    script = 'OCI_DIR=$1; source "$2"; shift 2\n'
    script += 'while (( $# )); do tenant_get_or "$1" "$2" "$3"; shift 3; done\n'
    args = []
    for nombre, ruta, default in _tabla():
        if nombre in DEFAULTS:
            args.extend(("ausente", ruta, default))
    esperados = [default for nombre, _, default in _tabla() if nombre in DEFAULTS]
    args.extend(("sombra", "features.intel_c0", "off",
                 "sombra", "features.llm_temperatura", "0.0"))
    salida = subprocess.run(
        ["bash", "-c", script, "_", str(tmp_path), str(RAIZ / "oci/lib/common.sh"), *args],
        capture_output=True, text=True, check=True,
    )
    assert salida.stdout.splitlines() == [*esperados, "sombra", ""]
    assert salida.stderr == ""
