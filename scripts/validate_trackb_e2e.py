#!/usr/bin/env python3
"""Validación E2E Track B — cadena completa contra ADB real.

Ejecutar en la VM vía SSH (no funciona desde Mac por TLS cert de DigiCert):

    cd /home/opc/tender-opportunity-tracker
    python3 scripts/validate_trackb_e2e.py

Valida:
  1. Full chain: insert 3 procesos → vectorize → search precedentes
  2. Semantic discrimination: infra query > cleaning
  3. RAM measurement: peak RSS < 400 MB
  4. Dual-write: ADB + JSONL consistent
  5. Facade isolation: service.obtener_precedentes → list[Precedente]
  6. Cleanup: no test data remains

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

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from dotenv import load_dotenv
load_dotenv(REPO_ROOT / ".env")

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("validate_trackb_e2e")

_TZ_SV = timezone(timedelta(hours=-6))
E2E_IDS = ["E2E-INFRA-001", "E2E-RED-002", "E2E-LIMPIEZA-003"]
CLIENT_ID = "e2e_test"


# ── Config mínimo ─────────────────────────────────────────────────────────────

def _make_cfg(tmp_data_dir: str) -> SimpleNamespace:
    """Config con solo los campos que necesita Track B, leídos de .env."""
    return SimpleNamespace(
        enable_memoria=True,
        adb_user=os.environ["ADB_USER"],
        adb_password=os.environ["ADB_PASSWORD"],
        adb_dsn=os.environ["ADB_DSN"],
        embedding_model_name=os.getenv("EMBEDDING_MODEL_NAME", "MULTILINGUAL_E5_SMALL"),
        chunk_tokens=int(os.getenv("CHUNK_TOKENS", "800")),
        chunk_overlap=int(os.getenv("CHUNK_OVERLAP", "100")),
        rag_top_k=int(os.getenv("RAG_TOP_K", "5")),
        precedentes_max=int(os.getenv("PRECEDENTES_MAX", "3")),
        data_dir=tmp_data_dir,
    )


# ── Datos sintéticos (~4000 chars c/u) ────────────────────────────────────────

_TEXTO_INFRA = """
OBJETO DE LA CONTRATACIÓN

El Ministerio de Hacienda de la República de El Salvador, a través de la Unidad de
Adquisiciones y Contrataciones Institucional (UACI), invita a participar en el proceso
de Licitación Pública para la adquisición de servidores tipo rack de alto rendimiento,
sistemas de almacenamiento SAN (Storage Area Network) y licenciamiento de plataforma
de virtualización para el Centro de Datos Institucional.

La necesidad surge del plan de modernización tecnológica aprobado en el ejercicio fiscal
2026, que contempla la renovación de la infraestructura de cómputo central para soportar
los sistemas críticos de administración tributaria, presupuesto y tesorería del Estado.

ESPECIFICACIONES TÉCNICAS

1. SERVIDORES TIPO RACK (Cantidad: 4 unidades)
   - Procesador: Mínimo 2x Intel Xeon Gold 6400 Series o AMD EPYC 9004 Series
   - Memoria RAM: 512 GB DDR5 ECC RDIMM por servidor, expandible a 2 TB
   - Almacenamiento interno: 2x SSD NVMe 960 GB para sistema operativo (RAID 1)
   - Controladora de red: 4x puertos 25GbE SFP28 + 2x puertos 1GbE RJ45
   - Factor de forma: 2U rack-mount, rieles deslizables incluidos
   - Fuentes de alimentación: redundantes hot-swap 1400W 80 Plus Titanium
   - Sistema de gestión remota: iDRAC/iLO con licencia Enterprise incluida
   - Compatibilidad: Certificados para VMware vSphere 8.x y Red Hat Enterprise Linux 9.x

2. ALMACENAMIENTO SAN (1 sistema)
   - Capacidad útil: Mínimo 100 TB con discos SAS 15K RPM o NVMe all-flash
   - Conectividad: 4x puertos FC 32 Gbps por controladora (dual controladora activa-activa)
   - Protección de datos: Snapshots ilimitados, replicación síncrona, deduplicación inline
   - IOPS: Mínimo 200,000 IOPS lectura aleatoria 4K (all-flash) o 50,000 IOPS (híbrido)
   - Latencia: Menor a 1 ms en operaciones de lectura aleatoria (configuración all-flash)

