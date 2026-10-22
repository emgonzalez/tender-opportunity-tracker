"""Servicio web residente en OCI (ADR 0005, Fase 2): configuración estática.

No toca OCI. Verifica lo que, mal puesto, rompe en silencio: que el 80/443 viaje en el
mismo update que el 22, que Caddy solo reenvíe lo que el servicio expone, que el timer
diario no dependa de los servicios nuevos y que el feedback siga apagado hasta verificar
HTTPS.
"""
import json
import subprocess
from pathlib import Path

import yaml

RAIZ = Path(__file__).resolve().parents[1]


def _ingress(ssh_cidr, web):
    salida = subprocess.run(
        ["bash", "-c", f'export OCI_DIR="{RAIZ}/oci"; source "$OCI_DIR/lib/01-network.sh"; '
                       f'web_ingress_rules "{ssh_cidr}" "{web}"'],
        capture_output=True, text=True, check=True,
    ).stdout
    return json.loads(salida)


def _puertos(reglas):
    return {r["tcpOptions"]["destinationPortRange"]["min"]: r["source"] for r in reglas}


def test_ingress_sin_web_solo_abre_el_22():
    assert _puertos(_ingress("1.2.3.4/32", "False")) == {22: "1.2.3.4/32"}


def test_ingress_con_web_suma_80_y_443_sin_tocar_el_22():
    assert _puertos(_ingress("1.2.3.4/32", "True")) == {
        22: "1.2.3.4/32", 80: "0.0.0.0/0", 443: "0.0.0.0/0",
    }


def test_security_list_usa_el_helper_en_un_solo_update():
    codigo = (RAIZ / "oci/lib/01-network.sh").read_text()
    assert codigo.count("security-list update") == 1
    assert 'web_ingress_rules "$ssh_cidr"' in codigo


def test_compose_e2_servicios_web_y_caddy():
    compose = yaml.safe_load((RAIZ / "docker-compose.e2.yml").read_text())
    servicios = compose["services"]
    assert {"amalia", "web", "caddy"} <= set(servicios)
    assert servicios["caddy"]["ports"] == ["80:80", "443:443"]
    assert "ports" not in servicios["web"]  # solo Caddy da la cara
    # La corrida diaria (`run --rm amalia`) no debe arrastrar a los servicios nuevos.
    assert "depends_on" not in servicios["amalia"]
    # Si hay OOM muere primero la corrida (1000), no el endpoint de los clics.
    assert servicios["amalia"]["oom_score_adj"] > servicios["web"]["oom_score_adj"]
    assert "caddy_data" in compose["volumes"]  # los certificados persisten


def test_caddy_solo_reenvia_feedback_y_healthz():
    caddyfile = (RAIZ / "oci/caddy/Caddyfile").read_text()
    assert "path /f/* /healthz" in caddyfile
    assert "respond 404" in caddyfile
    assert "/portal" not in caddyfile.replace("/portal, que", "")  # nunca se publica
    assert "{$WEB_HOSTNAME}" in caddyfile


def test_tenant_dev_declara_el_web_y_enciende_el_feedback():
    dev = yaml.safe_load((RAIZ / "oci/tenants.yaml").read_text())["tenants"]["multicomputos_dev"]
    assert dev["web"]["enabled"] is True
    assert dev["web"]["hostname"] == "dev.amalia.multicomputos.com"
    assert dev["web"]["dns_zone"] == "dev.amalia.multicomputos.com"
    assert dev["features"]["enable_feedback"] is True  # encendido el 2026-10-22, con healthz verificado


def test_el_tenant_a1_no_abre_el_web():
    a1 = yaml.safe_load((RAIZ / "oci/tenants.yaml").read_text())["tenants"]["multicomputos_dev_a1"]
    assert "web" not in a1


def test_el_feedback_se_gobierna_por_tenant_en_el_env():
    assert "ENABLE_FEEDBACK          features.enable_feedback  false" in (RAIZ / "oci/lib/04-app.sh").read_text()


def test_deploy_reserva_la_ip_antes_de_sincronizar_la_acl_de_la_adb():
    deploy = (RAIZ / "oci/deploy.sh").read_text()
    orden = ['web_reserve_ip "$TENANT"', 'adb_acl_sync   "$TENANT"', 'app_deploy     "$TENANT"',
             'web_dns_record "$TENANT"', 'web_up         "$TENANT"']
    posiciones = [deploy.index(x, deploy.index("network_create")) for x in orden]
    assert posiciones == sorted(posiciones)
