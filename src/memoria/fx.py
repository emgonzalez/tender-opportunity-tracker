"""src.memoria.fx — mantenimiento del tipo de cambio y de la moneda de ``proceso``.

Carga tasas diarias de Hacienda y tasas mensuales de respaldo. **Dry-run por defecto**.
Con ``--aplicar`` las escrituras se hacen en una transacción.

Uso:
    python -m src.memoria.fx cargar --moneda CRC --mes 2026-11 --colones-por-dolar 525.30 \\
        --fuente "BCCR referencia venta" [--aplicar]
    python -m src.memoria.fx cargar --csv tasas.csv [--aplicar]    # moneda,mes,valor_por_dolar,fuente
    python -m src.memoria.fx descargar [--aplicar]
    python -m src.memoria.fx descargar --historico --desde AAAA-MM-DD [--hasta AAAA-MM-DD] [--aplicar]
    python -m src.memoria.fx recalcular [--aplicar]
    python -m src.memoria.fx rellenar-moneda [--aplicar]           # solo portales de una moneda
    python -m src.memoria.fx estado                                # solo lectura

``tasa_a_usd = 1 / valor_por_dolar`` (6 decimales). ``rellenar-moneda`` jamás toca portales con dos monedas
(SICOP): un monto sin moneda allí no se puede asumir. Ninguno escribe ``proceso.actualizado_en`` salvo
``recalcular_monto_usd`` (que ya lo hacía): ninguna vista lo lee, ordenan por ``oportunidad_estado``/``cliente_proceso``.
"""
from __future__ import annotations

import argparse
import contextlib
import csv
import logging
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Iterator

from src.memoria import fx_hacienda, repo
from src.memoria.fx_regla import TasaDia, dia_local_cr

logger = logging.getLogger(__name__)

TOLERANCIA_REDONDEO = Decimal("0.001")  # 0,1 %
_SEIS = Decimal("0.000001")

_SIN_TASA_SQL = f"SELECT COUNT(*) FROM proceso p WHERE {repo.SQL_FILAS_SIN_TASA} AND p.moneda = :moneda"
_CAMBIARIAN_SQL = f"SELECT COUNT(*) FROM proceso p WHERE {repo.SQL_FILAS_A_RECALCULAR}"


class ErrorFx(ValueError):
    """Entrada inválida (valor, mes o moneda)."""


@dataclass(frozen=True)
class Tasa:
    moneda: str
    mes: date            # primer día del mes
    tasa_a_usd: Decimal  # 6 decimales
    fuente: str
    valor_por_dolar: Decimal


# ── Validaciones puras ──────────────────────────────────────────────────────

def monedas_permitidas() -> set[str]:
    """Monedas que declara algún portal, sin USD (usa tasa 1 y no se guarda)."""
    from src.portales import PORTALES

    salida: set[str] = set()
    for p in PORTALES.values():
        salida.update(p.monedas or (p.moneda,))
    salida.discard("USD")
    return salida


def parsear_mes(texto: str, hoy: date | None = None) -> date:
    try:
        anio, mes = (int(x) for x in texto.strip().split("-"))
        primero = date(anio, mes, 1)
    except (ValueError, AttributeError):
        raise ErrorFx(f"mes inválido {texto!r}: se espera AAAA-MM") from None
    hoy = hoy or datetime.now(timezone.utc).date()
    if primero > hoy.replace(day=1):
        raise ErrorFx(f"mes futuro {texto!r}: no se cargan tasas de meses que no han ocurrido")
    return primero


def construir_tasa(moneda: str, mes: str, valor: str, fuente: str = "", hoy: date | None = None) -> Tasa:
    moneda = (moneda or "").strip().upper()
    permitidas = monedas_permitidas()
    if moneda not in permitidas:
        raise ErrorFx(f"moneda {moneda!r} no permitida (portales declaran: {', '.join(sorted(permitidas)) or 'ninguna'}; "
                      "USD no se carga: usa tasa 1)")
    primero = parsear_mes(mes, hoy)
    try:
        v = Decimal(str(valor).strip())
    except InvalidOperation:
        raise ErrorFx(f"valor inválido {valor!r}") from None
    if not v.is_finite() or v <= 0:
        raise ErrorFx(f"valor por dólar debe ser > 0 (recibido {valor!r})")
    tasa = (Decimal(1) / v).quantize(_SEIS, rounding=ROUND_HALF_UP)
    if tasa <= 0:
        raise ErrorFx(f"valor {valor!r} tan grande que la tasa redondea a 0")
    error = abs(tasa * v - 1)
    if error > TOLERANCIA_REDONDEO:
        raise ErrorFx(f"el redondeo a 6 decimales cambiaría un monto en {error:.4%} (> 0,1 %) con valor {valor!r}")
    return Tasa(moneda, primero, tasa, (fuente or "").strip()[:120], v)


