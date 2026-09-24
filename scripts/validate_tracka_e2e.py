#!/usr/bin/env python3
"""Validacion E2E Track A — drill-down de pliegos con LLM real.

Ejecutar desde Mac (local):

    cd /path/to/tender-opportunity-tracker
    python3 scripts/validate_tracka_e2e.py

Valida:
  1. Pipeline completo: Phase B con Haiku (triage) + Sonnet (analysis) reales
  2. Matriz de riesgos: estructura valida (items, categorias, severidades)
  3. Cuota: libro mayor actualizado con gasto real por modelo
  4. Persistencia: estado en JSONL via guardar_estado
  5. RAM y limpieza: peak RSS < 400 MB, PDFs borrados tras limpiar_pliegos

Imprime un resumen JSON a stdout.
"""
import json
import logging
import os
import platform
import resource
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import fitz  # PyMuPDF

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from dotenv import load_dotenv

load_dotenv(REPO_ROOT / ".env")

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("validate_tracka_e2e")

_TZ_SV = timezone(timedelta(hours=-6))
PROCESO_REF = "E2E-TRACKA-001"
CLIENT_ID = "e2e_tracka"


# -- PDF sintetico -----------------------------------------------------------

_PLIEGO_TEXTO = """BASES DE LICITACION PUBLICA LP-2026-INFRA-001

OBJETO DE LA CONTRATACION

El Ministerio de Hacienda de la Republica de El Salvador, a traves de la Unidad de
Adquisiciones y Contrataciones Institucional (UACI), invita a participar en el proceso
de Licitacion Publica para la adquisicion de servidores tipo rack de alto rendimiento,
sistemas de almacenamiento SAN (Storage Area Network), switches de red de core y
distribucion, y licenciamiento de plataforma de virtualizacion para el Centro de Datos
Institucional.

La necesidad surge del plan de modernizacion tecnologica aprobado en el ejercicio fiscal
2026, que contempla la renovacion de la infraestructura de computo central para soportar
los sistemas criticos de administracion tributaria, presupuesto y tesoreria del Estado.

El monto estimado de la contratacion asciende a USD $450,000.00 (cuatrocientos cincuenta
mil dolares de los Estados Unidos de America), financiado con fondos GOES del ejercicio
fiscal 2026.

ESPECIFICACIONES TECNICAS

1. SERVIDORES TIPO RACK (Cantidad: 4 unidades)
   - Procesador: Minimo 2x Intel Xeon Gold 6400 Series o AMD EPYC 9004 Series
   - Memoria RAM: 512 GB DDR5 ECC RDIMM por servidor, expandible a 2 TB
   - Almacenamiento interno: 2x SSD NVMe 960 GB para sistema operativo (RAID 1)
   - Controladora de red: 4x puertos 25GbE SFP28 + 2x puertos 1GbE RJ45
   - Factor de forma: 2U rack-mount, rieles deslizables incluidos
   - Fuentes de alimentacion: redundantes hot-swap 1400W 80 Plus Titanium
   - Sistema de gestion remota: iDRAC/iLO con licencia Enterprise incluida
   - Compatibilidad: Certificados para VMware vSphere 8.x y RHEL 9.x

   Las marcas de referencia (sin restringir) incluyen: Dell PowerEdge R760,
   HPE ProLiant DL380 Gen11, Lenovo ThinkSystem SR650 V3 o equivalente funcional
   que cumpla con todas las especificaciones tecnicas minimas indicadas.

2. ALMACENAMIENTO SAN (1 sistema)
   - Capacidad util: Minimo 100 TB con tecnologia NVMe all-flash
   - Conectividad: 4x puertos FC 32 Gbps por controladora (dual controladora activa-activa)
   - Proteccion de datos: Snapshots ilimitados, replicacion sincrona, deduplicacion inline
   - IOPS: Minimo 200,000 IOPS lectura aleatoria 4K
   - Latencia: Menor a 1 ms en operaciones de lectura aleatoria
   - Marcas de referencia: Dell PowerStore, NetApp AFF A-Series, HPE Alletra o equivalente

3. SWITCHES DE RED (Core: 2 unidades, Distribucion: 8 unidades)
   - Core: Multicapa L3, minimo 48x 10GbE SFP+ y 6x 40GbE QSFP+
   - Distribucion: 48x 1GbE RJ45 + 4x 10GbE SFP+ uplinks
   - QoS: 8 colas por puerto, clasificacion DSCP
   - Marcas de referencia: Cisco Catalyst 9000 Series, Aruba CX, Juniper EX

4. LICENCIAMIENTO DE VIRTUALIZACION
   - Plataforma: VMware vSphere Enterprise Plus 8.x o equivalente funcional
   - Licencias para 4 servidores (licenciamiento por socket, 2 sockets por servidor)
   - Soporte y actualizaciones por 3 anios (Production Support 24/7)

CONDICIONES GENERALES

Plazo de entrega: 45 dias calendario contados a partir de la emision de la orden de
compra. No se aceptaran entregas parciales sin autorizacion previa del administrador
del contrato.

Lugar de entrega: Centro de Datos del Ministerio de Hacienda, Boulevard de Los Heroes,
San Salvador, El Salvador. Se requiere que el contratista realice la instalacion fisica
y configuracion basica de todos los equipos, incluyendo rack, cableado estructurado,
conexion a la red electrica y puesta en marcha.

Forma de pago: Pago unico contra recepcion definitiva satisfactoria de todos los bienes
y servicios, dentro de los 30 dias habiles siguientes a la presentacion de la factura
con el acta de recepcion firmada.

GARANTIAS REQUERIDAS

- Garantia de cumplimiento de contrato: 10% del monto adjudicado, vigente durante
  la ejecucion del contrato mas 60 dias adicionales.
- Garantia de buena obra: 10% del monto adjudicado, vigente por 1 anio posterior
  a la recepcion definitiva.
- Garantia de los equipos: Minimo 3 anios on-site con tiempo de respuesta de 4 horas
  en horario habil (8x5 NBD para componentes no criticos).
- Disponibilidad de repuestos: garantizada por el fabricante por 7 anios desde la
  fecha de entrega.

PENALIDADES POR INCUMPLIMIENTO

El contratista que incumpla los plazos de entrega sera sancionado con una multa del
0.5% del valor total del contrato por cada dia calendario de atraso, hasta un maximo
del 10% del valor total. Superado este porcentaje, la institucion podra declarar la
caducidad del contrato con perdida de la garantia de cumplimiento.

La entrega parcial sin autorizacion previa del administrador del contrato generara
una multa adicional del 2% del valor de los bienes no entregados.

El incumplimiento de los niveles de servicio de la garantia (tiempo de respuesta
mayor a 4 horas para componentes criticos) sera penalizado con USD 200.00 por
cada incidente no atendido dentro del SLA pactado, descontado del siguiente pago
de mantenimiento.

CRITERIOS DE EVALUACION

Las ofertas seran evaluadas conforme al sistema de puntaje ponderado siguiente:
  - Precio: 40 puntos (la oferta mas baja obtiene el maximo)
  - Especificaciones tecnicas: 35 puntos (cumplimiento y superacion)
  - Experiencia del oferente: 15 puntos (contratos similares en los ultimos 5 anios)
  - Garantia y soporte: 10 puntos (extension mas alla del minimo)

Se requiere que el oferente acredite al menos 3 contratos similares ejecutados
satisfactoriamente en los ultimos 5 anios con instituciones publicas o privadas
en Centroamerica, con un monto individual no menor a USD $200,000.00.

DOCUMENTOS LEGALES REQUERIDOS

- Solvencia de la DGII vigente
- Solvencia del ISSS y AFP
- Constancia de estar inscrito en el Registro de Oferentes de la UACI
- Poder legal del representante del oferente
- NIT y NRC de la empresa
- Estados financieros auditados de los ultimos 2 ejercicios fiscales
"""


