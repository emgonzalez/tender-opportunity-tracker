"""SICOP en los tenants: DEV lo corre con ventana virtual y correos redirigidos; PROD lo mantiene apagado.

`clients/` es común a los dos tenants y `multicomputos_cr` está activo en su YAML: lo único que evita que PROD
corra SICOP (y escriba a destinatarios reales) es `PORTALES_DESHABILITADOS`. Este test fija esa protección
para que nadie la quite sin darse cuenta.
"""
from __future__ import annotations

from pathlib import Path

import yaml

RAIZ = Path(__file__).resolve().parent.parent


def _tenants() -> dict:
    return yaml.safe_load((RAIZ / "oci" / "tenants.yaml").read_text(encoding="utf-8"))["tenants"]


def test_dev_corre_sicop_con_ventana_virtual_y_correos_redirigidos():
    dev = _tenants()["multicomputos_dev"]
    assert dev["features"]["sicop_headless"] is False
    assert dev["features"]["fx_auto"] is True
    assert dev["notification"]["override_to"], "DEV no puede escribir a destinatarios reales"
    assert "portales_deshabilitados" not in dev["features"]


def test_prod_omite_sicop_aunque_el_yaml_de_costa_rica_este_activo():
    prod = _tenants()["multicomputos_prod"]
    deshabilitados = {p.strip() for p in str(prod["features"]["portales_deshabilitados"]).split(",")}
    assert "sicop_cr" in deshabilitados
    assert "fx_auto" not in prod["features"]
    assert "sicop_headless" not in prod["features"], "SICOP no se enciende en PROD sin promoverlo a propósito"


def test_las_dos_variables_llegan_al_env_del_tenant():
    texto = (RAIZ / "oci" / "lib" / "04-app.sh").read_text(encoding="utf-8")
    assert "SICOP_HEADLESS           features.sicop_headless" in texto
    assert "PORTALES_DESHABILITADOS features.portales_deshabilitados" in texto
    assert '"FX_AUTO features.fx_auto false"' in " ".join(texto.split())
