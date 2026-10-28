#!/usr/bin/env python3
"""Capturas reproducibles de la vista de país del portal (banco visual de E13).

Renderiza `/portal` con el CÓDIGO REAL de `src/web/portal.py` (`_contexto_pais`, topes y plantillas) sobre un
conjunto de datos PESADO y determinista (≈ 260 oportunidades, títulos largos, sin montos, decenas de descartados y
votos), lo fotografía con Chromium en claro/oscuro × 1360/390 px y mide la altura de la página y de cada sección.
Es la herramienta con la que se puntúa la rúbrica de E13: «nada se da por bueno sin una captura mirada».

    python3 scripts/capturas_portal.py --salida /ruta/capturas [--etiqueta base] [--oportunidades 260]

Escribe `<etiqueta>_<tema>_<ancho>.png`, `<etiqueta>.html` y `<etiqueta>_medidas.json`, e imprime una tabla.
Necesita el Chromium de Playwright: lo busca en `~/Library/Caches/ms-playwright` (el paquete Python puede pedir
otra versión, por eso se usa `executable_path`) o en `$CHROMIUM_EXE`.
No es código de producción: reutiliza los ayudantes de `tests/test_web_portal.py` para simular la base.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import random
import sys
from datetime import datetime, timedelta
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))
sys.path.insert(0, str(RAIZ / "tests"))

TITULOS = [
    "ADQUISICIÓN DE EQUIPOS INFORMÁTICOS Y LICENCIAMIENTO DE SOFTWARE DE VIRTUALIZACIÓN PARA EL DATACENTER INSTITUCIONAL",
    "SERVICIO DE SOPORTE Y MANTENIMIENTO PREVENTIVO Y CORRECTIVO DE LA INFRAESTRUCTURA DE RED Y TELECOMUNICACIONES",
    "SUMINISTRO DE SERVIDORES, ALMACENAMIENTO Y RESPALDO PARA LA MODERNIZACIÓN DE LA PLATAFORMA TECNOLÓGICA",
    "RENOVACIÓN DE LICENCIAS DE SEGURIDAD PERIMETRAL FIREWALL Y PROTECCIÓN DE CORREO ELECTRÓNICO",
    "CONTRATACIÓN DE SERVICIOS DE NUBE PÚBLICA Y MIGRACIÓN DE APLICACIONES INSTITUCIONALES",
    "LICENCIAS DE BASE DE DATOS ORACLE Y SOPORTE PREMIER POR TRES AÑOS",
    "SWITCHES DE CAPA 3 PARA SEDE CENTRAL Y OFICINAS REGIONALES",
    "ADQUISICIÓN DE COMPUTADORAS PORTÁTILES Y ESTACIONES DE TRABAJO PARA PERSONAL ADMINISTRATIVO",
]
INSTITUCIONES = ["MINISTERIO DE SALUD", "ADMINISTRACIÓN NACIONAL DE ACUEDUCTOS Y ALCANTARILLADOS", "CEL", "ISSS",
                 "MUNICIPIO DE SAN SALVADOR CENTRO", "REGISTRO NACIONAL DE LAS PERSONAS NATURALES (RNPN)", "UES",
                 "MINISTERIO DE HACIENDA", "BANCO DE DESARROLLO DE EL SALVADOR (BANDESAL)", "TRIBUNAL SUPREMO ELECTORAL"]
MARCAS = ["Oracle", "Cisco", "Dell", "HPE", "Fortinet", "VMware", "Microsoft", "Veritas", "Nutanix", "Aruba", "Citrix", "APC"]
NOMBRES = ["Valeria R.", "Carlos M.", "Astrid C.", "Damaris G.", "Mauricio S.", "Ricardo P."]
HOY = datetime(2026, 10, 27, 10, 0)
MESES = ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"]
DIAS = ["lun", "mar", "mié", "jue", "vie", "sáb", "dom"]


def filas_pesadas(n: int, semilla: int = 7) -> list[dict]:
    """`n` procesos ya anotados (lo que entrega `tablero.clasificar`): mezcla realista de grupos, estados y cierres."""
    from test_web_portal import ZONA_SV, _fila

    rnd = random.Random(semilla)
    filas = []
    for i in range(n):
        ref = f"{rnd.choice([96, 500, 2404, 3213, 4100, 8652])}-2026-P{i + 100:04d}"
        r = rnd.random()
        grupo = "decidir" if r < 0.45 else ("baja" if r < 0.9 else "no_competible")
        # cierre: 25 % vencidas, el resto repartido en las próximas semanas
        dias = rnd.randint(-12, -1) if rnd.random() < 0.25 else rnd.randint(0, 40)
        limite = (HOY + timedelta(days=dias)).replace(hour=15, tzinfo=ZONA_SV)
        urg = None if dias < 0 else ("rojo" if dias <= 7 else ("ambar" if dias <= 14 else "verde"))
        e = rnd.random()
        estado = None if e < 0.72 else rnd.choice(["EVALUANDO"] * 6 + ["OFERTADO"] * 3 + ["GANADO", "PERDIDO", "DESCARTADO"])
        resp = rnd.choice(NOMBRES) if (estado or rnd.random() < 0.15) else None
        desde = HOY - timedelta(days=rnd.randint(0, 14))
        f = _fila(
            ref, grupo, titulo=rnd.choice(TITULOS), institucion=rnd.choice(INSTITUCIONES), match=rnd.randint(55, 97),
            marcas=rnd.sample(MARCAS, rnd.randint(0, 3)), monto_usd=None, estado=estado,
            token=desde.isoformat() if estado else "", actualizado_en=desde if estado else None,
            responsable_id=f"u{NOMBRES.index(resp)}" if resp else None, responsable_nombre=resp,
            fecha_limite=limite.replace(tzinfo=None), dias_restantes=dias, urgencia=urg,
            cierre_texto=(f"Cierra {DIAS[limite.weekday()]} {limite.day} {MESES[limite.month - 1]} · {dias} días"
                          if dias >= 0 else f"Venció hace {-dias} días"))
        filas.append(f)
    return filas


def descartados_pesados(n: int = 40) -> dict:
    rnd = random.Random(11)
    filas = [{"proceso_gid": f"comprasal_sv:D-{i:03d}", "numero_referencia": f"500-2026-D{i:04d}",
              "titulo": rnd.choice(TITULOS), "institucion": rnd.choice(INSTITUCIONES), "match": rnd.randint(40, 79),
              "motivo_descarte": rnd.choice(["fuera del portafolio", "filtro de texto", "triaje NO-GO"]), "token": ""}
             for i in range(n)]
    return {"filas": sorted(filas, key=lambda f: -f["match"]), "total": 952, "desde": datetime(2026, 10, 27, 13, 0)}


def votos_pesados(filas: list[dict], n: int = 30) -> dict:
    rnd = random.Random(3)
    out = {}
    for f in rnd.sample(filas, min(n, len(filas))):
        out[f["proceso_gid"]] = {"relevante": rnd.choice([0, 0, 1]), "irrelevante": rnd.choice([0, 1, 2]),
                                 "motivo": rnd.choice(["fuera_de_rubro", "monto_muy_bajo", "marca_no_representada"])}
    return out


def renderizar(tema: str, n: int) -> str:
    """HTML real de `/portal` con el tema pedido (cookie) sobre los datos pesados."""
    import pytest
    from test_web_portal import _alcance, _cli, _parchear_e11, _parchear_kpis

    from src.web import plantillas, portal

    filas = filas_pesadas(n)
    with pytest.MonkeyPatch.context() as mp:
        real = portal.pagina
        _parchear_kpis(mp)
        _parchear_e11(mp, filas=filas, votos=votos_pesados(filas), descartados=descartados_pesados(),
                      asignables=[{"usuario_id": f"u{i}", "nombre": nombre} for i, nombre in enumerate(NOMBRES)],
                      estancadas=4, pipeline={"monto_usd": None, "activas": 90, "con_monto": 0})
        mp.setattr(portal, "pagina", real)  # plantillas reales, no el stub de los tests
        cliente = _cli(mp, _alcance(roles=("gerente_pais",), clientes=("cli",), cambia=("cli",)))
        if hasattr(plantillas, "COOKIE_TEMA"):
            cliente.cookies.set(plantillas.COOKIE_TEMA, tema)
        r = cliente.get("/portal?dias=30")
        if r.status_code != 200:
            raise SystemExit(f"/portal respondió {r.status_code}: {r.text[:300]}")
        return r.text


def chromium() -> str:
    explicito = os.environ.get("CHROMIUM_EXE")
    if explicito and os.path.exists(explicito):
        return explicito
    hallados = sorted(glob.glob(os.path.expanduser(
        "~/Library/Caches/ms-playwright/chromium_headless_shell-*/**/chrome-headless-shell"), recursive=True))
    if not hallados:
        raise SystemExit("No encuentro Chromium (define CHROMIUM_EXE o instala el de Playwright).")
    return hallados[-1]


JS_MEDIDAS = """() => {
  const h = (e) => Math.round(e.getBoundingClientRect().height);
  const secciones = [...document.querySelectorAll('main > *, .pagina > *, body > section, body > div > section')]
    .filter(e => e.offsetParent !== null || e.tagName === 'SECTION')
    .map(e => ({etiqueta: (e.id ? '#' + e.id : (e.getAttribute('aria-labelledby') || e.tagName.toLowerCase() + '.' + (e.className || '').toString().split(' ')[0])),
                alto: h(e)}));
  return {altura_total: document.documentElement.scrollHeight, ancho_scroll: document.documentElement.scrollWidth,
          ancho_ventana: window.innerWidth, secciones};
}"""


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--salida", required=True, help="carpeta donde dejar capturas, html y medidas")
    ap.add_argument("--etiqueta", default="captura")
    ap.add_argument("--oportunidades", type=int, default=260)
    ap.add_argument("--temas", default="claro,oscuro")
    ap.add_argument("--anchos", default="1360,390")
    a = ap.parse_args()
    salida = Path(a.salida)
    salida.mkdir(parents=True, exist_ok=True)

    from playwright.sync_api import sync_playwright

    medidas: dict[str, dict] = {}
    with sync_playwright() as p:
        nav = p.chromium.launch(executable_path=chromium())
        for tema in a.temas.split(","):
            html = renderizar(tema, a.oportunidades)
            archivo = salida / f"{a.etiqueta}_{tema}.html"
            archivo.write_text(html, encoding="utf-8")
            for ancho in (int(x) for x in a.anchos.split(",")):
                pag = nav.new_page(viewport={"width": ancho, "height": 900},
                                   color_scheme="dark" if tema == "oscuro" else "light")
                pag.goto(f"file://{archivo}")
                m = pag.evaluate(JS_MEDIDAS)
                m["pantallas"] = round(m["altura_total"] / 900, 2)
                medidas[f"{tema}_{ancho}"] = m
                pag.screenshot(path=str(salida / f"{a.etiqueta}_{tema}_{ancho}.png"), full_page=True)
                pag.close()
        nav.close()
    (salida / f"{a.etiqueta}_medidas.json").write_text(json.dumps(medidas, indent=2, ensure_ascii=False))

    print(f"\n{'variante':<14}{'altura px':>10}{'pantallas':>10}{'scroll-x':>10}")
    for k, m in medidas.items():
        flag = "" if m["ancho_scroll"] <= m["ancho_ventana"] else "  ← SCROLL HORIZONTAL"
        print(f"{k:<14}{m['altura_total']:>10}{m['pantallas']:>10}{m['ancho_scroll']:>10}{flag}")
    ref = medidas.get("claro_1360") or next(iter(medidas.values()))
    print("\nSecciones (claro_1360):" if "claro_1360" in medidas else "\nSecciones:")
    for s in ref["secciones"]:
        print(f"  {s['alto']:>6} px  {s['etiqueta']}")


if __name__ == "__main__":
    main()