3. LICENCIAMIENTO DE VIRTUALIZACIÓN
   - Plataforma: VMware vSphere Enterprise Plus 8.x o equivalente funcional
   - Licencias para los 4 servidores (licenciamiento por socket, 2 sockets por servidor)
   - Soporte y actualizaciones por 3 años (Production Support 24/7)

PENALIDADES POR INCUMPLIMIENTO

El contratista que incumpla los plazos de entrega será sancionado con una multa del
0.5% del valor total del contrato por cada día calendario de atraso, hasta un máximo
del 10% del valor total. Superado este porcentaje, la institución podrá declarar la
caducidad del contrato con pérdida de la garantía de cumplimiento.

La entrega parcial sin autorización previa del administrador del contrato generará
una multa adicional del 2% del valor de los bienes no entregados.

GARANTÍAS REQUERIDAS

- Garantía de cumplimiento de contrato: 10% del monto adjudicado, vigente durante
  la ejecución del contrato más 60 días adicionales
- Garantía de buena obra: 10% del monto adjudicado, vigente por 1 año posterior
  a la recepción definitiva
- Garantía de los equipos: Mínimo 3 años on-site con tiempo de respuesta de 4 horas
  en horario hábil (8x5 NBD para componentes no críticos)
- Disponibilidad de repuestos: garantizada por el fabricante por 7 años desde la
  fecha de entrega
"""

_TEXTO_RED = """
OBJETO DE LA CONTRATACIÓN

El Ministerio de Salud de la República de El Salvador requiere la adquisición de
equipos de red (switches de core, distribución y acceso), firewall de nueva generación
y access points inalámbricos para la modernización de la infraestructura de red de las
oficinas centrales y cinco hospitales nacionales de la red pública de salud.

Este proyecto forma parte del Programa de Transformación Digital del Sector Salud,
financiado con fondos del préstamo BID ES-L1234, y busca garantizar la conectividad
segura y de alta disponibilidad para los sistemas de expediente clínico electrónico,
telemedicina y gestión hospitalaria que se implementarán durante 2026-2027.

ESPECIFICACIONES TÉCNICAS

1. SWITCHES DE CORE (Cantidad: 2 unidades)
   - Tipo: Multicapa L3, con capacidad de routing dinámico OSPF, BGP, VRRP
   - Puertos: Mínimo 48x 10GbE SFP+ y 6x 40GbE QSFP+ (uplinks)
   - Throughput: Mínimo 1.2 Tbps de capacidad de switching (wire-speed)
   - Redundancia: Fuentes de alimentación y ventiladores hot-swap redundantes
   - Stacking: Capacidad de virtualización en un solo dominio lógico
   - QoS: 8 colas por puerto, clasificación por DSCP, rate limiting granular

2. SWITCHES DE ACCESO (Cantidad: 40 unidades)
   - Puertos: 48x 1GbE RJ45 PoE+ (802.3at) + 4x 10GbE SFP+ uplinks
   - PoE Budget: Mínimo 740W por switch
   - Seguridad: 802.1X, DHCP snooping, dynamic ARP inspection, port security

3. FIREWALL DE NUEVA GENERACIÓN (Cantidad: 2, en clúster HA activo-pasivo)
   - Throughput de firewall: Mínimo 20 Gbps (tráfico real con todas las funciones habilitadas)
   - Throughput de IPS: Mínimo 8 Gbps con firmas activas
   - Funcionalidades: Application control, URL filtering, anti-malware, SSL inspection
   - VPN: Site-to-site IPSec (mínimo 50 túneles) y SSL VPN (mínimo 500 usuarios)
   - Gestión centralizada: Consola web con dashboard de amenazas en tiempo real

4. ACCESS POINTS (Cantidad: 120 unidades)
   - Estándar: Wi-Fi 6E (802.11ax), triple radio (2.4 GHz + 5 GHz + 6 GHz)
   - Cobertura: Interior, montaje en techo con kit de instalación incluido
   - PoE: Alimentación 802.3at (PoE+) desde el switch de acceso
   - Controladora: Gestión cloud o on-premises, con licencia por 3 años

PENALIDADES

El incumplimiento en los plazos de entrega dará lugar a una multa del 1% diario del
valor de los bienes no entregados, hasta un máximo del 15% del valor total del contrato.
La falta de disponibilidad de soporte técnico durante el período de garantía generará
una multa adicional de USD 500.00 por cada incidente no atendido dentro del SLA pactado.

