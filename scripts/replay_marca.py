#!/usr/bin/env python3
"""Compara reglas sobre el histórico sin escribir en bases de datos."""
from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.aprendizaje import evaluacion, recalcular, repo
from src.classifier.alias_clientes import id_canonico

ENVIADOS = {"confirmada", "por_confirmar"}


class _ConfigReplay:
    """Impide que la fachada LLM escriba consumo en otra conexión a la ADB."""
    llm_registro_habilitado = False

    def __init__(self, cfg):
        self._cfg = cfg

    def __getattr__(self, nombre):
        return getattr(self._cfg, nombre)


def configuracion():
    # La herramienta usa exclusivamente el entorno, nunca abre .env.
    with patch("dotenv.load_dotenv", return_value=False):
        from src.config import get_config
    return get_config()


def leer_borradores(conn, cliente):
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT proceso_gid, marca_id, etiqueta FROM ejemplo_marca "
                       "WHERE cliente_id = :cliente AND activo = 1 AND fuente = 'semilla' "
                       "ORDER BY ejemplo_id", {"cliente": cliente})
        return [dict(proceso_gid=f[0], marca_id=f[1], etiqueta=f[2]) for f in cursor.fetchall()]
    finally:
        cursor.close()


def etapa_a(filas, terminos, ejemplos):
    marcas = {"oracle"} | {recalcular.marca_id(e["marca_id"]) for e in ejemplos}
    marcas |= {recalcular.marca_id(t["marca_id"]) for t in terminos if t.get("tipo") != "ajena"}
    marcas |= {recalcular.marca_id(m.get("marca", "")) for f in filas for m in f["marcas"] if isinstance(m, dict)}
    marcas.discard("")
    decisiones = {}
    transiciones = Counter()
    salen = entran = 0
    por_gid = {f["proceso_gid"]: f for f in filas}
    for f in filas:
        nuevos = recalcular.calcular(f, terminos, marcas=sorted(marcas))
        decisiones[f["proceso_gid"]] = nuevos
        for marca, v in nuevos.items():
            viejo = recalcular.legado(f, marca)
            transiciones[(marca, "llega" if viejo else "no_llega", v.nivel)] += 1
        oracle = nuevos["oracle"].nivel
        viejo = recalcular.legado(f)
        salen += viejo and oracle not in ENVIADOS
        entran += not viejo and oracle == "por_confirmar"
    grupos = defaultdict(list)
    omitidos = 0
    for e in ejemplos:
        f = por_gid.get(e["proceso_gid"])
        if f is None or e["etiqueta"] not in {"positivo", "negativo", "ajena"}:
            omitidos += 1
            continue
        marca = recalcular.marca_id(e["marca_id"])
        grupos[marca].append((e["etiqueta"] == "positivo", recalcular.legado(f, marca),
                              decisiones[e["proceso_gid"]][marca].nivel in ENVIADOS))
    metricas = {}
    for marca, grupo in sorted(grupos.items()):
        metricas[marca] = {nombre: evaluacion.precision_recall_f1(
            evaluacion.matriz_confusion([g[0] for g in grupo], [g[i] for g in grupo]))
            for nombre, i in (("legada", 1), ("nueva", 2))}
    return dict(procesos=len(filas), transiciones=[dict(marca=m, legado=anterior, nuevo=n, cantidad=c)
                for (m, anterior, n), c in sorted(transiciones.items())], oracle_dejan_de_llegar=salen,
                oracle_nuevos_por_confirmar=entran, metricas=metricas,
                ejemplos_omitidos=omitidos, etiquetas="BORRADORES semilla; medición preliminar")


def _guardar_checkpoint(ruta, estado):
    ruta.parent.mkdir(parents=True, exist_ok=True)
    temporal = ruta.with_suffix(ruta.suffix + ".tmp")
    temporal.write_text(json.dumps(estado, ensure_ascii=False, indent=2), encoding="utf-8")
    temporal.replace(ruta)