def leer_csv(ruta: str, hoy: date | None = None) -> list[Tasa]:
    """Lee y valida TODO el CSV; si alguna fila falla, ErrorFx con todos los errores (nada se escribe)."""
    tasas: list[Tasa] = []
    errores: list[str] = []
    with open(ruta, newline="", encoding="utf-8") as fh:
        lector = csv.DictReader(fh)
        faltan = {"moneda", "mes", "valor_por_dolar"} - set(lector.fieldnames or [])
        if faltan:
            raise ErrorFx(f"al CSV le faltan columnas: {', '.join(sorted(faltan))}")
        for n, fila in enumerate(lector, start=2):
            try:
                tasas.append(construir_tasa(fila["moneda"], fila["mes"], fila["valor_por_dolar"],
                                            fila.get("fuente") or "", hoy))
            except ErrorFx as exc:
                errores.append(f"línea {n}: {exc}")
    if errores:
        raise ErrorFx("; ".join(errores))
    if not tasas:
        raise ErrorFx("el CSV no tiene filas")
    return tasas


# ── SQL ─────────────────────────────────────────────────────────────────────

def _uno(conn: Any, sql: str, params: dict | None = None) -> Any:
    cur = conn.cursor()
    try:
        cur.execute(sql, params or {})
        fila = cur.fetchone()
    finally:
        cur.close()
    return fila[0] if fila else None


def _todos(conn: Any, sql: str, params: dict | None = None) -> list:
    cur = conn.cursor()
    try:
        cur.execute(sql, params or {})
        return list(cur.fetchall())
    finally:
        cur.close()


def cargar_tasas(conn: Any, tasas: list[Tasa], aplicar: bool) -> dict[str, int]:
    """Informa (y con ``aplicar`` hace MERGE de) cada tasa. Cuenta insertar/actualizar/igual."""
    cuenta = {"insertar": 0, "actualizar": 0, "igual": 0}
    for t in tasas:
        previa = _uno(conn, "SELECT tasa_a_usd FROM fx_tasa WHERE moneda = :moneda AND mes = :mes",
                      {"moneda": t.moneda, "mes": t.mes})
        if previa is None:
            accion = "insertar"
        elif Decimal(str(previa)) == t.tasa_a_usd:
            accion = "igual"
        else:
            accion = "actualizar"
        cuenta[accion] += 1
        print(f"  {accion:10s} {t.moneda} {t.mes:%Y-%m}: tasa_a_usd={t.tasa_a_usd} (1 USD = {t.valor_por_dolar} {t.moneda})"
              + (f" [antes {previa}]" if accion == "actualizar" else ""))
        if aplicar and accion != "igual":
            cur = conn.cursor()
            try:
                cur.execute(
                    "MERGE INTO fx_tasa tgt USING (SELECT :moneda AS moneda, :mes AS mes FROM dual) src "
                    "ON (tgt.moneda = src.moneda AND tgt.mes = src.mes) "
                    "WHEN MATCHED THEN UPDATE SET tgt.tasa_a_usd = :tasa, tgt.fuente = :fuente "
                    "WHEN NOT MATCHED THEN INSERT (moneda, mes, tasa_a_usd, fuente) "
                    "VALUES (:moneda, :mes, :tasa, :fuente)",
                    {"moneda": t.moneda, "mes": t.mes, "tasa": float(t.tasa_a_usd), "fuente": t.fuente or None},
                )
            finally:
                cur.close()
    return cuenta


def _hoy_cr() -> date:
    return dia_local_cr(datetime.now(timezone.utc))


def _fecha_cli(texto: str) -> date:
    try:
        dia = date.fromisoformat(texto)
        if dia.isoformat() == texto:
            return dia
    except ValueError:
        pass
    raise ErrorFx(f"fecha inválida {texto!r}: se espera AAAA-MM-DD")


def _descargar(args: argparse.Namespace) -> tuple[list[TasaDia], int]:
    if not args.historico:
        if args.desde or args.hasta:
            raise ErrorFx("--desde y --hasta requieren --historico")
        # La ejecución diaria programada dispone de una sola petición HTTP.
        return [fx_hacienda.obtener_ultimo(intentos=1)], 0
    if not args.desde:
        raise ErrorFx("--historico requiere --desde")
    desde = _fecha_cli(args.desde)
    hasta = _fecha_cli(args.hasta) if args.hasta else _hoy_cr()
    if desde > hasta:
        raise ErrorFx("--desde debe ser anterior o igual a --hasta")

    tasas: list[TasaDia] = []
    fallos = 0
    while True:
        fin = desde + timedelta(days=min(13, (hasta - desde).days))
        try:
            tramo = fx_hacienda.obtener_rango(desde, fin)
        except fx_hacienda.ErrorFxHacienda as exc:
            print(f"ERROR: tramo {desde} a {fin}: {exc}", file=sys.stderr)
            fallos += 1
        else:
            if not tramo and any(
                (desde.weekday() + dia) % 7 < 5 for dia in range((fin - desde).days + 1)
            ):
                print(f"ERROR: tramo {desde} a {fin}: SIN DATOS de Hacienda", file=sys.stderr)
                fallos += 1
            tasas.extend(tramo)
        if fin == hasta:
            return tasas, fallos
        desde = fin + timedelta(days=1)