GARANTÍAS

- Garantía de equipos de red (switches y APs): Mínimo 5 años con reemplazo advance (NBD)
- Garantía de firewall: 3 años con actualizaciones de firmware y firmas de seguridad
- Soporte técnico: 24x7 para equipos críticos (core switches y firewall)
- Transferencia de conocimiento: 40 horas de capacitación al personal de TI del Ministerio
"""

_TEXTO_LIMPIEZA = """
OBJETO DE LA CONTRATACIÓN

La Asamblea Legislativa de la República de El Salvador solicita ofertas para la
contratación de servicios de limpieza y mantenimiento general de las instalaciones
del edificio principal y sus tres edificios anexos, incluyendo todas las áreas comunes,
oficinas administrativas, salones de sesiones plenarias y de comisiones, servicios
sanitarios, comedores, estacionamientos techados y áreas exteriores ajardinadas.

El contrato tiene como propósito mantener las instalaciones en condiciones óptimas
de higiene y presentación, acorde con la dignidad institucional del primer órgano del
Estado, y en cumplimiento de las normativas sanitarias vigentes emitidas por el
Ministerio de Salud.

ESPECIFICACIONES DEL SERVICIO

1. LIMPIEZA GENERAL DIARIA
   - Frecuencia: De lunes a viernes, en horario de 5:00 AM a 7:00 AM (pre-jornada)
     y de 12:00 PM a 1:00 PM (mantenimiento de medio día)
   - Áreas cubiertas: 15,000 m2 de oficinas, 3,000 m2 de áreas comunes, 2,000 m2
     de áreas exteriores, 1,500 m2 de estacionamientos techados
   - Personal mínimo: 25 operarios de limpieza + 2 supervisores de turno
   - Actividades: Barrido, trapeado, aspirado, desinfección de superficies de contacto,
     limpieza de vidrios interiores, recolección y clasificación de residuos sólidos

2. LIMPIEZA PROFUNDA MENSUAL
   - Lavado de alfombras con máquina extractora
   - Cristalizado y encerado de pisos de mármol y granito
   - Limpieza de vidrios exteriores (hasta 4to nivel, con equipo de altura certificado)
   - Desinfección profunda de servicios sanitarios y comedores

3. INSUMOS Y MATERIALES
   - El contratista proveerá todos los insumos: detergentes biodegradables, desinfectantes
     con registro sanitario, papel higiénico jumbo, jabón líquido antibacterial, bolsas de
     basura clasificadas (orgánico, reciclable, común), ambientadores
   - Reporte mensual de inventario y consumo de insumos
   - Fichas técnicas y hojas de seguridad (MSDS) de todos los productos químicos

4. FUMIGACIÓN Y CONTROL DE PLAGAS
   - Frecuencia: Mensual para áreas interiores, quincenal para áreas exteriores y jardines
   - Productos: Biodegradables y de baja toxicidad, aprobados por el MARN
   - Certificación: Empresa fumigadora con registro vigente del Ministerio de Salud

PENALIDADES

El incumplimiento parcial del servicio se sancionará con deducciones proporcionales
al porcentaje de áreas no atendidas, calculadas sobre el monto mensual del contrato.
La acumulación de 3 incumplimientos graves (áreas no atendidas por más de 24 horas)
en un trimestre facultará a la institución para rescindir el contrato sin responsabilidad.
La ausencia no justificada de personal será penalizada con USD 25.00 por operario por día.

GARANTÍAS

- Garantía de cumplimiento: 5% del monto total anual del contrato
- Póliza de seguro de responsabilidad civil: USD 50,000 por evento
- Póliza de riesgos laborales: cobertura completa para todo el personal asignado
- Vigencia del contrato: 12 meses con opción de prórroga por 6 meses adicionales,
  sujeta a evaluación satisfactoria de desempeño
