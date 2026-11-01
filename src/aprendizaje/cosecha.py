"""Cosecha conservadora: agrega ejemplos sin modificar la verdad humana.

Feedback no distingue votos de validación: requieren otra clase de fuente,
incluso si hay varios destinatarios. El llamador administra commit y rollback.
"""
from __future__ import annotations

from collections import defaultdict

from src.aprendizaje import repo
from src.classifier.marcas import contiene, normalizar, validar_evidencia

PESOS = {"voto_usuario": 0.6, "resultado_comercial": 0.9, "participacion": 0.8, "pliego": 0.25}


def preparar_senales(senales, existentes=()):
    """Deduplica y exige corroboración de fuente distinta para etiquetas débiles."""
    protegidos = {(e.get("proceso_gid"), e["marca_id"]) for e in existentes
                  if e.get("fuente") == "humana" or e.get("split") in {"critico", "prueba"}}
    previos = {(e.get("proceso_gid"), e["marca_id"], e["fuente"], e["etiqueta"]) for e in existentes}
    grupos = defaultdict(dict)
    for s in senales:
        if (s.get("validacion") or s.get("prueba") or s.get("fuente") not in PESOS
                or not s.get("proceso_gid") or not s.get("marca_id") or not s.get("titulo_texto")):
            continue
        if s.get("etiqueta") not in repo.ETIQUETAS:
            continue
        grupos[(s["proceso_gid"], s["marca_id"])][(s["fuente"], s["etiqueta"])] = s
    salida = []
    for clave, grupo in sorted(grupos.items()):
        if clave in protegidos or len({s["etiqueta"] == "positivo" for s in grupo.values()}) > 1:
            continue
        fuentes = {s["fuente"] for s in grupo.values()}
        for s in grupo.values():
            if s["fuente"] in {"voto_usuario", "pliego"} and len(fuentes) < 2:
                continue
            if (*clave, s["fuente"], s["etiqueta"]) not in previos:
                salida.append({**s, "peso": PESOS[s["fuente"]]})
    return salida


def _marcas(titulo, terminos):
    return {t["marca_id"] for t in terminos if t["tipo"] in {"fuerte", "ajena"}
            and normalizar(t["termino"]) != "crystal" and contiene(titulo, t["termino"])}