def _cargar_diarias(conn: Any, tasas: list[TasaDia], aplicar: bool) -> None:
    for tasa in tasas:
        if aplicar:
            accion = "insertó/actualizó" if repo.guardar_tasa_dia(conn, tasa) else "ya estaba"
        else:
            previa = _uno(conn, """
                SELECT CASE WHEN venta = CAST(:venta AS NUMBER(12,4))
                             AND DECODE(compra, CAST(:compra AS NUMBER(12,4)), 1, 0) = 1
                             AND DECODE(fuente, :fuente, 1, 0) = 1 THEN 'ya estaba'
                            ELSE 'actualizaría' END
                  FROM fx_tasa_dia WHERE moneda = :moneda AND dia = :dia
            """, {"moneda": tasa.moneda, "dia": tasa.dia, "venta": tasa.venta,
                  "compra": tasa.compra, "fuente": tasa.fuente or None})
            accion = previa or "insertaría"
        print(f"  {accion:19s} {tasa.moneda} {tasa.dia}: venta={tasa.venta} ({tasa.fuente})")


def recalcular(conn: Any, aplicar: bool) -> dict[str, int]:
    if aplicar:
        cambiadas = repo.recalcular_monto_usd(conn)
    else:
        cambiadas = int(_uno(conn, _CAMBIARIAN_SQL) or 0)
    sin_tasa = {m: int(_uno(conn, _SIN_TASA_SQL, {"moneda": m}) or 0) for m in sorted(monedas_permitidas())}
    print(f"  filas {'actualizadas' if aplicar else 'que cambiarían'}: {cambiadas}")
    for m, n in sin_tasa.items():
        print(f"  filas con monto en {m} SIN tasa: {n}")
    return {"cambiadas": cambiadas, **{f"sin_tasa_{m}": n for m, n in sin_tasa.items()}}


def rellenar_moneda(conn: Any, aplicar: bool) -> dict[str, int]:
    """``UPDATE proceso SET moneda`` solo en portales de una moneda. No escribe ``actualizado_en``."""
    from src.portales import PORTALES

    resultado: dict[str, int] = {}
    for pid, p in PORTALES.items():
        n = int(_uno(conn, "SELECT COUNT(*) FROM proceso WHERE portal_id = :p AND moneda IS NULL", {"p": pid}) or 0)
        if not p.moneda_unica:
            print(f"  {pid}: {n} sin moneda — OMITIDO (publica {'/'.join(p.monedas)}; no se puede asumir)")
            continue
        print(f"  {pid}: {n} sin moneda -> {p.moneda}")
        resultado[pid] = n
        if aplicar and n:
            cur = conn.cursor()
            try:
                cur.execute("UPDATE proceso SET moneda = :m WHERE portal_id = :p AND moneda IS NULL",
                            {"m": p.moneda, "p": pid})
            finally:
                cur.close()
    return resultado


def estado(conn: Any) -> None:
    from src.portales import PORTALES

    print("Tasas cargadas:")
    filas = _todos(conn, "SELECT moneda, mes, tasa_a_usd, fuente FROM fx_tasa ORDER BY moneda, mes")
    for moneda, mes, tasa, fuente in filas:
        print(f"  {moneda} {mes:%Y-%m}: {tasa} ({fuente or 's/fuente'})")
    if not filas:
        print("  (ninguna)")
    print("Últimas tasas diarias cargadas por moneda:")
    diarias = _todos(conn, """
        SELECT moneda, dia, venta, posicion
          FROM (SELECT moneda, dia, venta,
                       ROW_NUMBER() OVER (PARTITION BY moneda ORDER BY dia DESC) posicion
                  FROM fx_tasa_dia)
         WHERE posicion <= 7 ORDER BY moneda, dia DESC
    """)
    hoy = _hoy_cr()
    for moneda, dia, venta, posicion in diarias:
        fecha = dia.date() if isinstance(dia, datetime) else dia
        edad = f" (antigüedad: {(hoy - fecha).days} días)" if posicion == 1 else ""
        print(f"  {moneda} {fecha}: venta={venta}{edad}")
    if not diarias:
        print("  (ninguna)")
    print("Filas con moneda NULL por portal:")
    for pid in PORTALES:
        n = _uno(conn, "SELECT COUNT(*) FROM proceso WHERE portal_id = :p AND moneda IS NULL", {"p": pid})
        print(f"  {pid}: {int(n or 0)}")
    print("Filas con monto sin tasa:")
    sin_tasa = dict(_todos(conn, f"""
        SELECT p.moneda, COUNT(*) FROM proceso p WHERE {repo.SQL_FILAS_SIN_TASA} GROUP BY p.moneda
    """))
    for m in sorted(monedas_permitidas() | sin_tasa.keys()):
        print(f"  {m}: {sin_tasa.get(m, 0)}")