"""


def _make_proceso(ref: str, institucion: str, descripcion: str):
    """Objeto compatible con Proceso (SimpleNamespace con los mismos atributos)."""
    return SimpleNamespace(
        numero_referencia=ref,
        institucion=institucion,
        descripcion=descripcion,
        fecha_publicacion=datetime.now(_TZ_SV),
        url_directa=f"https://comprasal.gob.sv/test/{ref}",
        forma_contratacion="Licitación Pública",
        tipo_actividad="Infraestructura",
        monto=250000.0,
        fecha_limite=datetime.now(_TZ_SV) + timedelta(days=30),
    )


def _make_resultado(ref: str, estado: str = "ANALIZADO"):
    """ResultadoAnalisis real (dataclass) para compatibilidad con history.save_analisis."""
    from src.scraper.models import ResultadoAnalisis
    return ResultadoAnalisis(
        proceso_uuid=ref,
        estado=estado,
        formato="NATIVO",
        triaje="GO",
        triaje_confianza=0.90,
        modelo_analisis="e2e-test",
        generado_en=datetime.now(_TZ_SV).isoformat(),
    )


# ── Cleanup ──────────────────────────────────────────────────────────────────

def _cleanup_db(cfg):
    from src.memoria import db
    with db.connection(cfg) as conn:
        cursor = conn.cursor()
        try:
            # FK order: children first
            for tabla in ["licitaciones_chunks", "licitaciones_analisis", "licitaciones_metadata"]:
                cursor.execute(
                    f"DELETE FROM {tabla} WHERE proceso_id LIKE :prefix",
                    {"prefix": "E2E-%"},
                )
                logger.info("Cleanup: %d filas eliminadas de %s", cursor.rowcount, tabla)
        finally:
            cursor.close()


def _cleanup_jsonl(data_dir: str):
    path = Path(data_dir) / "history" / f"{CLIENT_ID}.analisis.jsonl"
    if path.exists():
        path.unlink()
        logger.info("Cleanup: eliminado %s", path)


# ── Tests ─────────────────────────────────────────────────────────────────────

def test_full_chain(cfg) -> dict:
    """Insert 3 procesos, vectorize, search precedentes."""
    from src.memoria import db, rag, repo, vectorizer

    datos = [
        ("E2E-INFRA-001", "Ministerio de Hacienda",
         "Adquisición de servidores rack y almacenamiento SAN", _TEXTO_INFRA),
        ("E2E-RED-002", "Ministerio de Salud",
         "Equipos de red, firewall y access points", _TEXTO_RED),
        ("E2E-LIMPIEZA-003", "Asamblea Legislativa",
         "Servicio de limpieza de oficinas", _TEXTO_LIMPIEZA),
    ]

    chunks_por_proceso = {}
    with db.connection(cfg) as conn:
        for ref, inst, desc, texto in datos:
            proceso = _make_proceso(ref, inst, desc)
            resultado = _make_resultado(ref)
            repo.upsert_proceso(conn, proceso, CLIENT_ID, estado="ANALIZADO")
            repo.save_analisis_db(conn, resultado)
            n = vectorizer.vectorizar_proceso(conn, ref, texto, cfg)
            chunks_por_proceso[ref] = n
            logger.info("Insertado %s: %d chunks", ref, n)

    all_vectorized = all(n > 0 for n in chunks_por_proceso.values())

    with db.connection(cfg) as conn:
        resultados = rag.buscar_precedentes(
            conn, "servidores rack para data center", cfg,
            excluir_proceso_id="E2E-INFRA-001",
        )

    precedentes_found = len(resultados) > 0
    excluido = all(r["proceso_id"] != "E2E-INFRA-001" for r in resultados)

    return {
        "all_vectorized": all_vectorized,
        "chunks": chunks_por_proceso,
        "precedentes_found": precedentes_found,
        "precedentes_count": len(resultados),
        "current_excluded": excluido,
        "pass": all_vectorized and precedentes_found and excluido,
    }


def test_semantic_discrimination(cfg) -> dict:
    """Infra query should be closer to RED (networking) than LIMPIEZA (cleaning)."""
    from src.memoria import db, rag

    with db.connection(cfg) as conn:
        resultados = rag.buscar_precedentes(
            conn,
            "servidores rack para data center virtualización almacenamiento",
            cfg,
            excluir_proceso_id="E2E-INFRA-001",
            limite=10,
        )

    dist_red = next((r["distancia"] for r in resultados if r["proceso_id"] == "E2E-RED-002"), None)
    dist_limpieza = next((r["distancia"] for r in resultados if r["proceso_id"] == "E2E-LIMPIEZA-003"), None)

    discriminates = False
    if dist_red is not None and dist_limpieza is not None:
        discriminates = dist_red < dist_limpieza

    return {
        "distancia_red": float(dist_red) if dist_red is not None else None,
        "distancia_limpieza": float(dist_limpieza) if dist_limpieza is not None else None,
        "discriminates": discriminates,
        "pass": discriminates,
    }


def test_ram() -> dict:
    """Peak RSS should be < 400 MB (Python process alone)."""
    peak_raw = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if platform.system() == "Darwin":
        peak_mb = peak_raw / (1024 * 1024)  # macOS: bytes
    else:
        peak_mb = peak_raw / 1024            # Linux: KB

    return {
        "peak_rss_mb": round(peak_mb, 1),
        "limit_mb": 400,
        "within_limit": peak_mb < 400,
        "pass": peak_mb < 400,
    }


def test_dual_write(cfg) -> dict:
    """guardar_estado writes to both ADB and JSONL consistently."""
    from src.memoria import service, db, repo
    from src import history

    ref = "E2E-INFRA-001"
    resultado = _make_resultado(ref, estado="NOTIFICADO")

    service.guardar_estado(cfg, CLIENT_ID, resultado)

    with db.connection(cfg) as conn:
        estado_adb = repo.get_estado(conn, ref)

    estado_jsonl = history.analisis_estado(CLIENT_ID, ref, cfg.data_dir)

    both_match = estado_adb == "NOTIFICADO" and estado_jsonl == "NOTIFICADO"
    return {
        "estado_adb": estado_adb,
        "estado_jsonl": estado_jsonl,
        "both_match": both_match,
        "pass": both_match,
    }


def test_facade(cfg) -> dict:
    """service.obtener_precedentes returns list[Precedente] with valid fields."""
    from src.memoria import service
    from src.scraper.models import Precedente

    proceso = _make_proceso("E2E-RED-002", "Ministerio de Salud",
                            "Equipos de red, firewall y access points")
    resultado = _make_resultado("E2E-RED-002")

    precedentes = service.obtener_precedentes(cfg, proceso, resultado)

    is_list = isinstance(precedentes, list)
    all_precedente = all(isinstance(p, Precedente) for p in precedentes)
    fields_ok = all(
        p.proceso_id and isinstance(p.distancia, float) and p.distancia >= 0
        for p in precedentes
    )
    no_self = all(p.proceso_id != "E2E-RED-002" for p in precedentes)

    return {
        "is_list": is_list,
        "count": len(precedentes),
        "all_precedente_type": all_precedente,
        "fields_valid": fields_ok,
        "no_self_reference": no_self,
        "pass": is_list and all_precedente and fields_ok and no_self and len(precedentes) > 0,
    }


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    with tempfile.TemporaryDirectory(prefix="e2e_trackb_") as tmp_dir:
        cfg = _make_cfg(tmp_dir)
        results = {}
        overall_pass = True

        try:
            logger.info("=== Limpieza previa ===")
            _cleanup_db(cfg)

            logger.info("=== 1/5 Full chain ===")
            r = test_full_chain(cfg)
            results["full_chain"] = r
            overall_pass = overall_pass and r["pass"]

            logger.info("=== 2/5 Semantic discrimination ===")
            r = test_semantic_discrimination(cfg)
            results["semantic_discrimination"] = r
            overall_pass = overall_pass and r["pass"]

            logger.info("=== 3/5 RAM measurement ===")
            r = test_ram()
            results["ram"] = r
            overall_pass = overall_pass and r["pass"]

            logger.info("=== 4/5 Dual-write ===")
            r = test_dual_write(cfg)
            results["dual_write"] = r
            overall_pass = overall_pass and r["pass"]

            logger.info("=== 5/5 Facade isolation ===")
            r = test_facade(cfg)
            results["facade"] = r
            overall_pass = overall_pass and r["pass"]

        finally:
            logger.info("=== Cleanup final ===")
            try:
                _cleanup_db(cfg)
            except Exception as exc:
                logger.error("Cleanup DB falló: %s", exc)
            _cleanup_jsonl(tmp_dir)
            from src.memoria import db
            db.close_pool()

        results["all_pass"] = overall_pass
        print(json.dumps(results, indent=2, default=str))
        return 0 if overall_pass else 1


if __name__ == "__main__":
    sys.exit(main())