def _crear_pdf_sintetico(destino: str) -> str:
    """Genera un PDF de ~10 paginas con texto de pliego de infraestructura TI."""
    doc = fitz.open()

    # Partir el texto en bloques para distribuir en paginas
    lineas = _PLIEGO_TEXTO.strip().split("\n")
    lineas_por_pagina = max(1, len(lineas) // 10)

    for i in range(0, len(lineas), lineas_por_pagina):
        bloque = "\n".join(lineas[i : i + lineas_por_pagina])
        page = doc.new_page(width=612, height=792)  # Letter
        rect = fitz.Rect(50, 50, 562, 742)
        page.insert_textbox(
            rect,
            bloque,
            fontsize=10,
            fontname="helv",
        )

    doc.save(destino)
    n = doc.page_count
    doc.close()
    logger.info("PDF sintetico creado: %s (%d paginas)", destino, n)
    return destino


# -- Config minimo ------------------------------------------------------------

def _make_cfg(
    tmp_dir: str,
    tmp_data_dir: str,
    *,
    proveedor: str = "anthropic",
    familia: str = "nativo",
) -> SimpleNamespace:
    """Config minima del E2E, parametrizada por proveedor y familia.

    **Por que los IDs de modelo ya no se escriben aca.** Hasta la Fase 9 este
    script fijaba `model_triage="claude-haiku-4-5-20251001"` y
    `model_analysis="claude-sonnet-4-6"` como literales, y ambos DIFERIAN de los
    defaults de `config.py`. O sea que la validacion end-to-end certificaba una
    combinacion de modelos que produccion no usaba. Ahora los resuelve
    `src.llm.modelos`, la misma tabla que usa el pipeline: si el E2E pasa, pasa
    con lo que de verdad corre.

    La credencial que se exige depende del proveedor ACTIVO: con `oci` no hace
    falta `ANTHROPIC_API_KEY`, que es justamente el punto de la abstraccion.
    """
    return SimpleNamespace(
        enable_drilldown=True,
        enable_memoria=False,
        # -- Proveedor de LLM --
        llm_provider=proveedor,
        oci_familia=familia,
        anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY", ""),
        # Vacios a proposito: manda la tabla de `src.llm.modelos`.
        model_triage="", model_analysis="", model_classifier="", model_trainer="",
        oci_model_triage="", oci_model_analysis="",
        oci_model_classifier="", oci_model_trainer="",
        # -- Credenciales de OCI (solo se usan con proveedor="oci") --
        oci_config_file=os.environ.get(
            "OCI_CONFIG_FILE", os.path.expanduser("~/.oci/config")),
        oci_config_profile=os.environ.get("OCI_CONFIG_PROFILE", "MULTICOMPUTOS"),
        oci_key_content=os.environ.get("OCI_KEY_CONTENT", ""),
        oci_user_ocid=os.environ.get("OCI_USER_OCID", ""),
        oci_tenancy_ocid=os.environ.get("OCI_TENANCY_OCID", ""),
        oci_fingerprint=os.environ.get("OCI_FINGERPRINT", ""),
        oci_region=os.environ.get("OCI_REGION", "us-chicago-1"),
        oci_genai_compartment_ocid=os.environ.get("OCI_GENAI_COMPARTMENT_OCID", ""),
        oci_max_tokens_salida=0,          # 0 = techo del modelo (ver modelos.TECHO_SALIDA)
        oci_analisis_raster_pages=20,
        oci_classifier_batch_size=0,      # 0 = derivado del techo
        llm_timeout_s=600,
        # -- Resto --
        pliego_max_mb=32,
        pliego_max_pages=100,
        triage_pages=15,
        triage_raster_pages=5,
        daily_budget_usd=10.0,
        analisis_tokens_por_pagina=3000,
        drilldown_tmp_dir=tmp_dir,
        budget_path=os.path.join(tmp_dir, "gasto_diario.json"),
        data_dir=tmp_data_dir,
    )


def _make_proceso():
    from src.scraper.models import Proceso
    return Proceso(
        institucion="Ministerio de Hacienda",
        descripcion="Adquisicion de servidores rack, almacenamiento SAN, switches de red y licenciamiento de virtualizacion",
        fecha_publicacion=datetime.now(_TZ_SV),
        url_directa=f"https://comprasal.gob.sv/test/{PROCESO_REF}",
        numero_referencia=PROCESO_REF,
        forma_contratacion="Licitacion Publica",
        tipo_actividad="Infraestructura",
        monto=450000.0,
        fecha_limite=datetime.now(_TZ_SV) + timedelta(days=45),
    )


# -- Tests --------------------------------------------------------------------

def test_pipeline_completo(cfg, pdf_path: str, proceso) -> dict:
    """Phase B completa con LLM real."""
    from src.drilldown.pipeline import analizar_pliego_proceso

    ctx_cliente = (
        "Cliente: MULTICOMPUTOS.\n"
        "Descripcion: Integrador de soluciones de tecnologia en El Salvador.\n"
        "Categorias del portafolio: Infraestructura (Dell, HPE, Lenovo), "
        "Ciberseguridad (Fortinet, Palo Alto), Redes (Cisco, Aruba).\n"
        "Contexto de mercado: Empresa con experiencia en licitaciones GOES de TI."
    )
    inclusion_signals = [
        "servidor, server, rack, almacenamiento, storage, SAN",
        "switch, router, firewall, red, network",
        "virtualizacion, VMware, vSphere, licenciamiento",
    ]
    exclusion_signals = [
        "alimentos, bebidas, limpieza, fumigacion",
    ]

    res = analizar_pliego_proceso(
        proceso, pdf_path, cfg, ctx_cliente, CLIENT_ID,
        inclusion_signals, exclusion_signals,
    )

    ok = (
        res.estado == "ANALIZADO"
        and res.triaje == "GO"
        and res.triaje_confianza >= 0.50
        and res.matriz is not None
    )

    return {
        "estado": res.estado,
        "triaje": res.triaje,
        "triaje_confianza": res.triaje_confianza,
        "tiene_matriz": res.matriz is not None,
        "error": res.error or "",
        "resultado": res,  # carry forward for other tests
        "pass": ok,
    }


def test_matriz_valida(resultado) -> dict:
    """Estructura de MatrizRiesgos."""
    matriz = resultado.matriz
    if matriz is None:
        return {"pass": False, "error": "matriz is None"}

    items = matriz.items
    categorias_ok = all(
        it.categoria in {"financiero", "tecnico", "legal", "plazos"}
        for it in items
    )
    severidades_ok = all(
        it.severidad in {"alta", "media", "baja"}
        for it in items
    )
    alguna_clausula = any(it.clausula_ref.strip() for it in items) if items else False
    resumen_ok = bool(matriz.resumen.strip())
    tiene_items = len(items) >= 1

    ok = tiene_items and categorias_ok and severidades_ok and alguna_clausula and resumen_ok

    return {
        "n_items": len(items),
        "categorias_ok": categorias_ok,
        "severidades_ok": severidades_ok,
        "alguna_clausula_ref": alguna_clausula,
        "resumen_no_vacio": resumen_ok,
        "pass": ok,
    }


def test_cuota(cfg) -> dict:
    """Libro mayor de gasto actualizado con consumo real."""
    budget_path = cfg.budget_path
    if not os.path.exists(budget_path):
        return {"pass": False, "error": "budget file not found"}

    with open(budget_path, "r") as f:
        libro = json.load(f)

    gasto = libro.get("gasto_usd", 0)
    por_modelo = libro.get("por_modelo", {})

    # Tiene que haber una entrada por cada modelo que el pipeline USO de verdad.
    #
    # Antes esto buscaba las subcadenas "haiku" y "sonnet": quedaba en rojo con
    # cualquier proveedor que no fuera Anthropic, y ademas era una asercion
    # floja —"algun modelo con haiku en el nombre"— que no verificaba que el
    # libro tarifara el modelo correcto. Ahora se resuelve contra la misma tabla
    # que usa el pipeline, asi que ademas atrapa el bug de tarifacion: si el
    # `UsoModelo` se construyera con el ID del proveedor equivocado, el libro
    # tendria una clave que no es esta y el test lo ve.
    from src.llm import modelos

    esperado_triaje = modelos.modelo_de(cfg, "triaje")
    esperado_analisis = modelos.modelo_de(cfg, "analisis")

    tiene_triage = por_modelo.get(esperado_triaje, {}).get("llamadas", 0) >= 1
    tiene_analysis = por_modelo.get(esperado_analisis, {}).get("llamadas", 0) >= 1

    ok = gasto > 0 and tiene_triage and tiene_analysis

    return {
        "gasto_usd": gasto,
        "modelos": list(por_modelo.keys()),
        "esperados": [esperado_triaje, esperado_analisis],
        "tiene_triage": tiene_triage,
        "tiene_analysis": tiene_analysis,
        "pass": ok,
    }


def test_persistencia(cfg) -> dict:
    """Estado en JSONL via guardar_estado."""
    from src import history

    estado = history.analisis_estado(CLIENT_ID, PROCESO_REF, cfg.data_dir)
    jsonl_path = os.path.join(cfg.data_dir, "history", f"{CLIENT_ID}.analisis.jsonl")
    archivo_existe = os.path.exists(jsonl_path)

    ok = estado == "ANALIZADO" and archivo_existe

    return {
        "estado_jsonl": estado,
        "archivo_existe": archivo_existe,
        "pass": ok,
    }


def test_ram_y_limpieza(cfg, pdf_path: str) -> dict:
    """Peak RSS < 400 MB + PDFs borrados tras limpiar_pliegos."""
    from src.drilldown.pipeline import limpiar_pliegos

    # Medir RAM
    peak_raw = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if platform.system() == "Darwin":
        peak_mb = peak_raw / (1024 * 1024)  # macOS: bytes
    else:
        peak_mb = peak_raw / 1024            # Linux: KB

    ram_ok = peak_mb < 400

    # Limpiar PDFs
    limpiar_pliegos({PROCESO_REF: pdf_path})
    pdf_borrado = not os.path.exists(pdf_path)

    # Verificar que no quedan PDFs en tmp_dir
    pdfs_restantes = [
        f for f in os.listdir(cfg.drilldown_tmp_dir)
        if f.endswith(".pdf")
    ] if os.path.isdir(cfg.drilldown_tmp_dir) else []

    ok = ram_ok and pdf_borrado and len(pdfs_restantes) == 0

    return {
        "peak_rss_mb": round(peak_mb, 1),
        "limit_mb": 400,
        "ram_ok": ram_ok,
        "pdf_borrado": pdf_borrado,
        "pdfs_restantes": len(pdfs_restantes),
        "pass": ok,
    }


# -- Cleanup ------------------------------------------------------------------

def _cleanup_jsonl(data_dir: str):
    path = Path(data_dir) / "history" / f"{CLIENT_ID}.analisis.jsonl"
    if path.exists():
        path.unlink()
        logger.info("Cleanup: eliminado %s", path)


# -- Main ---------------------------------------------------------------------

def main():
    import argparse

    from src import llm
    from src.llm import modelos

    p = argparse.ArgumentParser(
        description="Validacion end-to-end de Track A, por proveedor y familia.")
    p.add_argument("--proveedor", default=os.getenv("LLM_PROVIDER", "anthropic"),
                   choices=list(modelos.PROVEEDORES),
                   help="anthropic (Claude via API) u oci (OCI Generative AI).")
    p.add_argument("--familia", default=os.getenv("OCI_FAMILIA", "nativo"),
                   choices=list(modelos.FAMILIAS),
                   help="Familia de modelos dentro de OCI. Ignorada con --proveedor anthropic.")
    args = p.parse_args()

    with tempfile.TemporaryDirectory(prefix="e2e_tracka_") as tmp_dir:
        tmp_data_dir = os.path.join(tmp_dir, "data")
        os.makedirs(tmp_data_dir, exist_ok=True)

        cfg = _make_cfg(tmp_dir, tmp_data_dir,
                        proveedor=args.proveedor, familia=args.familia)

        # Falla en 200 ms si la credencial del proveedor ACTIVO no esta, en vez
        # de hacerlo a mitad del pipeline. Ademas dispara el WARNING de
        # gobernanza cuando la familia saca los pliegos de OCI.
        llm.validar_configuracion(cfg)

        logger.info(
            "Proveedor=%s familia=%s | triaje=%s | analisis=%s (techo %s tokens)",
            args.proveedor,
            args.familia if args.proveedor == "oci" else "-",
            modelos.modelo_de(cfg, "triaje"),
            modelos.modelo_de(cfg, "analisis"),
            modelos.max_tokens_de(cfg, "analisis", 16000),
        )
        proceso = _make_proceso()
        results = {}
        overall_pass = True

        # Generar PDF sintetico
        pdf_path = os.path.join(tmp_dir, "pliego_test.pdf")
        _crear_pdf_sintetico(pdf_path)

        try:
            logger.info("=== 1/5 Pipeline completo (triaje + analisis) ===")
            r = test_pipeline_completo(cfg, pdf_path, proceso)
            resultado = r.pop("resultado")  # no serializar el dataclass completo
            results["pipeline_completo"] = r
            overall_pass = overall_pass and r["pass"]

            if r["pass"]:
                logger.info("=== 2/5 Matriz valida ===")
                r2 = test_matriz_valida(resultado)
                results["matriz_valida"] = r2
                overall_pass = overall_pass and r2["pass"]

                logger.info("=== 3/5 Cuota ===")
                r3 = test_cuota(cfg)
                results["cuota"] = r3
                overall_pass = overall_pass and r3["pass"]

                logger.info("=== 4/5 Persistencia ===")
                r4 = test_persistencia(cfg)
                results["persistencia"] = r4
                overall_pass = overall_pass and r4["pass"]
            else:
                logger.error("Pipeline no llego a ANALIZADO; saltando tests 2-4.")
                results["matriz_valida"] = {"pass": False, "skipped": True}
                results["cuota"] = {"pass": False, "skipped": True}
                results["persistencia"] = {"pass": False, "skipped": True}
                overall_pass = False

            logger.info("=== 5/5 RAM y limpieza ===")
            r5 = test_ram_y_limpieza(cfg, pdf_path)
            results["ram_y_limpieza"] = r5
            overall_pass = overall_pass and r5["pass"]

        finally:
            logger.info("=== Cleanup ===")
            _cleanup_jsonl(tmp_data_dir)

        results["all_pass"] = overall_pass
        print(json.dumps(results, indent=2, default=str))
        return 0 if overall_pass else 1


if __name__ == "__main__":
    sys.exit(main())
