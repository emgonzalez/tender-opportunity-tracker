"""Replay aislado de pliegos históricos; nunca invoca la orquestación diaria.

El checkpoint público contiene únicamente diagnósticos. Las respuestas necesarias
para reanudar viven en un temporal privado, eliminado al completar el lote.
"""
from __future__ import annotations

import argparse
import asyncio
import dataclasses
import hashlib
import json
import logging
import math
import os
import re
import signal
import tempfile
import time
from collections import Counter
from contextlib import ExitStack, contextmanager
from dataclasses import asdict
from datetime import datetime, timedelta
from datetime import time as hora
from difflib import SequenceMatcher
from pathlib import Path
from statistics import median
from unittest.mock import patch
from zoneinfo import ZoneInfo

from src import llm
from src.classifier.alias_clientes import id_canonico
from src.classifier.client_profile import hash_contenido
from src.classifier.filtro_ruido import evaluar_ruido
from src.classifier.marcas import contiene
from src.drilldown import analysis, contexto, lectura, pdf, pipeline, quota, triage, verificacion
from src.drilldown.citas import normalizar, validar_cita, validar_en_documento
from src.llm.contrato import metadatos_prompt
from src.notifier.veredicto_marca import EQUIVALENCIAS
from src.portales import PORTAL_ID_DEFAULT
from src.scraper.models import ProcesoClasificado, ResultadoAnalisis

VERSION = 1
MAX_PLIEGOS = 100
MAX_SEGUNDOS = 3600


class Detener(BaseException):
    """Interrumpe incluso las capas que degradan capturando Exception."""


def configuracion():
    # Config importa dotenv al cargar el módulo: impedir incluso esa lectura.
    with patch("dotenv.load_dotenv", return_value=False):
        from src.config import Config
    return Config()


class ConfigReplay:
    """Vista local: todas las capas no solicitadas quedan apagadas."""

    def __init__(self, cfg, capas, temporal):
        self._cfg = cfg
        for n in range(9):
            setattr(self, f"intel_c{n}", "activo" if f"c{n}" in capas else "off")
        self.drilldown_tmp_dir = str(temporal)
        self.llm_registro_habilitado = False
        self.enable_memoria = False
        self.debug_dir = None
        self.replay_observaciones = []
        self.verificacion_presupuesto_disponible = True
        self.llm_timeout_s = min(getattr(cfg, "llm_timeout_s", 120), 120)

    def __copy__(self):
        copia = object.__new__(type(self))
        copia.__dict__.update(self.__dict__)
        return copia

    def __getattr__(self, nombre):
        if nombre.startswith("_") or "_cfg" not in self.__dict__:
            raise AttributeError(nombre)
        return getattr(self._cfg, nombre)


def ventana_prohibida(ahora=None):
    local = (ahora or datetime.now(ZoneInfo("America/El_Salvador"))).astimezone(
        ZoneInfo("America/El_Salvador"))
    return hora(6, 45) <= local.time().replace(tzinfo=None) < hora(7, 30)


def duracion_segura(maximo, forzar=False, ahora=None):
    """Detiene también la descarga antes de que comience la ventana diaria."""
    if forzar:
        return maximo
    local = (ahora or datetime.now(ZoneInfo("America/El_Salvador"))).astimezone(
        ZoneInfo("America/El_Salvador"))
    inicio = local.replace(hour=6, minute=45, second=0, microsecond=0)
    fin = inicio.replace(hour=7, minute=30)
    if inicio <= local < fin:
        return 0
    if local >= fin:
        inicio += timedelta(days=1)
    # Margen para cerrar Chromium antes de que el timer use la sesión.
    return min(maximo, max(0, (inicio - local).total_seconds() - 5))


def _json(valor, defecto=None):
    if hasattr(valor, "read"):
        valor = valor.read()
    return json.loads(valor) if isinstance(valor, str) else (valor if valor is not None else defecto)