def leer_senales(conn, cliente_id):
    """Exige una marca explícita; jamás cosecha las recomendaciones del LLM."""
    terminos = repo.listar_terminos_activos(conn, cliente_id)
    binds = {"cliente_id": cliente_id}
    senales = []
    votos = repo._todos(conn, """
        SELECT f.proceso_gid, p.titulo, f.voto, f.motivo,
               JSON_SERIALIZE(f.motivos RETURNING CLOB)
        FROM feedback f JOIN proceso p ON p.proceso_gid = f.proceso_gid
        WHERE f.cliente_id = :cliente_id
    """, binds)
    for gid, titulo, voto, motivo, motivos_json in votos:
        motivos = repo._a_json(motivos_json) or []
        if voto == "RELEVANTE":
            etiqueta = "positivo"
        elif motivo == "marca_no_representada" or "marca_no_representada" in motivos:
            etiqueta = "ajena"
        else:
            # Plazos, monto e interés comercial no niegan una marca.
            continue
        for marca in _marcas(titulo, terminos):
            senales.append(dict(proceso_gid=gid, titulo_texto=titulo, marca_id=marca,
                                fuente="voto_usuario", etiqueta=etiqueta))
    comerciales = repo._todos(conn, """
        SELECT o.proceso_gid, p.titulo, o.estado, o.motivo FROM oportunidad_estado o
        JOIN proceso p ON p.proceso_gid = o.proceso_gid WHERE o.cliente_id = :cliente_id
    """, binds)
    for gid, titulo, estado, motivo in comerciales:
        etiqueta = "positivo" if estado in {"OFERTADO", "GANADO"} else None
        if estado == "DESCARTADO" and normalizar(motivo or "") == "no es nuestra marca":
            etiqueta = "negativo"
        if etiqueta:
            for marca in _marcas(titulo, terminos):
                senales.append(dict(proceso_gid=gid, titulo_texto=titulo, marca_id=marca,
                                    fuente="resultado_comercial", etiqueta=etiqueta))
    participaciones = repo._todos(conn, """
        SELECT p.proceso_gid, p.titulo FROM proceso_participado pp JOIN proceso p
        ON p.numero_referencia = pp.codigo_proceso JOIN cliente_portal c
        ON c.cliente_id = pp.cliente_id AND c.portal_id = p.portal_id AND c.activo = 1
        WHERE pp.cliente_id = :cliente_id
    """, binds)
    for gid, titulo in participaciones:
        for marca in _marcas(titulo, terminos):
            senales.append(dict(proceso_gid=gid, titulo_texto=titulo, marca_id=marca,
                                fuente="participacion", etiqueta="positivo"))
    pliegos = repo._todos(conn, """
        SELECT d.proceso_gid, p.titulo, JSON_SERIALIZE(d.salida RETURNING CLOB),
               pg.pagina, pg.texto FROM decision_capa d JOIN proceso p ON p.proceso_gid = d.proceso_gid
        JOIN pagina_pliego pg ON pg.proceso_gid = d.proceso_gid
        WHERE d.cliente_id = :cliente_id AND d.capa = 'pliego' AND d.variante = 'activa'
        AND d.score >= 0.95
    """, binds)
    for gid, titulo, crudo, pagina, texto in pliegos:
        datos = repo._a_json(crudo)
        exigida = datos.get("marca_exigida") if isinstance(datos, dict) else None
        if not isinstance(exigida, dict) or exigida.get("pagina") != pagina:
            continue
        marca, cita = exigida.get("marca"), exigida.get("cita")
        fuertes = [t for t in terminos if t["marca_id"] == marca and t["tipo"] in {"fuerte", "ajena"}]
        texto = texto.read() if hasattr(texto, "read") else texto
        if validar_evidencia(cita, texto, [t["termino"] for t in fuertes]):
            senales.append(dict(proceso_gid=gid, titulo_texto=titulo, marca_id=marca,
                                fuente="pliego", etiqueta="ajena" if all(t["tipo"] == "ajena" for t in fuertes)
                                else "positivo", evidencia=cita, pagina=pagina))
    return senales


def ejecutar(conn, cliente_id, aplicar=False):
    """Previsualiza por defecto; agregar_ejemplo calcula el embedding en la base."""
    existentes = repo._todos(conn, """
        SELECT proceso_gid, marca_id, fuente, etiqueta, split FROM ejemplo_marca
        WHERE cliente_id = :cliente_id
    """, {"cliente_id": cliente_id})
    existentes = [dict(zip(("proceso_gid", "marca_id", "fuente", "etiqueta", "split"), f))
                  for f in existentes]
    senales = leer_senales(conn, cliente_id)
    candidatos = preparar_senales(senales, existentes)
    nuevos = 0
    if aplicar and candidatos:
        repo._exigir_auditable("EJEMPLO_ETIQUETADO")
        for s in candidatos:
            resultado = repo.agregar_ejemplo(conn, cliente_id, s["marca_id"], s["etiqueta"],
                                            s["fuente"], s["titulo_texto"], proceso_gid=s["proceso_gid"],
                                            peso=s["peso"], evidencia=s.get("evidencia"),
                                            pagina=s.get("pagina"), split="entrenamiento")
            nuevos += bool(resultado["nuevo"])
            if resultado["nuevo"]:
                repo._auditar(conn, cliente_id, "EJEMPLO_ETIQUETADO", "ciclo",
                              f"ejemplo_id={resultado['ejemplo_id']};fuente={s['fuente']}")
    return {"candidatos": len(candidatos), "nuevos": nuevos, "omitidos": len(senales) - len(candidatos)}