def etapa_b(filas, terminos, cfg, profile, presupuesto, *, parcial=False, estimar=False,
            checkpoint=None, estado=None, cliente="", decir=print):
    from src import llm
    from src.classifier import engine, rules
    from src.drilldown.quota import costo_usd
    from src.llm.contrato import BloqueTexto, Pedido
    from src.scraper.models import Proceso

    cfg = _ConfigReplay(cfg)
    modelo = llm.modelo_de(cfg, "clasificacion")
    ids = [f["proceso_gid"] for f in filas]
    estado = estado or dict(version=1, cliente=cliente, ids=ids, resultados={}, gastado=0.0, lotes=0)
    if (estado["version"] != 1 or estado["cliente"] != cliente or estado["ids"] != ids
            or not math.isfinite(estado["gastado"]) or estado["gastado"] < 0
            or not set(estado["resultados"]).issubset(ids)):
        raise ValueError("Checkpoint incompatible")
    if len(ids) != len(set(ids)):
        raise ValueError("Ids duplicados")
    pendientes = [f for f in filas if f["proceso_gid"] not in estado["resultados"]]
    tamano = engine._batch_size(cfg)
    lotes = []
    for inicio in range(0, len(pendientes), tamano):
        lote = pendientes[inicio:inicio + tamano]
        procesos = [Proceso(institucion="", descripcion=f["titulo"],
                            fecha_publicacion=datetime(2000, 1, 1), url_directa="",
                            numero_referencia=str(i)) for i, f in enumerate(lote)]
        datos = engine._procesos_to_dicts(procesos)
        for d in datos:
            d["tipo_actividad"] = ""
        sistema = rules.get_system_prompt_v2(profile)
        usuario = rules.get_user_prompt_v2(datos)
        esquema = rules.esquema_clasificacion_v2() if llm.soporta_esquema(modelo) else None
        # Un token por byte UTF-8, más margen de transporte, y TODO el techo de salida.
        tokens = len((sistema + usuario + json.dumps(esquema)).encode("utf-8")) + 4096
        estimado = costo_usd(modelo, tokens, engine.MAX_TOKENS)
        if not math.isfinite(estimado) or estimado <= 0:
            raise ValueError("Tarifa no disponible")
        pedido = Pedido(etapa="clasificacion", sistema=sistema, contenido=[BloqueTexto(usuario)],
                        max_tokens=engine.MAX_TOKENS, temperatura=llm.temperatura_configurada(cfg),
                        esquema=esquema, nombre_esquema="clasificacion_marcas")
        lotes.append((lote, procesos, pedido, estimado))
    total_estimado = sum(lote[3] for lote in lotes)
    disponible = max(0, presupuesto - estado["gastado"])
    cabe = total_estimado <= disponible
    decir(f"Pre-vuelo: {len(pendientes)} títulos pendientes · {len(lotes)} lotes · "
          f"estimado conservador US${total_estimado:.4f} · orientativo ≈ real "
          f"US${total_estimado / 2.3:.4f} (estimado / 2,3; la guardia sobreestima ≈ 2,3×). "
          f"{'Cabe' if cabe else 'NO cabe'} en el presupuesto US${presupuesto:.2f}.")
    if not cabe:
        decir(f"Faltarían US${max(0, total_estimado - disponible):.4f}; exige --parcial para continuar.")
    lotes_anteriores = estado["lotes"]
    restantes = total_estimado
    for lote, procesos, pedido, estimado in lotes:
        if estimar or (not cabe and not parcial) or estado["gastado"] + estimado > presupuesto:
            break
        costo = estimado
        nuevos = None
        try:
            respuesta = llm.completar(cfg, pedido)
            uso = respuesta.uso
            real = costo_usd(modelo, uso.input_tokens, uso.output_tokens)
            if not math.isfinite(real) or real < 0:
                raise ValueError("Consumo inválido")
            costo = real
            nuevos = engine._parsear_v2(respuesta.texto, procesos, profile, terminos)
            if len(nuevos) != len(lote):
                nuevos = None
        except Exception:
            # Sin uso reportado, se conserva la reserva y no se repite el cobro.
            pass
        estado["gastado"] += costo
        estado["lotes"] += 1
        for i, f in enumerate(lote):
            nuevo = nuevos[i] if nuevos is not None else None
            # Lista cerrada: no persistir títulos, razones ni citas con contactos.
            estado["resultados"][f["proceso_gid"]] = dict(
                error=nuevo is None,
                es_relevante=nuevo.es_relevante if nuevo else None,
                marcas=[dict(marca=recalcular.publico(m.marca),
                             veredicto=m.veredicto, origen="replay",
                             score=getattr(m, "score", None),
                             evidencia_valida=bool(getattr(m, "evidencia_valida", False)))
                        for m in nuevo.marcas_recomendadas] if nuevo else [])
        restantes -= estimado
        if checkpoint:
            _guardar_checkpoint(checkpoint, estado)
        n = len(estado["resultados"])
        media = estado["gastado"] / estado["lotes"]
        proyeccion = media * (len(lotes) - (estado["lotes"] - lotes_anteriores))
        print(f"Procesados {n} de {len(filas)} títulos · lotes {estado['lotes']} de "
              f"{lotes_anteriores + len(lotes)} · gastado US${estado['gastado']:.4f} · "
              f"media/lote US${media:.4f} · restante proyectado US${proyeccion:.4f} · "
              f"total proyectado US${estado['gastado'] + proyeccion:.4f}", file=sys.stderr)
    n = len(estado["resultados"])
    adicionales = max(0, restantes * 1.1 - max(0, presupuesto - estado["gastado"]))
    balance = dict(tope=presupuesto, gastado=estado["gastado"], procesados=n,
                   total=len(filas), pendientes=len(filas) - n,
                   costo_medio_lote=estado["gastado"] / estado["lotes"] if estado["lotes"] else 0,
                   estimado_restante_conservador=max(0, restantes),
                   estimado_restante_orientativo=max(0, restantes) / 2.3,
                   adicional_recomendado=adicionales, completo=n == len(filas))
    if checkpoint and not estimar:
        _guardar_checkpoint(checkpoint, estado)
    decir(f"REPORTE FINAL: Procesados {n} de {len(filas)} ({100 * n / len(filas) if filas else 100:.1f} %) · "
          f"Gastado US${estado['gastado']:.4f} de US${presupuesto:.2f} · "
          f"Faltan {len(filas) - n} títulos ≈ US${restantes:.4f} (conservador) / "
          f"≈ US${restantes / 2.3:.4f} (orientativo) · "
          f"Presupuesto adicional recomendado: US${adicionales:.4f} (con 10 % de margen)")
    comparados = [(f, estado["resultados"][f["proceso_gid"]]) for f in filas
                  if f["proceso_gid"] in estado["resultados"]
                  and not estado["resultados"][f["proceso_gid"]]["error"]]
    cantidad = len(comparados)
    return dict(presupuesto=balance, comparados=cantidad, sin_comparar=len(filas) - cantidad,
                errores=sum(r["error"] for r in estado["resultados"].values()),
                relevante_a_no_relevante=sum(bool(f.get("es_relevante")) and not r["es_relevante"]
                                            for f, r in comparados),
                cambios_de_marca=sum(
                    {m.get("marca", "").casefold() for m in f["marcas"] if isinstance(m, dict)} !=
                    {m["marca"].casefold() for m in r["marcas"] if m["veredicto"] != "fuera"}
                    for f, r in comparados),
                tasa_relevantes_antes=sum(bool(f.get("es_relevante")) for f, _ in comparados) /
                cantidad if cantidad else None,
                tasa_relevantes_despues=sum(r["es_relevante"] for _, r in comparados) /
                cantidad if cantidad else None)


