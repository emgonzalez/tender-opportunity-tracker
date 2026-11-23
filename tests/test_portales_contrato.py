"""src.portales.contrato, modalidades y moneda (docs/arquitectura/portales.md §2, §7 y §9). Sin red."""
from __future__ import annotations

import asyncio
import dataclasses
from types import SimpleNamespace

import pytest

from src.portales import contrato, modalidades
from src.portales.modalidades import (
    COMPETIBLE,
    CONDICIONAL,
    NO_COMPETIBLE,
    competibilidad_de,
    modalidad_de,
)
from src.portales.moneda import formatear_monto, moneda_de, texto_monto

# ── Errores ─────────────────────────────────────────────────────────────────


def test_jerarquia_de_errores():
    assert issubclass(contrato.ErrorPortal, RuntimeError)
    for cls in (contrato.CredencialesFaltantes, contrato.SesionInvalida,
                contrato.PortalNoDisponible, contrato.ContratoRoto):
        assert issubclass(cls, contrato.ErrorPortal)
    assert issubclass(contrato.BloqueoAcceso, contrato.ContratoRoto)


def test_error_portal_lleva_portal_y_recorta_el_detalle():
    e = contrato.BloqueoAcceso("sicop_cr", "WAF", detalle="<html>" + "x" * 2000)
    assert e.portal_id == "sicop_cr" and e.mensaje == "WAF"
    assert len(e.detalle) == contrato.MAX_DETALLE
    assert "sicop_cr" in str(e) and "WAF" in str(e)
    assert contrato.ErrorPortal("p", "m").detalle == ""


def test_documento_pliego_defaults_e_inmutable():
    d = contrato.DocumentoPliego(nombre="cartel.pdf", url="https://x/1")
    assert (d.extension, d.tamano_bytes, d.categoria, d.puntaje) == ("", None, "", 0)
    with pytest.raises(dataclasses.FrozenInstanceError):
        d.puntaje = 3  # type: ignore[misc]


def test_un_transporte_falso_cumple_el_protocolo():
    class Falso:
        async def json(self, metodo, url, cuerpo=None):
            return {"metodo": metodo, "url": url, "cuerpo": cuerpo}

        async def binario(self, url, max_bytes):
            return b"%PDF"[:max_bytes]

    t = Falso()
    assert isinstance(t, contrato.TransporteJSON)
    assert asyncio.run(t.json("POST", "u", {"a": 1}))["cuerpo"] == {"a": 1}
    assert asyncio.run(t.binario("u", 2)) == b"%P"
    assert not isinstance(object(), contrato.TransporteJSON)


def test_un_localizador_falso_cumple_el_protocolo():
    class Loc:
        def __init__(self, config, portal):
            self.abierto = False

        async def __aenter__(self):
            self.abierto = True
            return self

        async def __aexit__(self, *exc):
            self.abierto = False

        async def completar(self, proceso):
            return None

        async def documentos(self, proceso):
            return [contrato.DocumentoPliego("a.pdf", "u", "pdf")]

        async def texto_base(self, proceso):
            return ""

        async def descargar(self, doc, destino_dir, max_bytes):
            return None

    async def usar():
        async with Loc(None, None) as loc:
            assert loc.abierto
            return await loc.documentos(None)

    assert isinstance(Loc(None, None), contrato.LocalizadorPliego)
    assert asyncio.run(usar())[0].extension == "pdf"


# ── Modalidades ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("codigo,esperado", [
    *[(c, COMPETIBLE) for c in ("LY", "LE", "LD", "SE", "LN", "LI", "LA")],
    *[(c, CONDICIONAL) for c in ("PE", "CD", "CE")],
    *[(c, NO_COMPETIBLE) for c in ("PX", "PP", "RE")],
    ("ZZ", CONDICIONAL),     # desconocido: que decida el pliego
    (" ld ", COMPETIBLE),    # tolera espacios y minúsculas
])
def test_competibilidad_sicop_por_codigo(codigo, esperado):
    assert competibilidad_de("sicop_cr", codigo) == esperado


def test_la_tabla_cubre_el_catalogo_bd01_de_la_fixture():
    import json
    from pathlib import Path
    ruta = Path(__file__).parent / "fixtures" / "sicop" / "coCommonCd_list_BD01.json"
    catalogo = {f["cd"]: f["cdNmEs"] for f in json.loads(ruta.read_text(encoding="utf-8"))}
    tabla = {m.codigo: m.nombre for m in modalidades.MODALIDADES if m.portal_id == "sicop_cr"}
    assert tabla == catalogo


def test_las_normas_estan_dentro_del_check_de_la_adb():
    assert {m.norm for m in modalidades.MODALIDADES} <= set(modalidades.NORMAS)
    assert len({(m.portal_id, m.codigo) for m in modalidades.MODALIDADES}) == len(modalidades.MODALIDADES)


def _regla_vieja_comprasal(forma: str) -> str:
    """La regla de `analysis.py` antes de A2a: forma sin «directa» ⇒ competible; si no, siguen las reglas."""
    from src.drilldown.analysis import _normalizar
    f = _normalizar(forma)
    return COMPETIBLE if f and "directa" not in f else CONDICIONAL


