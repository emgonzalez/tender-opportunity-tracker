"""Pruebas de la regla de conversión diaria sin I/O."""
from datetime import date, datetime, timedelta, timezone

import pytest

from src.memoria.fx_regla import TasaDia, convertir_a_usd, dia_local_cr, tasa_vigente


def _tasa(dia: date) -> TasaDia:
    return TasaDia("CRC", dia, 445.2, 440.9, "Hacienda")


def test_tasa_vigente_elige_el_mismo_dia_entre_tasas_desordenadas():
    jueves = _tasa(date(2026, 10, 29))
    viernes = _tasa(date(2026, 10, 30))
    lunes = _tasa(date(2026, 11, 2))
    assert tasa_vigente(iter([lunes, jueves, viernes]), viernes.dia) == viernes


def test_tasa_vigente_cubre_fin_de_semana_y_cambio_de_mes():
    viernes = _tasa(date(2026, 10, 30))
    lunes = _tasa(date(2026, 11, 2))
    assert tasa_vigente({lunes.dia: lunes, viernes.dia: viernes}, date(2026, 11, 1)) == viernes


def test_tasa_vigente_usa_el_dia_anterior_si_falta_el_dia():
    lunes = _tasa(date(2026, 11, 23))
    miercoles = _tasa(date(2026, 11, 25))
    assert tasa_vigente([miercoles, lunes], date(2026, 11, 24)) == lunes


def test_ventana_incluye_siete_dias_de_atraso():
    tasa = _tasa(date(2026, 11, 24))
    assert tasa_vigente([tasa], tasa.dia + timedelta(days=7)) == tasa
    assert tasa_vigente([tasa], tasa.dia + timedelta(days=8)) is None
    assert tasa_vigente([tasa], tasa.dia + timedelta(days=1), max_dias=0) is None
    assert tasa_vigente([], tasa.dia) is None


@pytest.mark.parametrize("monto,venta,esperado", [(2.5, 2, 1.25), (2.5, 4, 0.63), (-2.5, 4, -0.63)])
def test_conversion_redondea_half_up(monto, venta, esperado):
    assert convertir_a_usd(monto, venta) == esperado


@pytest.mark.parametrize("monto,venta", [(10, 0), (10, -1), (1, float("inf")), (float("nan"), 1)])
def test_conversion_rechaza_entradas_invalidas(monto, venta):
    with pytest.raises(ValueError):
        convertir_a_usd(monto, venta)


@pytest.mark.parametrize("publicacion,esperado", [
    (datetime(2026, 11, 24, 5, 59), date(2026, 11, 23)),
    (datetime(2026, 11, 24, 6, 0, tzinfo=timezone.utc), date(2026, 11, 24)),
    (datetime(2026, 11, 24, 7, 59, tzinfo=timezone(timedelta(hours=2))), date(2026, 11, 23)),
])
def test_dia_local_cr(publicacion, esperado):
    assert dia_local_cr(publicacion) == esperado
