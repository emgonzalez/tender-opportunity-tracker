from contextlib import nullcontext
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import requests

from src.memoria import fx_diario
from src.persistencia.cache import escribir_json, leer_json
from src.portales import obtener_portal
from src.portales.moneda import texto_monto
from src.scraper.models import Proceso

CR = obtener_portal("sicop_cr")
SV = obtener_portal("comprasal_sv")
ZONA_CR = timezone(timedelta(hours=-6))
AHORA = datetime(2026, 11, 24, 7, tzinfo=ZONA_CR)
HOY = AHORA.date()


class Hacienda:
    def __init__(self, dia: date | None, venta: float = 500) -> None:
        self.dia = dia
        self.venta = venta
        self.llamadas: list[str] = []

    def get(self, url: str, **kwargs: object) -> SimpleNamespace:
        self.llamadas.append(url)
        if self.dia is None:
            raise requests.ConnectionError("Hacienda caída")
        datos = {
            "venta": {"fecha": self.dia.isoformat(), "valor": self.venta},
            "compra": {"fecha": self.dia.isoformat(), "valor": 480},
        }
        return SimpleNamespace(status_code=200, json=lambda: datos)


@pytest.fixture
def cfg(tmp_path, monkeypatch) -> SimpleNamespace:
    monkeypatch.setattr(fx_diario, "_corrida", None)
    return SimpleNamespace(
        data_dir=tmp_path, fx_auto=True, fx_api_url="https://hacienda.test",
        enable_esquema_v2=False, fx_alerta_dias=5, fx_repetir_alerta_dias=3,
    )


def proceso(dia: date = HOY, **cambios: object) -> Proceso:
    fecha = datetime(dia.year, dia.month, dia.day, 3, tzinfo=ZONA_CR)
    base = Proceso("Institución", "Compra", fecha, "https://sicop.test", monto=1000,
                   moneda="CRC", portal_id="sicop_cr")
    return replace(base, **cambios)


@pytest.mark.parametrize("modo", ["apagado", "solo_usd", "sicop_deshabilitado", "otra_moneda"])
def test_sin_crc_aplicable_no_consulta(cfg, modo, caplog) -> None:
    portales = [SV, CR]
    if modo == "apagado":
        cfg.fx_auto = False
    elif modo == "solo_usd":
        portales = [SV]
    elif modo == "sicop_deshabilitado":
        cfg.portales_deshabilitados = {"sicop_cr"}
    else:
        portales = [replace(SV, moneda="EUR")]
    hacienda = Hacienda(None)
    resultado = fx_diario.preparar(cfg, portales, sesion=hacienda, ahora=AHORA)
    assert resultado == fx_diario.TasasDiarias()
    assert hacienda.llamadas == []
    assert list(cfg.data_dir.iterdir()) == []
    if modo == "otra_moneda":
        assert "EUR" in caplog.text


def test_red_guarda_fecha_publicada_y_caida_recupera_cache(cfg) -> None:
    ayer = HOY - timedelta(days=1)
    hacienda = Hacienda(ayer)
    resultado = fx_diario.preparar(cfg, [CR], sesion=hacienda, ahora=AHORA)
    assert resultado.aplica and resultado.origen == "red"
    assert resultado.ultima.dia == ayer
    assert hacienda.llamadas == [cfg.fx_api_url]
    assert leer_json(cfg.data_dir / "fx_tasas.json") == [
        {"dia": ayer.isoformat(), "moneda": "CRC", "venta": 500, "compra": 480, "fuente": "Hacienda"}
    ]
    caida = Hacienda(None)
    respaldo = fx_diario.preparar(cfg, [CR], sesion=caida, ahora=AHORA)
    assert respaldo.origen == "cache" and respaldo.tasas == resultado.tasas
    assert caida.llamadas == [cfg.fx_api_url]
    estado = leer_json(cfg.data_dir / "fx_estado.json")
    assert estado["ultimo_exito"] == AHORA.isoformat()
    assert estado["ultimo_dia_tasa"] == ayer.isoformat()
    assert estado["fallos_consecutivos"] == 1
    fx_diario.preparar(cfg, [CR], sesion=hacienda, ahora=AHORA)
    assert leer_json(cfg.data_dir / "fx_estado.json")["fallos_consecutivos"] == 0


def test_poda_y_conversion_sin_red_disponible(cfg) -> None:
    atrasos = (14, 13, 10, 4, 0)
    for atraso in atrasos:
        fecha = HOY - timedelta(days=atraso)
        fx_diario.preparar(cfg, [CR], sesion=Hacienda(fecha, 500 + atraso * 50),
                           ahora=AHORA - timedelta(days=atraso))

    resultado = fx_diario.preparar(cfg, [CR], sesion=Hacienda(None), ahora=AHORA)
    assert {fila["dia"] for fila in leer_json(cfg.data_dir / "fx_tasas.json")} == {
        (HOY - timedelta(days=d)).isoformat() for d in atrasos if d < 14
    }
    assert resultado.origen == "cache"
    mismo_dia, sabado, lunes, sin_tasa = [
        proceso(HOY - timedelta(days=atraso)) for atraso in (0, 3, 1, 21)
    ]
    existente = proceso(monto_usd=99)
    usd = proceso(moneda="USD", portal_id="comprasal_sv")
    mezcla = proceso(monto=None, moneda="", montos_por_moneda={"CRC": 1000, "USD": 80})
    sin_fecha = proceso(fecha_publicacion=None)
    invalido = proceso(monto=float("nan"))
    assert fx_diario.enriquecer(
        cfg, [invalido, mismo_dia, sabado, lunes, sin_tasa, existente, usd, mezcla, sin_fecha],
    ) == 3
    assert mismo_dia.monto_usd == 2
    assert sabado.monto_usd == lunes.monto_usd == 1.43
    assert texto_monto(mismo_dia) == "CRC 1,000 (≈ USD 2)"
    assert texto_monto(sin_tasa) == "CRC 1,000"
    assert existente.monto_usd == 99
    assert all(p.monto_usd is None for p in (usd, mezcla, sin_fecha, invalido))


