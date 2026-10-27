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


def test_caddy_solo_reenvia_lo_que_el_servicio_expone():
    """Entrega 7: feedback, sonda y el portal con su acceso. Todo lo demás, 404 en el borde."""
    caddyfile = (RAIZ / "oci/caddy/Caddyfile").read_text()
    assert "path /f/* /healthz /acceso /acceso/* /salir /portal /portal/*" in caddyfile
    assert caddyfile.count("reverse_proxy") == 1
    handles = caddyfile.split("handle")
    assert "respond 404" in handles[-1]  # el handle sin matcher es el 404
    assert "{$WEB_HOSTNAME}" in caddyfile


def test_lo_que_caddy_publica_es_justo_lo_que_el_servicio_tiene():
    """Si se agrega una ruta al servicio sin tocar Caddy (o al revés) esto avisa."""
    import re

    from src.web import acceso, feedback, portal

    caddyfile = (RAIZ / "oci/caddy/Caddyfile").read_text()
    publicado = set(re.search(r"@expuesto path (.+)", caddyfile).group(1).split())
    # `/healthz` vive en la fábrica de la app; el resto, en los routers que ella monta.
    propias = {"/healthz"} | {r.path for router in (feedback.router, portal.router, acceso.router)
                              for r in router.routes}

    def cubierta(ruta: str) -> bool:
        return any(ruta == p or (p.endswith("/*") and ruta.startswith(p[:-1])) or
                   (p == "/portal" and ruta.startswith("/portal")) for p in publicado)

    assert all(cubierta(p) for p in propias), {p for p in propias if not cubierta(p)}
    assert {"/acceso", "/salir", "/portal", "/healthz"} <= propias


def test_el_portal_es_un_flag_por_tenant_apagado_por_defecto_y_encendido_en_dev():
    assert "ENABLE_PORTAL            features.enable_portal    false" in (RAIZ / "oci/lib/04-app.sh").read_text()
    tenants = yaml.safe_load((RAIZ / "oci/tenants.yaml").read_text())["tenants"]
    # Encendido el 2026-10-26 en DEV (Entrega 7), con IDENTIDAD_SECRET y la imagen E3-E7 desplegadas.
    assert tenants["multicomputos_dev"]["features"]["enable_portal"] is True
    # Cualquier otro tenant lo hereda apagado: publicar el login es una decisión explícita por entorno.
    for nombre, tenant in tenants.items():
        if nombre != "multicomputos_dev":
            assert tenant["features"].get("enable_portal", False) is False, nombre


def test_el_secreto_de_identidad_sobrevive_a_los_despliegues():
    """`deploy.sh` reconstruye el .env de la VM: un IDENTIDAD_SECRET puesto a mano se pierde (pasó el
    2026-10-26) y con él el portal queda cerrado. Debe salir del state, estable, como el del feedback."""
    app = (RAIZ / "oci/lib/04-app.sh").read_text()
    web = (RAIZ / "oci/lib/08-web.sh").read_text()
    assert "FEEDBACK_HMAC_SECRET IDENTIDAD_SECRET; do" in app  # se borra el residuo local...
    assert "printf 'IDENTIDAD_SECRET=%s\\n' \"$(web_identidad_secret \"$tenant\")\"" in app  # ...y se reescribe
    assert 'f="${OCI_DIR}/state/${1}_identidad.secret"' in web
    assert "[[ ! -s \"$f\" ]]" in web  # se genera solo la primera vez
    assert "openssl rand -hex 32" in web
    # Distinto del del feedback: rotar uno no debe invalidar el otro.
    assert "_identidad.secret" in web and "_feedback_hmac.secret" in web


def test_deploy_recrea_caddy_si_su_caddyfile_cambio():
    """Regresión del 2026-10-26: rsync reemplaza el Caddyfile (otro inodo), el bind mount queda viendo el
    viejo y Caddy sigue sirviendo la lista anterior. `web_up` compara contenidos y recrea solo Caddy."""
    web = (RAIZ / "oci/lib/08-web.sh").read_text()
    assert "md5sum ${app_dir}/oci/caddy/Caddyfile" in web
    assert "docker exec amalia-caddy md5sum /etc/caddy/Caddyfile" in web
    assert "up -d --force-recreate caddy" in web
    # Va DESPUÉS del `up -d web caddy` y ANTES de esperar al web.
    assert web.index("up -d web caddy") < web.index("--force-recreate caddy") < web.index("responde /healthz por dentro")