def seleccionar(conn, cliente, *, desde=None, hasta=None, refs=(), solo_relevantes=False, maximo=MAX_PLIEGOS):
    """Fechas UTC inclusivas por día; binds para cada referencia, orden estable."""
    condiciones = ["cp.cliente_id = :cliente"]
    binds = {"cliente": cliente}
    if refs:
        nombres = []
        for i, ref in enumerate(dict.fromkeys(refs)):
            nombre = f"ref{i}"
            nombres.append(":" + nombre)
            binds[nombre] = ref
        condiciones.append("p.numero_referencia IN (" + ",".join(nombres) + ")")
    else:
        condiciones += ["p.fecha_pub_utc >= :desde", "p.fecha_pub_utc < :hasta"]
        binds.update(desde=desde, hasta=hasta + timedelta(days=1))
    if solo_relevantes:
        condiciones.append("cp.es_relevante = 1")
    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT cp.proceso_gid, p.numero_referencia, p.titulo, p.institucion,
                   p.fecha_pub_utc, p.url_portal, p.modalidad_raw, cp.es_relevante,
                   cp.estado, cp.triaje, cp.triaje_conf, cp.ultimo_error, cp.n_paginas,
                   JSON_SERIALIZE(a.matriz_riesgos RETURNING CLOB), cp.razon,
                   JSON_SERIALIZE(cp.marcas RETURNING CLOB)
            FROM cliente_proceso cp JOIN proceso p ON p.proceso_gid = cp.proceso_gid
            LEFT JOIN analisis a ON a.cliente_id = cp.cliente_id AND a.proceso_gid = cp.proceso_gid
            WHERE """ + " AND ".join(condiciones) + " ORDER BY p.fecha_pub_utc, cp.proceso_gid", binds)
        filas = []
        for f in cur.fetchall():
            gid, ref, titulo, institucion, fecha, url, modalidad, relevante, estado, tr, conf, error, paginas, matriz, razon, marcas = f
            previo = ResultadoAnalisis.desde_dict(dict(proceso_uuid=ref, estado="ANALIZADO",
                                                       matriz=_json(matriz)))
            analysis.aplicar_competibilidad(previo, forma_contratacion=modalidad or "")
            filas.append(dict(gid=gid, referencia=ref, proceso=ProcesoClasificado(
                institucion=institucion or "", descripcion=titulo or "", fecha_publicacion=fecha,
                url_directa=url or "", numero_referencia=ref, forma_contratacion=modalidad or "",
                es_relevante=bool(relevante), razon=razon or ""), marcas=_json(marcas, []),
                anterior=dict(estado=estado, triaje=tr, confianza=float(conf or 0),
                              parse_error="parse" in (error or "").lower(),
                              error_llm=estado == "ERROR_LLM", tiene_analisis=matriz is not None,
                              competible=previo.competible if matriz is not None else None),
                matriz_previa=_json(matriz), paginas=int(paginas or 60)))
        return filas[:maximo]
    finally:
        cur.close()


def guardar(ruta, datos):
    ruta = Path(ruta)
    ruta.parent.mkdir(parents=True, exist_ok=True)
    fd, nombre = tempfile.mkstemp(prefix=".replay-", dir=ruta.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as archivo:
            json.dump(datos, archivo, ensure_ascii=False, indent=2, allow_nan=False)
        os.replace(nombre, ruta)
    finally:
        Path(nombre).unlink(missing_ok=True)


def limpiar(raiz):
    """Solo archivos del directorio privado; nunca seguir enlaces simbólicos."""
    raiz = Path(raiz)
    for ruta in raiz.iterdir():
        if ruta.is_symlink() or ruta.is_file():
            ruta.unlink()
        else:
            raise ValueError("Temporal contiene un directorio inesperado")
    raiz.rmdir()


@contextmanager
def logs_privados():
    anterior = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        yield
    finally:
        logging.disable(anterior)


@contextmanager
def limite_tiempo(segundos):
    anterior = signal.getsignal(signal.SIGALRM)
    def detener(*_):
        raise Detener("tiempo agotado")
    signal.signal(signal.SIGALRM, detener)
    signal.setitimer(signal.ITIMER_REAL, segundos)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, anterior)


class Guardia:
    """Reserva durable antes de llamar; un resultado sin uso conserva la reserva."""

    def __init__(self, estado, ruta, raiz, presupuesto, completar):
        self.estado, self.ruta, self.raiz = estado, ruta, raiz
        self.presupuesto, self.completar = presupuesto, completar
        self.gid = ""

    def __call__(self, cfg, pedido, **kwargs):
        if ventana_prohibida() and not self.estado["forzar_ventana"]:
            raise Detener("ventana diaria")
        capa = (pedido.contexto or {}).get("capa", "C0")
        if cfg.intel_c0 == "off" and capa == "C0":
            capa = "base"
        if "paginas_c2" in pedido.nombre_esquema:
            capa = "C2"
        prompt = metadatos_prompt(pedido, cfg)
        prompt["cliente_config"] = self.estado.get("cliente_config")
        hashes = self.estado.setdefault("prompts", {}).setdefault(capa, [])
        if prompt["hash"] not in hashes:
            if hashes:
                aviso = "mezcla de versiones de prompt: " + capa
                avisos = self.estado.setdefault("avisos", [])
                if aviso not in avisos:
                    avisos.append(aviso)
            hashes.append(prompt["hash"])
        modelo = llm.modelo_de(cfg, pedido.etapa)
        clave = hashlib.sha256((self.gid + modelo + llm.prompt_hash(pedido) + str(pedido.max_tokens)
                                + str(pedido.temperatura) + json.dumps(pedido.esquema, sort_keys=True)).encode()).hexdigest()
        cache = self.raiz / (clave + ".respuesta")
        llamadas = self.estado["llamadas"]
        if clave in llamadas:
            if not cache.is_file() or cache.is_symlink():
                raise Detener("llamada ya reservada sin respuesta recuperable; no se repite el cobro")
            datos = json.loads(cache.read_text(encoding="utf-8"))
            datos["uso"] = llm.Uso(**datos["uso"]) if datos["uso"] else None
            respuesta = llm.Respuesta(**datos)
            cfg.replay_observaciones.append((pedido.nombre_esquema, respuesta.texto))
            return respuesta
        reserva = quota.costo_usd(modelo, tokens_de_reserva(pedido), pedido.max_tokens)
        if not math.isfinite(reserva) or reserva <= 0:
            raise Detener("reserva inválida")
        if self.estado["gastado"] + reserva > self.presupuesto:
            raise Detener("presupuesto agotado")
        llamadas[clave] = dict(capa=capa, gid=self.gid, costo=reserva, latencia_ms=0, reservado=True, prompt=prompt)
        self.estado["gastado"] += reserva
        guardar(self.ruta, self.estado)
        inicio = time.monotonic()
        try:
            respuesta = self.completar(cfg, pedido, **kwargs)
            datos = {k: getattr(respuesta, k) for k in (
                "texto", "modelo", "proveedor", "truncada", "logprob_decision")}
            datos["uso"] = asdict(respuesta.uso) if respuesta.uso else None
            guardar(cache, datos)
            cfg.replay_observaciones.append((pedido.nombre_esquema, respuesta.texto))
            if respuesta.uso:
                real = quota.costo_usd(respuesta.modelo or modelo, respuesta.uso.input_tokens,
                                       respuesta.uso.output_tokens)
                if not math.isfinite(real) or real < 0:
                    raise ValueError("Consumo inválido")
                self.estado["gastado"] += real - reserva
                llamadas[clave].update(costo=real, reservado=False)
            return respuesta
        finally:
            llamadas[clave]["latencia_ms"] = round((time.monotonic() - inicio) * 1000)
            guardar(self.ruta, self.estado)


def analizar(fila, ruta, cfg, perfil):
    """Solo funciones de inferencia y derivaciones puras; sin pipeline de estado."""
    cfg.replay_observaciones = []
    proceso = fila["proceso"]
    ctx = pipeline.contexto_cliente(perfil, con_senales=cfg.intel_c0 != "off")
    extra = contexto.armar_contexto(proceso, proceso, fila["marcas"], [], []) if cfg.intel_c1 == "activo" else None
    total = pdf.page_count(ruta)
    escaneado = pdf.is_scanned(ruta)
    texto = pdf.extract_text_first_pages(ruta, 10) if not escaneado else None
    tr = triage.triage(cfg, ctx, texto=texto, pdf_path=ruta, scanned=escaneado,
                       metadatos={"proceso_gid": fila["gid"]}, contexto_proceso=extra,
                       portal_id=getattr(perfil, "portal_id", None) or PORTAL_ID_DEFAULT)
    c2 = dict(c2_selector="vacio", c2_transcritas=0)
    elegidas = lectura.seleccionar_paginas(ruta, total, cfg, {}, diagnostico=c2) if cfg.intel_c2 == "activo" else []
    with llm.contrato.paginas_documento(ruta, elegidas or range(1, min(total, 10) + 1)):
        res = analysis.analizar_pliego(ruta, cfg, ctx, fila["referencia"],
            formato="ESCANEADO" if escaneado else "NATIVO", n_paginas=total,
            senales_triaje=getattr(tr, "datos", {}), contexto_proceso=extra,
            portal_id=getattr(perfil, "portal_id", None) or PORTAL_ID_DEFAULT)
    analysis.aplicar_competibilidad(res, forma_contratacion=proceso.forma_contratacion,
                                   marcas_cliente=pipeline.marcas_del_portafolio(perfil),
                                   nuevo=cfg.intel_c0 == "activo")
    if escaneado:
        leidas = lectura.transcribir_paginas(ruta, elegidas, cfg) if elegidas else []
        c2["c2_transcritas"] = len(leidas)
        paginas = {p["pagina"]: dict(texto=p["texto"], fuente_texto="vision_llm") for p in leidas}
    else:
        import fitz
        with fitz.open(ruta) as documento:
            paginas = {n: dict(texto=p.get_text(), fuente_texto="capa_texto") for n, p in enumerate(documento, 1)}
    exigida = res.marca_exigida or {}
    validacion = validar_en_documento(exigida.get("cita", ""), paginas, exigida.get("pagina"))
    cita_valida = validacion["ok"]
    if cita_valida:
        exigida["pagina"] = validacion["pagina_real"]
    comprobacion = verificacion.Verificacion()
    if cfg.intel_c3 == "activo":
        afirmacion = dict(texto=("El proceso exige la marca " + exigida.get("marca", ""))
                         if exigida else "El pliego impide competir al cliente", negativa=True)
        comprobacion = verificacion.verificar_afirmacion(afirmacion, paginas, cfg,
                                                        modelo_distinto_de=res.modelo_analisis)
        if res.competible is False and (comprobacion.soporta != "si" or comprobacion.solo_vision):
            res.competible = None
    ver = dict(soporta=comprobacion.soporta, cita_valida=validar_en_documento(
        comprobacion.cita, paginas, comprobacion.pagina)["ok"],
        solo_vision=comprobacion.solo_vision, pagina=comprobacion.pagina,
        tiene_cita=bool(comprobacion.cita))
    diagnostico = diagnosticar_respuestas(cfg.replay_observaciones, paginas, n_paginas=total)
    leidas_nativas = list(paginas.values())[:10] if not escaneado else []
    sin_texto = sum(len(p["texto"].strip()) < 40 for p in leidas_nativas)
    formato = "ESCANEADO" if escaneado else (
        "NATIVO_ESCASO" if leidas_nativas and sin_texto / len(leidas_nativas) > .3 else "NATIVO")
    diagnostico["diagnostico"].update(
        **c2, paginas_sin_texto=sin_texto,
        triaje_motivo=recortar(getattr(tr, "datos", {}).get("motivo", tr[2].removeprefix("INCIERTO: ")), 160),
        formato=formato,
        n_paginas=min(total, getattr(cfg, "triage_raster_pages", 5) if escaneado else 10), caracteres_texto=len(texto or ""))
    cfg.replay_observaciones.clear()
    return dict(**diagnostico, triaje="INCIERTO" if tr[2].startswith("INCIERTO:") else tr[0],
                confianza=tr[1], estado=res.estado, ruido=asdict(evaluar_ruido(proceso.descripcion)),
                parse_error=bool((res.anexos or {}).get("parse_error")) or "parse" in (res.error or "").lower() or tr[2] == "parse_error",
                marca_exigida=({"marca": next((marca for marca, aliases in EQUIVALENCIAS.items()
                    if any(contiene(exigida.get("marca", ""), alias) for alias in aliases)), "otra"),
                    "evidencia_valida": cita_valida, "pagina": exigida.get("pagina")}
                    if exigida else None), marca_cita_valida=cita_valida,
                pagina_marca=exigida.get("pagina"), proveedor_unico=res.proveedor_unico,
                carta_fabricante=res.carta_fabricante, equivalente_admitido=res.equivalente_admitido,
                competible=res.competible, paginas=elegidas, verificacion=ver)


def recortar(texto, limite):
    """Suprime contactos e identificadores personales antes de publicar extractos."""
    texto = str(texto or "")
    texto = re.sub(r"[\w.+-]+@[\w.-]+", "[correo]", texto)
    texto = re.sub(r"https?://\S+", "[url]", texto)
    texto = re.sub(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", "[ip]", texto)
    texto = re.sub(r"(?<!\w)\+?\d[\d .()-]{6,}\d", "[identificador]", texto)
    return " ".join(texto.split())[:limite]


def mejor_coincidencia(cita, texto):
    """Ventanas de palabras comparables a las del validador; nunca valida."""
    cita, texto = normalizar(cita), normalizar(texto)
    tokens = list(re.finditer(r"\w+", texto))
    n = max(1, len(re.findall(r"\w+", cita)))
    mejor, fragmento = 0.0, texto[:120]
    for i in range(max(1, len(tokens) - n + 1)):
        if not tokens:
            break
        inicio = tokens[i].start()
        fin = tokens[min(i + n - 1, len(tokens) - 1)].end()
        puntuacion = SequenceMatcher(None, cita, texto[inicio:fin], autojunk=False).ratio()
        if puntuacion > mejor:
            mejor = puntuacion
            fragmento = texto[max(0, inicio - 30):max(0, inicio - 30) + 120]
    return mejor, fragmento


def diagnosticar_cita(fuente, candidata, paginas, n_paginas=None):
    cita, pagina = candidata["cita"], candidata.get("pagina")
    textos = {n: p.get("texto", "") if isinstance(p, dict) else p for n, p in paginas.items()}
    texto = textos.get(pagina, "") if type(pagina) is int else ""
    valida = validar_cita(cita, texto).ok
    local, fragmento_local = mejor_coincidencia(cita, texto)
    global_, fragmento_global = mejor_coincidencia(cita, "\n".join(textos.values()))
    motivo = None
    if not valida:
        if type(pagina) is not int:
            motivo = "sin_pagina"
        elif pagina < 1 or (n_paginas is not None and pagina > n_paginas):
            motivo = "pagina_inexistente"
        elif pagina not in paginas and n_paginas is None:
            motivo = "pagina_inexistente"
        elif not texto.strip():
            motivo = "sin_texto_de_pagina"
        else:
            motivo = "texto_no_coincide"
    documental = validar_en_documento(cita, paginas, pagina)
    salida = dict(pagina_real=documental["pagina_real"], valida_en_documento=documental["ok"],
                  fuente=fuente, cita=recortar(cita, 160), pagina=pagina, valida=valida,
                  similitud=max(local, global_), similitud_pagina=local,
                  similitud_texto=global_, motivo_invalidez=motivo)
    if any(textos.values()):
        salida["fragmento"] = recortar(fragmento_local if local >= global_ else fragmento_global, 120)
    return salida


def diagnosticar_respuestas(observaciones, paginas, *, n_paginas=None):
    """Cuenta también citas emitidas y rechazadas antes de la salida validada."""
    from src.llm.parseo import cargar
    ultimas = {}
    fallos = {}
    reparadas = set()
    emisiones = []
    for nombre, texto in observaciones:
        if nombre not in {"analisis_marcas", "triaje_c0", "verificacion_c3"}:
            continue
        try:
            data = cargar(texto)
            if not isinstance(data, dict):
                raise ValueError("Objeto requerido")
            emisiones.append((nombre, data))
            ultimas[nombre] = data
            if fallos.get(nombre) and nombre != "verificacion_c3":
                reparadas.add(nombre)
        except (ValueError, TypeError):
            if nombre != "verificacion_c3":
                fallos[nombre] = fallos.get(nombre, 0) + 1
    emitidas = invalidas = 0
    citas = []
    for nombre, data in emisiones:
        candidatas = [data.get("marca_exigida")]
        if nombre == "verificacion_c3":
            candidatas.append(data)
        for campo in ("marcas_involucradas", "marcas_mencionadas"):
            if isinstance(data.get(campo), list):
                candidatas.extend(data[campo])
        for candidata in candidatas:
            if not isinstance(candidata, dict) or not candidata.get("cita"):
                continue
            citas.append(diagnosticar_cita(
                {"analisis_marcas": "analisis", "triaje_c0": "triaje", "verificacion_c3": "verificacion"}[nombre],
                candidata, paginas, n_paginas))
            if nombre == "verificacion_c3" or data is not ultimas[nombre]:
                continue
            emitidas += 1
            invalidas += not validar_en_documento(candidata["cita"], paginas, candidata.get("pagina"))["ok"]
    return dict(citas_emitidas=emitidas, citas_emitidas_invalidas=invalidas,
                fallos_parseo_llamadas=sum(fallos.values()), reparaciones_llm=len(reparadas), diagnostico=dict(citas=citas, citas_validas_en_documento=sum(c["valida_en_documento"] for c in citas)))


def metricas(resultados):
    m = dict(comparados=0, triaje_igual=0, triaje_diferente=0, parse_legado=0,
             parse_reparados=0, no_go_parse_corregidos=0, error_llm=0, parse_error=0,
             competible_igual=0, competible_diferente=0, false_a_none=0,
             marca_cita_valida=0, citas=0, citas_invalidas=0, marca_en_selector=0,
             marca_despues_10=0, fallos_parseo_llamadas=0, reparaciones_llm=0, discrepancias=[])
    for ref, r in resultados.items():
        if "nuevo" not in r:
            continue
        a, n = r["anterior"], r["nuevo"]
        m["comparados"] += 1
        m["error_llm"] += n["estado"] == "ERROR_LLM" or n["triaje"] == "ERROR_LLM"
        m["parse_error"] += n["parse_error"]
        reparado = not n["parse_error"] and n["estado"] != "ERROR_LLM" and n["triaje"] != "ERROR_LLM"
        m["parse_legado"] += a["parse_error"]
        m["parse_reparados"] += a["parse_error"] and reparado
        m["no_go_parse_corregidos"] += a["parse_error"] and reparado and a["triaje"] == "NO-GO" and n["triaje"] in {"GO", "INCIERTO"}
        if a["triaje"] in {"GO", "NO-GO"} and n["triaje"] in {"GO", "NO-GO"}:
            m["triaje_igual" if a["triaje"] == n["triaje"] else "triaje_diferente"] += 1
        if a["tiene_analisis"]:
            m["competible_igual" if a["competible"] == n["competible"] else "competible_diferente"] += 1
            m["false_a_none"] += a["competible"] is False and n["competible"] is None
        m["marca_cita_valida"] += n["marca_cita_valida"]
        v = n["verificacion"]
        m["fallos_parseo_llamadas"] += n.get("fallos_parseo_llamadas", 0)
        m["reparaciones_llm"] += n.get("reparaciones_llm", 0)
        m["citas"] += n.get("citas_emitidas", bool(n["marca_exigida"])) + bool(v["tiene_cita"])
        m["citas_invalidas"] += n.get("citas_emitidas_invalidas", bool(n["marca_exigida"]) and not n["marca_cita_valida"]) + (v["tiene_cita"] and not v["cita_valida"])
        pagina = n["pagina_marca"]
        m["marca_en_selector"] += bool(n["marca_cita_valida"] and pagina in n["paginas"])
        m["marca_despues_10"] += bool(n["marca_cita_valida"] and pagina and pagina > 10)
        if len(m["discrepancias"]) < 10 and (a["triaje"] != n["triaje"] or a["competible"] != n["competible"]):
            m["discrepancias"].append(dict(referencia=ref, anterior={k: a[k] for k in ("triaje", "competible")},
                                           nuevo={k: n[k] for k in ("triaje", "competible")}))
    m["tasa_reparacion"] = m["parse_reparados"] / m["parse_legado"] if m["parse_legado"] else None
    m["tasa_citas_invalidas"] = m["citas_invalidas"] / m["citas"] if m["citas"] else None
    motivos, formatos, inciertos = Counter(), {}, Counter()
    similitudes = []
    validas_documento = 0
    for r in resultados.values():
        nuevo = r.get("nuevo", {})
        d = nuevo.get("diagnostico", {})
        formato = d.get("formato", "DESCONOCIDO")
        for cita in d.get("citas", []):
            validas_documento += bool(cita.get("valida_en_documento", cita["valida"]))
            if not cita["valida"]:
                motivo = cita["motivo_invalidez"]
                motivos[motivo] += 1
                formatos.setdefault(formato, Counter())[motivo] += 1
                similitudes.append(cita["similitud"])
        if nuevo.get("triaje") == "INCIERTO":
            inciertos[normalizar(d.get("triaje_motivo", ""))] += 1
    m["diagnostico"] = dict(citas_validas_en_documento=validas_documento, invalidas_por_motivo=dict(motivos),
        invalidas_por_formato={f: dict(c) for f, c in formatos.items()},
        similitud_mediana_invalidas=median(similitudes) if similitudes else None,
        motivos_incierto=[dict(motivo=t, cantidad=c) for t, c in inciertos.most_common(5)])
    return m


# Tokens por llamada MEDIDOS en DEV (docs/planes/evaluacion-precision-triaje.md: triaje 12,9 k de entrada y 75 de salida,
# análisis 25,6 k y 504) más lo que agregan las capas nuevas: contexto C1 (+2 k), selector de páginas C2 (miniaturas,
# ~8 k), verificador C3 (~5 k, hasta dos llamadas). Con `MARGEN` por pliegos más grandes que la mediana.
_LLAMADAS_TIPICAS = (
    ("triaje", 12_900 + 2_000, 200),
    ("analisis", 25_600 + 2_000, 600),
    ("triaje", 8_000, 300),           # selector C2
    ("triaje", 5_000, 200),           # verificador C3 (1.ª)
    ("triaje", 5_000, 200),           # verificador C3 (2.ª)
)
MARGEN = 1.5
BYTES_POR_TOKEN = 3                   # texto plano: conservador frente a ~4-5.5 chars/token
TOKENS_POR_PAGINA = 3000              # página de PDF (capa de texto ~700 tokens; escaneada ~1.500-2.500 como imagen)
TOKENS_POR_IMAGEN = 2500
TOPE_TOKENS_DOCUMENTO = 150_000


def tokens_de_reserva(pedido) -> int:
    """Tokens de ENTRADA que se reservan antes de una llamada: texto a ~3 caracteres por token, imágenes y páginas de
    PDF por su costo típico. NO se cuentan los bytes del archivo ni del base64 (ese error reservaba decenas de
    dólares por llamada y detenía el repaso a la primera: las imágenes no se facturan por su base64)."""
    caracteres = len(pedido.sistema or "")
    tokens = 1024
    for bloque in pedido.contenido:
        if isinstance(bloque, llm.BloqueTexto):
            caracteres += len(bloque.texto)
        elif isinstance(bloque, llm.BloqueImagen):
            tokens += TOKENS_POR_IMAGEN
        elif isinstance(bloque, llm.BloqueDocumento):
            tokens += min(max(bloque.paginas, 1) * TOKENS_POR_PAGINA, TOPE_TOKENS_DOCUMENTO)
    return tokens + caracteres // BYTES_POR_TOKEN


def estimacion(filas, cfg):
    """Pre-vuelo: costo por proceso con las llamadas típicas medidas, no con el tamaño máximo de un pliego."""
    n = len(filas)
    por_proceso = sum(quota.costo_usd(llm.modelo_de(cfg, etapa), tin, tout) for etapa, tin, tout in _LLAMADAS_TIPICAS)
    conservador = n * por_proceso * MARGEN
    return dict(procesos=n, analisis_previos=sum(f["anterior"]["tiene_analisis"] for f in filas),
                descargas=n, conservador=conservador, orientativo=conservador / 2.3)


def persistir_decisiones(estado, filas, cliente, conectar):
    """Única escritura ADB permitida; el checkpoint permite reintentarla sin LLM."""
    if conectar is None:
        return
    from src.aprendizaje import repo
    estado.pop("integracion_pendiente", None)
    guardadas = estado.setdefault("decisiones_guardadas", [])
    estado["fallos_decision"] = 0
    gids = {f["referencia"]: f["gid"] for f in filas}
    for referencia, resultado in estado["resultados"].items():
        if "nuevo" not in resultado:
            continue
        for capa in estado["capas"]:
            clave = referencia + ":" + capa
            if clave in guardadas:
                continue
            try:
                with logs_privados(), conectar() as conn:
                    llamadas = [llamada for llamada in estado["llamadas"].values()
                                if llamada.get("gid") == gids[referencia] and llamada["capa"] == capa.upper()]
                    repo.guardar_decision(conn, cliente, gids[referencia], capa.upper(),
                        version=str(VERSION), variante="replay", salida={**resultado,
                            "prompt": (llamadas[-1].get("prompt") if llamadas else None) or
                                dict(hash=None, app=estado.get("app", "dev"),
                                     cliente_config=estado.get("cliente_config")),
                            "prompts": [llamada["prompt"] for llamada in llamadas if llamada.get("prompt")]},
                        costo_usd=sum(llamada["costo"] for llamada in llamadas),
                        latencia_ms=sum(llamada["latencia_ms"] for llamada in llamadas),
                        entrada_hash=hashlib.sha256((estado["firma"] + clave + json.dumps(
                            [llamada.get("prompt", {}).get("hash") for llamada in llamadas])).encode()).hexdigest())
                    conn.commit()
                guardadas.append(clave)
            except Exception:
                estado["fallos_decision"] += 1


def ejecutar(filas, cfg, perfil, args, *, conectar_decisiones=None, descargar=None, inferir=analizar, decir=print):
    inicio = time.monotonic()
    ids = [f["gid"] for f in filas]
    capas = sorted(set(args.capas.split(",")))
    firma = hashlib.sha256((repr(perfil) + repr([
        llm.modelo_de(cfg, etapa) for etapa in ("triaje", "analisis", "verificacion")
    ])).encode()).hexdigest()
    checkpoint = args.checkpoint or args.reanudar or Path("replay_pliegos.checkpoint.json")
    if args.reanudar:
        estado = json.loads(args.reanudar.read_text(encoding="utf-8"))
        if (estado.get("version") != VERSION or estado.get("ids") != ids
                or estado.get("cliente") != args.cliente or estado.get("capas") != capas
                or estado.get("firma") != firma
                or not math.isfinite(estado["gastado"]) or estado["gastado"] < 0):
            raise ValueError("Checkpoint incompatible")
        estado.setdefault("prompts", {})
        raiz = Path(estado["temporal"])
        if raiz.is_symlink() or raiz.parent != Path(tempfile.gettempdir()).resolve() or not raiz.name.startswith("amalia-replay-"):
            raise ValueError("Temporal ajeno")
        if estado.get("completo"):
            with limite_tiempo(args.tiempo_max_s):
                persistir_decisiones(estado, filas, args.cliente, conectar_decisiones)
            guardar(checkpoint, estado)
            return estado
        if not raiz.is_dir():
            raise ValueError("Temporal de reanudación ausente")
    else:
        raiz = Path(tempfile.mkdtemp(prefix="amalia-replay-")).resolve()
        estado = dict(version=VERSION, ids=ids, cliente=args.cliente, capas=capas, firma=firma,
                      temporal=str(raiz), referencias=[f["referencia"] for f in filas],
                      resultados={}, llamadas={}, gastado=0.0, pdfs={}, completo=False)
    estado.setdefault("prompts", {})
    estado["app"] = getattr(cfg, "app_version", None) or "dev"
    # El perfil del replay puede venir del archivo (sin versión de ADB): se identifica por la huella de su contenido.
    estado["cliente_config"] = (getattr(perfil, "version", None)
                                or (hash_contenido(dataclasses.asdict(perfil))[:12] if dataclasses.is_dataclass(perfil) else None))
    estado.pop("corte", None)
    estado["forzar_ventana"] = args.forzar_ventana
    local = ConfigReplay(cfg, capas, raiz)
    guardia = Guardia(estado, checkpoint, raiz, args.presupuesto_usd, llm.completar)
    guardar(checkpoint, estado)
    pendientes = [f for f in filas if f["referencia"] not in estado["resultados"]]
    try:
        with limite_tiempo(args.tiempo_max_s), ExitStack() as stack:
            # Los módulos vecinos pueden registrar errores con URLs o texto.
            stack.enter_context(logs_privados())
            if not args.sin_descarga:
                bajar = descargar or pipeline.descargar_pliegos
                procesos = [f["proceso"] for f in pendientes if f["referencia"] not in estado["pdfs"]]
                nuevos = asyncio.run(bajar(procesos, local))
                for ref, ruta in nuevos.items():
                    p = Path(ruta)
                    if p.is_symlink() or p.resolve().parent != raiz:
                        raise ValueError("Descarga fuera del temporal")
                    estado["pdfs"][ref] = str(p.resolve())
                guardar(checkpoint, estado)
            decir(f"Descargados {len(estado['pdfs'])}; no disponibles {len(pendientes) - sum(f['referencia'] in estado['pdfs'] for f in pendientes)}")
            stack.enter_context(patch.object(llm, "completar", guardia))
            stack.enter_context(patch.object(quota, "cabe_lectura", return_value=True))
            stack.enter_context(patch.object(quota, "cobrar_lectura", return_value=0))
            for fila in pendientes:
                ref = fila["referencia"]
                ruta = estado["pdfs"].get(ref)
                if not ruta:
                    estado["resultados"][ref] = dict(no_disponible=True)
                    guardar(checkpoint, estado)
                    continue
                if Path(ruta).is_symlink() or Path(ruta).resolve().parent != raiz:
                    raise ValueError("PDF fuera del temporal")
                guardia.gid = fila["gid"]
                try:
                    nuevo = inferir(fila, ruta, local, perfil)
                    resultado = dict(anterior=fila["anterior"], nuevo=nuevo)
                except Exception:
                    resultado = dict(error=True)
                estado["resultados"][ref] = resultado
                guardar(checkpoint, estado)
    except Detener as exc:
        estado["corte"] = str(exc)
    finally:
        estado["completo"] = len(estado["resultados"]) == len(filas)
        estado["metricas"] = metricas(estado["resultados"])
        estado["medias_por_capa"] = {}
        for capa in {llamada["capa"] for llamada in estado["llamadas"].values()}:
            llamadas = [llamada for llamada in estado["llamadas"].values() if llamada["capa"] == capa]
            estado["medias_por_capa"][capa] = dict(llamadas=len(llamadas), costo=sum(llamada["costo"] for llamada in llamadas) / len(llamadas),
                latencia_ms=sum(llamada["latencia_ms"] for llamada in llamadas) / len(llamadas))
        if "c1" in capas:
            estado["medias_por_capa"]["C1"] = dict(llamadas=0, costo=0, latencia_ms=0,
                nota="Contexto incorporado en C0/análisis; sin llamada adicional")
        try:
            with limite_tiempo(max(.001, args.tiempo_max_s - (time.monotonic() - inicio))):
                persistir_decisiones(estado, filas, args.cliente, conectar_decisiones)
        except Detener:
            estado["corte_decisiones"] = "tiempo agotado; decisiones conservadas en checkpoint"
        guardar(checkpoint, estado)
        if estado["completo"]:
            limpiar(raiz)
    return estado


def main(argv=None, *, conectar=None, cfg=None, decir=print):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cliente", required=True)
    seleccion = parser.add_mutually_exclusive_group()
    seleccion.add_argument("--refs")
    seleccion.add_argument("--archivo", type=Path)
    parser.add_argument("--desde", type=lambda texto: datetime.strptime(texto, "%Y-%m-%d"))
    parser.add_argument("--hasta", type=lambda texto: datetime.strptime(texto, "%Y-%m-%d"))
    parser.add_argument("--max", type=int, default=MAX_PLIEGOS)
    parser.add_argument("--solo-relevantes", action="store_true")
    parser.add_argument("--capas")
    parser.add_argument("--presupuesto-usd", type=float, default=3.00)
    parser.add_argument("--tiempo-max-s", type=int, default=MAX_SEGUNDOS)
    for nombre in ("estimar", "sin-descarga", "forzar-ventana"):
        parser.add_argument("--" + nombre, action="store_true")
    for nombre in ("checkpoint", "reanudar", "json"):
        parser.add_argument("--" + nombre, type=Path)
    args = parser.parse_args(argv)
    if args.reanudar:
        try:
            previo = json.loads(args.reanudar.read_text(encoding="utf-8"))
            if not (args.refs or args.archivo or args.desde or args.hasta):
                args.refs = ",".join(previo["referencias"])
            args.capas = args.capas or ",".join(previo["capas"])
        except (OSError, ValueError, KeyError, TypeError):
            parser.error("Checkpoint ilegible")
    args.capas = args.capas or "c0,c1,c2,c3"
    if not math.isfinite(args.presupuesto_usd) or args.presupuesto_usd <= 0:
        parser.error("Presupuesto finito y positivo requerido")
    if not 1 <= args.max <= MAX_PLIEGOS or not 1 <= args.tiempo_max_s <= MAX_SEGUNDOS:
        parser.error("Límites: 1–100 pliegos y 1–3600 segundos")
    if not set(args.capas.split(",")) <= {"c0", "c1", "c2", "c3"}:
        parser.error("Capas válidas: c0,c1,c2,c3")
    if ((args.refs or args.archivo) and (args.desde or args.hasta)) or not (
            args.refs or args.archivo or (args.desde and args.hasta)):
        parser.error("Indique fechas completas, --refs o --archivo")
    if args.desde and args.hasta and args.desde > args.hasta:
        parser.error("Rango de fechas invertido")
    if ventana_prohibida() and not args.forzar_ventana:
        decir("Replay bloqueado entre 06:45 y 07:30 de El Salvador")
        return 2
    try:
        args.cliente = id_canonico(args.cliente)
        cfg = cfg or configuracion()
        if conectar is None:
            from src.memoria import db
            def conectar():
                return db.connection(cfg)
        refs = args.refs.split(",") if args.refs else (args.archivo.read_text(encoding="utf-8").replace(",", "\n").splitlines() if args.archivo else [])
        refs = [r.strip() for r in refs if r.strip()]
        if (args.refs or args.archivo) and not refs:
            raise ValueError("Lista vacía")
        args.tiempo_max_s = duracion_segura(args.tiempo_max_s, args.forzar_ventana)
        if args.tiempo_max_s <= 0:
            decir("Replay bloqueado por proximidad de la ventana diaria")
            return 2
        inicio = time.monotonic()
        with limite_tiempo(args.tiempo_max_s), logs_privados(), conectar() as conn:
            cur = conn.cursor()
            try:
                cur.execute("SET TRANSACTION READ ONLY")
            finally:
                cur.close()
            filas = seleccionar(conn, args.cliente, desde=args.desde, hasta=args.hasta, refs=refs,
                                solo_relevantes=args.solo_relevantes, maximo=args.max)
            if not args.estimar:
                from src.classifier.client_profile import _parse_profile
                from src.clientes import repo
                activo = repo.activo(conn, args.cliente)
                if activo is None:
                    raise ValueError("Sin perfil activo")
                perfil = _parse_profile(activo[1], "replay")
                for fila in filas:
                    if fila.get("matriz_previa") is not None:
                        previo = ResultadoAnalisis.desde_dict(dict(proceso_uuid=fila["referencia"],
                            estado="ANALIZADO", matriz=fila["matriz_previa"]))
                        analysis.aplicar_competibilidad(previo,
                            forma_contratacion=fila["proceso"].forma_contratacion,
                            marcas_cliente=pipeline.marcas_del_portafolio(perfil))
                        fila["anterior"]["competible"] = previo.competible
                        fila["anterior"]["competibilidad_reconstruida"] = True
        args.tiempo_max_s = max(.001, args.tiempo_max_s - (time.monotonic() - inicio))
        estimado = estimacion(filas, cfg)
        decir(f"{estimado['procesos']} procesos; {estimado['analisis_previos']} con análisis previo; "
              f"{estimado['descargas']} descargas; estimado conservador US${estimado['conservador']:.4f}; "
              f"orientativo ≈ real/2,3: US${estimado['orientativo']:.4f}")
        if args.estimar:
            resultado = dict(estimacion=estimado)
        else:
            resultado = ejecutar(filas, cfg, perfil, args, conectar_decisiones=conectar, decir=decir)
            n, total = len(resultado["resultados"]), len(filas)
            restante = estimado["conservador"] * (total - n) / max(total, 1)
            reservados = sum(llamada["costo"] for llamada in resultado.get("llamadas", {}).values()
                             if llamada.get("reservado"))
            resultado["balance"] = dict(procesados=n, total=total, gastado=resultado["gastado"],
                gasto_real_confirmado=resultado["gastado"] - reservados, reservado_sin_uso=reservados,
                presupuesto=args.presupuesto_usd, faltan=total - n, estimado_restante=restante,
                adicional_recomendado=max(0, restante * 1.1 - max(0, args.presupuesto_usd - resultado["gastado"])))
            decir("REPORTE FINAL: " + json.dumps(resultado["balance"], ensure_ascii=False))
            decir("Prompts: " + json.dumps(resultado.get("prompts", {})))
            for aviso in resultado.get("avisos", []):
                decir(aviso)
            decir("Acuerdo: " + json.dumps(resultado["metricas"], ensure_ascii=False))
            decir("Costo y latencia: " + json.dumps(resultado["medias_por_capa"]))
            if resultado.get("fallos_decision"):
                decir(f"Decisiones no persistidas: {resultado['fallos_decision']}; conservadas en checkpoint")
            if not resultado["completo"]:
                decir("Temporal privado conservado para reanudar sin repetir llamadas pagadas.")
        if args.json:
            guardar(args.json, resultado)
        if not args.estimar:
            if not resultado["completo"]:
                return 3
            if any(r.get("error") for r in resultado["resultados"].values()):
                return 1
        return 0
    except Detener:
        decir("Replay interrumpido por límite de tiempo")
        return 3
    except Exception as exc:
        decir(f"Replay no disponible ({type(exc).__name__}); no se imprimen detalles sensibles")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