# ── Conexión ────────────────────────────────────────────────────────────────

@contextlib.contextmanager
def _abrir(cfg: Any, escribir: bool) -> Iterator[Any]:
    """Lectura: ``SET TRANSACTION READ ONLY`` y rollback siempre. Escritura: una transacción, commit al final."""
    from src.memoria import db

    pool = db.get_pool(cfg)
    conn = pool.acquire()
    try:
        if not escribir:
            cur = conn.cursor()
            try:
                cur.execute("SET TRANSACTION READ ONLY")
            finally:
                cur.close()
        yield conn
        if escribir:
            conn.commit()
        else:
            conn.rollback()
    except Exception:
        conn.rollback()
        raise
    finally:
        pool.release(conn)


# ── CLI ─────────────────────────────────────────────────────────────────────

def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("cargar", help="carga tasas mensuales en fx_tasa")
    c.add_argument("--moneda")
    c.add_argument("--mes", help="AAAA-MM")
    c.add_argument("--colones-por-dolar", "--valor-por-dolar", dest="valor", help="unidades de la moneda por 1 USD")
    c.add_argument("--fuente", default="")
    c.add_argument("--csv", help="columnas: moneda,mes,valor_por_dolar,fuente")
    c.add_argument("--aplicar", action="store_true", help="escribe (por defecto, dry-run)")
    d = sub.add_parser("descargar", help="descarga la venta diaria de Hacienda en CRC")
    d.add_argument("--historico", action="store_true", help="solicita el histórico por tramos")
    d.add_argument("--desde", help="AAAA-MM-DD")
    d.add_argument("--hasta", help="AAAA-MM-DD (por defecto, hoy en Costa Rica)")
    d.add_argument("--aplicar", action="store_true", help="escribe (por defecto, dry-run)")
    for nombre in ("recalcular", "rellenar-moneda"):
        sub.add_parser(nombre).add_argument("--aplicar", action="store_true", help="escribe (por defecto, dry-run)")
    sub.add_parser("estado", help="solo lectura")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                        handlers=[logging.StreamHandler(sys.stdout)])
    tasas: list[Tasa] = []
    if args.cmd == "cargar":
        try:
            if args.csv:
                if args.moneda or args.mes or args.valor:
                    raise ErrorFx("--csv no se combina con --moneda/--mes/--colones-por-dolar")
                tasas = leer_csv(args.csv)
            else:
                if not (args.moneda and args.mes and args.valor):
                    raise ErrorFx("indica --moneda, --mes y --colones-por-dolar, o --csv")
                tasas = [construir_tasa(args.moneda, args.mes, args.valor, args.fuente)]
        except (ErrorFx, OSError) as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2

    from src.config import get_config

    aplicar = getattr(args, "aplicar", False)
    print("[SOLO LECTURA] estado" if args.cmd == "estado" else f"[{'APLICAR' if aplicar else 'DRY-RUN'}] {args.cmd}")
    diarias: list[TasaDia] = []
    fallos = 0
    if args.cmd == "descargar":
        try:
            diarias, fallos = _descargar(args)
        except ErrorFx as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2
        except fx_hacienda.ErrorFxHacienda as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
        if not diarias:
            print("  (ninguna tasa descargada)")
            return int(fallos > 0)
    with _abrir(get_config(), escribir=aplicar) as conn:
        if args.cmd == "cargar":
            cargar_tasas(conn, tasas, aplicar)
        elif args.cmd == "descargar":
            _cargar_diarias(conn, diarias, aplicar)
        elif args.cmd == "recalcular":
            recalcular(conn, aplicar)
        elif args.cmd == "rellenar-moneda":
            rellenar_moneda(conn, aplicar)
        else:
            estado(conn)
    if args.cmd != "estado" and not aplicar:
        print("(dry-run: no se escribió nada; repite con --aplicar)")
    return int(fallos > 0)


if __name__ == "__main__":
    sys.exit(main())