def main(argv=None, *, conectar=None, cfg=None, decir=print):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cliente", default="multicomputos_sv")
    parser.add_argument("--json", type=Path)
    parser.add_argument("--llm", action="store_true")
    parser.add_argument("--presupuesto-usd", type=float, default=5.00,
                        help="Tope histórico total en USD, incluido lo gastado antes de reanudar")
    parser.add_argument("--estimar", action="store_true")
    parser.add_argument("--parcial", action="store_true")
    parser.add_argument("--checkpoint", type=Path)
    parser.add_argument("--reanudar", type=Path)
    args = parser.parse_args(argv)
    if not math.isfinite(args.presupuesto_usd) or args.presupuesto_usd < 0:
        parser.error("--presupuesto-usd debe ser finito y no negativo")
    try:
        cliente = id_canonico(args.cliente)
        if conectar is None:
            from src.memoria import db
            cfg = cfg or configuracion()
            def conectar():
                return db.connection(cfg)
        with conectar() as conn:
            # Oracle rechaza cualquier DML posterior en esta transacción.
            cur = conn.cursor()
            try:
                cur.execute("SET TRANSACTION READ ONLY")
            finally:
                cur.close()
            filas = recalcular.leer_procesos(conn, cliente)
            terminos = repo.listar_terminos_activos(conn, cliente)
            resultado = {"etapa_a": etapa_a(filas, terminos, leer_borradores(conn, cliente))}
            if args.llm or args.estimar or args.reanudar:
                from src.classifier.client_profile import _parse_profile
                from src.clientes import repo as clientes_repo
                activo = clientes_repo.activo(conn, cliente)
                if activo is None:
                    raise ValueError("Sin perfil activo")
                perfil = _parse_profile(activo[1], "replay")
                resultado["etapa_b"] = etapa_b(filas, terminos, cfg or configuracion(), perfil,
                    args.presupuesto_usd, parcial=args.parcial, estimar=args.estimar,
                    checkpoint=args.checkpoint or args.reanudar or
                    Path(f"data/replay_marca_{cliente}.checkpoint.json"),
                    estado=json.loads(args.reanudar.read_text(encoding="utf-8")) if args.reanudar else None,
                    cliente=cliente, decir=decir)
            resultado["presupuesto"] = resultado.get("etapa_b", {}).get("presupuesto", dict(
                tope=args.presupuesto_usd, gastado=0, procesados=0, total=len(filas),
                pendientes=len(filas), costo_medio_lote=0, estimado_restante_conservador=0,
                estimado_restante_orientativo=0, adicional_recomendado=0, completo=not filas))
        a = resultado["etapa_a"]
        decir("BORRADORES de semilla: medición PRELIMINAR; no autoriza activación automática.")
        decir("Marca | Legado → Nuevo | Cantidad")
        for t in a["transiciones"]:
            decir(f"{recalcular.publico(t['marca'])} | {t['legado']} → {t['nuevo']} | {t['cantidad']}")
        decir("Métricas P/R/F1, Wilson y matriz: " + json.dumps(a["metricas"], ensure_ascii=False))
        if "etapa_b" in resultado:
            decir("Etapa B (solo procesos comparados): " + json.dumps(resultado["etapa_b"]))
        if args.json:
            args.json.write_text(json.dumps(resultado, ensure_ascii=False, indent=2), encoding="utf-8")
        decir(f"Resumen ejecutivo: {a['procesos']} procesos; Oracle dejaría de recibir "
              f"{a['oracle_dejan_de_llegar']}; nuevos por confirmar: {a['oracle_nuevos_por_confirmar']}. "
              f"Borradores sin cobertura: {a['ejemplos_omitidos']}.")
    except Exception as exc:
        decir(f"ERROR: replay no disponible ({type(exc).__name__}); sin escrituras en bases de datos")
        return 1
    if "etapa_b" in resultado and not args.estimar:
        if resultado["etapa_b"]["errores"]:
            return 1
        if not resultado["presupuesto"]["completo"]:
            return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