@pytest.mark.parametrize("forma", [
    "Licitación", "Licitación Pública", "Licitación Competitiva", "Subasta Inversa",
    "Comparación de Precios", "Contratación Directa", "CONTRATACIÓN DIRECTA", "Contratacion directa",
    "Contratación Directa por Urgencia", "Libre Gestión", "", "é", "f",
])
def test_comprasal_reproduce_la_regla_vieja(forma):
    assert competibilidad_de("comprasal_sv", "", forma) == _regla_vieja_comprasal(forma)


def test_sicop_sin_codigo_usa_el_nombre_de_la_modalidad():
    assert competibilidad_de("sicop_cr", "", "Procedimiento por excepción") == NO_COMPETIBLE
    assert competibilidad_de("sicop_cr", "", "LICITACIÓN REDUCIDA") == COMPETIBLE
    assert competibilidad_de("sicop_cr", "", "Contratación directa") == CONDICIONAL


def test_modalidad_de():
    px = modalidad_de(SimpleNamespace(portal_id="sicop_cr", modalidad_codigo="PX"))
    assert px.norm == "CONTRATACION_DIRECTA" and px.competibilidad == NO_COMPETIBLE
    assert modalidad_de(SimpleNamespace(portal_id="comprasal_sv", modalidad_codigo="")) is None
    assert modalidad_de(SimpleNamespace()) is None  # objeto viejo, sin campos de portal
    raro = modalidad_de(SimpleNamespace(portal_id="sicop_cr", modalidad_codigo="zz"))
    assert (raro.codigo, raro.nombre, raro.norm, raro.competibilidad) == ("ZZ", "ZZ", "OTRO", CONDICIONAL)


# ── Moneda ──────────────────────────────────────────────────────────────────


def _p(**kw):
    return SimpleNamespace(**kw)


def test_moneda_de():
    assert moneda_de(_p()) == "USD"                                  # línea vieja ⇒ COMPRASAL
    assert moneda_de(_p(portal_id="comprasal_sv", moneda="")) == "USD"
    assert moneda_de(_p(portal_id="sicop_cr", moneda="")) == ""      # dos monedas: no se asume
    assert moneda_de(_p(portal_id="sicop_cr", moneda="crc")) == "CRC"
    assert moneda_de(_p(portal_id="desconocido", moneda="")) == ""


@pytest.mark.parametrize("monto", [None, 0, 0.0, -5, "abc", True, float("nan"), float("inf")])
def test_formatear_monto_nunca_da_cero_ni_basura(monto):
    assert formatear_monto(monto, "USD") == ""


def test_formatear_monto():
    assert formatear_monto(45000, "USD") == "USD 45,000"
    assert formatear_monto(12_500_000.4, "CRC") == "CRC 12,500,000"
    assert formatear_monto(12_500_000, "CRC", miles=".") == "CRC 12.500.000"
    assert formatear_monto(1500, "") == "1,500 (moneda sin confirmar)"
    assert "USD" not in formatear_monto(1500, "")


def test_texto_monto_comprasal_queda_identico():
    p = _p(portal_id="comprasal_sv", moneda="", monto=45000.0, monto_usd=45000.0)
    assert texto_monto(p) == "USD 45,000"                   # mismo texto que email_sender: f"USD {m:,.0f}"
    assert texto_monto(p, equivalente=45000.0) == "USD 45,000"  # nunca «USD x (≈ USD x)»
    assert texto_monto(_p(monto=45000.0)) == "USD 45,000"   # objeto viejo, sin portal_id ni moneda
    assert texto_monto(_p(monto=None)) == ""


def test_texto_monto_sicop_con_y_sin_equivalente():
    p = _p(portal_id="sicop_cr", moneda="CRC", monto=12_500_000.0)
    assert texto_monto(p) == "CRC 12,500,000"                                  # sin tasa ⇒ solo el original
    assert texto_monto(p, equivalente=24000) == "CRC 12,500,000 (≈ USD 24,000)"
    assert texto_monto(p, equivalente=24000, con_equivalente=False) == "CRC 12,500,000"
    p.monto_usd = 24000.0                                                      # tasa real de la ADB
    assert texto_monto(p) == "CRC 12,500,000 (≈ USD 24,000)"
    assert texto_monto(p, moneda_equivalente="EUR") == "CRC 12,500,000"        # monto_usd no sirve para EUR
    assert texto_monto(p, equivalente=0) == "CRC 12,500,000"                   # un equivalente 0 no se rotula


def test_texto_monto_sicop_sin_moneda_nunca_dice_usd():
    p = _p(portal_id="sicop_cr", moneda="", monto=12_500_000.0, monto_usd=24000.0)
    assert texto_monto(p) == "12,500,000 (moneda sin confirmar)"
    assert "USD" not in texto_monto(p, equivalente=24000)


def test_texto_monto_mezcla():
    p = _p(portal_id="sicop_cr", moneda="", monto=None, montos_por_moneda={"USD": 35000, "CRC": 1.2e7})
    assert texto_monto(p) == "CRC 12,000,000 + USD 35,000"
    assert texto_monto(p, equivalente=58000) == "CRC 12,000,000 + USD 35,000 (≈ USD 58,000)"
    assert texto_monto(_p(portal_id="sicop_cr", montos_por_moneda={"CRC": 0, "USD": 0})) == ""
