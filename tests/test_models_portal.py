"""Campos de portal en `Proceso` (docs/arquitectura/portales.md §3 y §4): COMPRASAL no cambia."""
from __future__ import annotations

import dataclasses
import json
import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from src.scraper.models import (
    MOTIVO_MODALIDAD_NO_COMPETIBLE,
    MOTIVOS_BLOQUEO,
    Proceso,
    ProcesoClasificado,
)

_FECHA = datetime(2026, 11, 20, 8, 0, tzinfo=ZoneInfo("America/El_Salvador"))
_NUEVOS = ("portal_id", "moneda", "montos_por_moneda", "monto_usd", "id_externo",
           "modalidad_codigo", "detalle", "datos_portal")


def test_proceso_viejo_se_construye_igual_y_es_de_comprasal():
    p = Proceso("Inst", "Compra de servidores", _FECHA, "https://x", "96-2026-P0011",
                "Licitación", "Bienes", 45000.0, None)
    assert (p.portal_id, p.moneda, p.montos_por_moneda, p.monto_usd) == ("comprasal_sv", "", {}, None)
    assert (p.id_externo, p.modalidad_codigo, p.detalle, p.datos_portal) == ("", "", "", {})
    assert p.proceso_gid == "comprasal_sv:96-2026-P0011"


def test_los_campos_viejos_conservan_su_orden_y_los_nuevos_van_al_final():
    nombres = [f.name for f in dataclasses.fields(Proceso)]
    assert nombres[:9] == ["institucion", "descripcion", "fecha_publicacion", "url_directa", "numero_referencia",
                           "forma_contratacion", "tipo_actividad", "monto", "fecha_limite"]
    assert tuple(nombres[9:]) == _NUEVOS


def test_los_dicts_por_defecto_no_se_comparten():
    a, b = Proceso("i", "d", _FECHA, "u"), Proceso("i", "d", _FECHA, "u")
    a.datos_portal["x"] = 1
    a.montos_por_moneda["CRC"] = 1.0
    assert b.datos_portal == {} and b.montos_por_moneda == {}


def test_proceso_gid_es_propiedad_y_asdict_no_la_serializa():
    p = Proceso("i", "d", _FECHA, "u", "2026LD-000364-0015499999", portal_id="sicop_cr")
    assert p.proceso_gid == "sicop_cr:2026LD-000364-0015499999"
    assert "proceso_gid" not in dataclasses.asdict(p)


def _con_todo() -> Proceso:
    """Un Proceso con un valor NO default en cada campo, generado desde `fields` (detecta olvidos)."""
    valores = {}
    for f in dataclasses.fields(Proceso):
        if f.name in ("fecha_publicacion", "fecha_limite"):
            valores[f.name] = _FECHA
        elif f.name in ("monto", "monto_usd"):
            valores[f.name] = 123.0
        elif f.name == "montos_por_moneda":
            valores[f.name] = {"CRC": 1.0, "USD": 2.0}
        elif f.name == "datos_portal":
            valores[f.name] = {"cartel_seq": "0", "unspsc": ["43211500"]}
        else:
            valores[f.name] = f"valor-{f.name}"
    return Proceso(**valores)


def test_from_proceso_copia_todos_los_campos():
    origen = _con_todo()
    copia = ProcesoClasificado.from_proceso(origen)
    for f in dataclasses.fields(Proceso):
        assert getattr(copia, f.name) == getattr(origen, f.name), f.name
    assert copia.proceso_gid == origen.proceso_gid
    # Los campos de clasificación arrancan en su default.
    assert copia.es_relevante is False and copia.tags == [] and copia.marcas_recomendadas == []


def test_from_proceso_no_comparte_los_dicts():
    origen = _con_todo()
    copia = ProcesoClasificado.from_proceso(origen)
    copia.datos_portal["nuevo"] = 1
    copia.montos_por_moneda["EUR"] = 3.0
    assert "nuevo" not in origen.datos_portal and "EUR" not in origen.montos_por_moneda


def test_from_proceso_de_un_proceso_viejo_de_comprasal():
    viejo = Proceso("Inst", "Desc", _FECHA, "https://x", "96-2026-P0011", "Licitación", "Bienes", None, _FECHA)
    c = ProcesoClasificado.from_proceso(viejo)
    assert (c.numero_referencia, c.forma_contratacion, c.tipo_actividad, c.fecha_limite) == (
        "96-2026-P0011", "Licitación", "Bienes", _FECHA)
    assert c.portal_id == "comprasal_sv" and c.moneda == ""


def test_motivo_de_modalidad_no_competible():
    assert MOTIVO_MODALIDAD_NO_COMPETIBLE == "modalidad_no_competible"
    assert MOTIVOS_BLOQUEO[MOTIVO_MODALIDAD_NO_COMPETIBLE] == (
        "Modalidad sin competencia abierta (excepción, remate o equivalente)")


# ── Formatos de referencia disjuntos (§4): por eso v1 no se prefija ──────────

_RE_COMPRASAL = re.compile(r"\d+-\d{4}-[A-Z]\d+")
_RE_SICOP = re.compile(r"\d{4}[A-Z]{2}-\d{6}-\d{10}")


def _referencias_sicop() -> list[str]:
    ruta = Path(__file__).parent / "fixtures" / "sicop" / "epCartel_searchEpCartels_pagina.json"
    return [fila["instCartelNo"] for fila in json.loads(ruta.read_text(encoding="utf-8"))["content"]]


def test_las_referencias_de_ambos_portales_no_colisionan():
    comprasal = ["96-2026-P0011", "1000000005-2026-P0039", "300-2026-P0168", "0000-2099-P0001"]
    sicop = _referencias_sicop() + ["2026LD-000364-0015499999", "2026XE-000012-0001102101"]
    assert sicop
    for ref in comprasal:
        assert _RE_COMPRASAL.fullmatch(ref) and not _RE_SICOP.fullmatch(ref), ref
    for ref in sicop:
        assert _RE_SICOP.fullmatch(ref) and not _RE_COMPRASAL.fullmatch(ref), ref
    gids = {Proceso("i", "d", _FECHA, "u", r, portal_id=pid).proceso_gid
            for pid, refs in (("comprasal_sv", comprasal), ("sicop_cr", sicop)) for r in refs}
    assert len(gids) == len(set(comprasal)) + len(set(sicop))