def test_conversion_sin_preparar_lee_disco_sin_red(cfg, monkeypatch) -> None:
    fecha = fx_diario.dia_local_cr(datetime.now(timezone.utc))
    fx_diario.preparar(cfg, [CR], sesion=Hacienda(fecha))
    monkeypatch.setattr(fx_diario, "_corrida", None)
    monkeypatch.setattr(fx_diario.fx_hacienda, "obtener_ultimo", lambda **kw: pytest.fail("llamada de red"))
    p = proceso(fecha, monto=502.5)
    assert fx_diario.enriquecer(cfg, [p]) == 1
    assert p.monto_usd == 1.01


def test_sin_red_ni_cache_muestra_original(cfg) -> None:
    resultado = fx_diario.preparar(cfg, [CR], sesion=Hacienda(None), ahora=AHORA)
    p = proceso()
    assert resultado == fx_diario.TasasDiarias(aplica=True)
    assert fx_diario.enriquecer(cfg, [p]) == 0
    assert texto_monto(p) == "CRC 1,000"


@pytest.mark.parametrize(
    ("atraso", "desde_alerta", "esperada"),
    [(4, None, False), (5, None, False), (6, None, True), (6, 2, False),
     (6, 3, True), (None, None, True), (None, 0, False)],
)
def test_debe_alertar(atraso, desde_alerta, esperada) -> None:
    estado = {} if desde_alerta is None else {"ultima_alerta": (HOY - timedelta(days=desde_alerta)).isoformat()}
    ultimo = None if atraso is None else HOY - timedelta(days=atraso)
    assert fx_diario.debe_alertar(estado, HOY, ultimo, 5, 3) is esperada


def test_cerrar_tolera_adb_caida_y_alerta_una_vez(cfg, monkeypatch) -> None:
    cfg.enable_esquema_v2 = True
    conexiones = []
    alertas = []

    def conexion(_cfg: SimpleNamespace) -> None:
        conexiones.append(_cfg)
        raise OSError("ADB caída")

    monkeypatch.setattr(fx_diario.db, "connection", conexion)
    monkeypatch.setattr(fx_diario, "send_ops_alert", lambda *args: alertas.append(args) or True)
    fx_diario.preparar(cfg, [CR], sesion=Hacienda(HOY - timedelta(days=6)), ahora=AHORA)
    fx_diario.cerrar(cfg)
    fx_diario.preparar(cfg, [CR], sesion=Hacienda(None), ahora=AHORA + timedelta(days=1))
    fx_diario.cerrar(cfg)
    assert conexiones == [cfg, cfg, cfg]
    assert len(alertas) == 1
    assert "CRC/USD" in alertas[0][1]
    assert "6 días" in alertas[0][2]
    assert "python -m src.memoria.fx descargar --aplicar" in alertas[0][2]


def test_cache_corrupta_y_fallos_de_escritura_y_alerta(cfg, monkeypatch) -> None:
    escribir_json(cfg.data_dir / "fx_tasas.json", [None, {"dia": "inválida"}])
    escribir_json(cfg.data_dir / "fx_estado.json", [])

    def falla(*args: object) -> None:
        raise OSError("fallo simulado")

    monkeypatch.setattr(fx_diario, "escribir_json", falla)
    monkeypatch.setattr(fx_diario, "send_ops_alert", falla)
    resultado = fx_diario.preparar(cfg, [CR], sesion=Hacienda(HOY - timedelta(days=6)), ahora=AHORA)
    assert resultado.aplica and resultado.origen == "red"
    fx_diario.cerrar(cfg)


def test_vigencia_inclusiva_y_sin_tasas_futuras(cfg) -> None:
    fecha_tasa = HOY - timedelta(days=10)
    fx_diario.preparar(cfg, [CR], sesion=Hacienda(fecha_tasa), ahora=AHORA)
    fx_diario.preparar(cfg, [CR], sesion=Hacienda(HOY), ahora=AHORA)
    a_siete, a_ocho = (proceso(fecha_tasa + timedelta(days=dias)) for dias in (7, 8))
    assert fx_diario.enriquecer(cfg, [a_siete, a_ocho]) == 1
    assert a_siete.monto_usd == 2
    assert a_ocho.monto_usd is None



def test_adb_v2_guarda_tasa_y_recalcula_al_cerrar(cfg, monkeypatch) -> None:
    cfg.enable_esquema_v2 = True
    conn = object()
    operaciones = []
    monkeypatch.setattr(fx_diario.db, "connection", lambda config: nullcontext(conn))
    monkeypatch.setattr(fx_diario.repo, "guardar_tasa_dia",
                        lambda conexion, tasa: operaciones.append((conexion, tasa)))
    monkeypatch.setattr(fx_diario.repo, "recalcular_monto_usd",
                        lambda conexion: operaciones.append((conexion, "recalcular")))
    monkeypatch.setattr(fx_diario, "send_ops_alert", lambda *a: pytest.fail("tasa vigente"))
    ayer = HOY - timedelta(days=1)
    tasas = fx_diario.preparar(cfg, [CR], sesion=Hacienda(ayer), ahora=AHORA)
    assert operaciones == [(conn, tasas.ultima)]
    assert tasas.ultima.dia == ayer
    fx_diario.cerrar(cfg)
    assert operaciones == [(conn, tasas.ultima), (conn, "recalcular")]
