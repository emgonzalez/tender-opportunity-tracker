"""Semilla del diccionario por marca (`marca_termino`): solo nombres PÚBLICOS de productos, nada de clientes.

Se carga con ``python -m src.aprendizaje semilla --aplicar`` (dry-run por defecto) en estado ``semilla``;
desde ahí el ciclo de aprendizaje propone, activa o rechaza términos con auditoría. Los términos se guardan
tal cual (con mayúsculas y tildes); la comparación por palabra completa y sin tildes la hace el
clasificador, no este módulo.

Reglas del diccionario:
  * ``fuerte``  — confirma la marca por sí solo (salvo que un ``excluye`` también aparezca).
  * ``debil``   — sugiere la marca, nunca la confirma: solo lleva a «Por confirmar».
  * ``excluye`` — contrapatrón: el proceso habla de la marca para salir de ella.
  * ``ajena``   — alias de una marca que el cliente NO representa (el ``marca_id`` es la marca ajena).
  * Nunca «Crystal» solo: «Crystal Ball» es Oracle y «Crystal Reports» es SAP.
"""
from __future__ import annotations

import unicodedata
from typing import Iterable, Optional

SEMILLA: dict[str, dict[str, tuple[str, ...]]] = {
    "oracle": {
        "fuerte": (
            "Oracle", "OCI", "Oracle Cloud", "Exadata", "ZDLRA", "WebLogic", "Cloud@Customer", "NetSuite",
            "MySQL", "Java SE", "Solaris", "SPARC", "PeopleSoft", "JD Edwards", "Hyperion", "Siebel",
            "Primavera", "derechos de actualización por procesador",
        ),
        # Se conserva como indicio de dominio, nunca como confirmación de Oracle.
        "debil": ("base de datos",),
        "excluye": ("migración de oracle a", "migrar de oracle"),
    },
    "ibm": {"ajena": ("IBM", "Cognos", "SPSS", "Db2", "Informix", "WebSphere", "Maximo", "QRadar")},
    "microsoft": {"ajena": ("Power BI", "SQL Server", "Dynamics")},
    "salesforce": {"ajena": ("Tableau", "Salesforce")},
    "sap": {"ajena": ("SAP", "HANA", "BusinessObjects", "Crystal Reports")},
    "qlik": {"ajena": ("Qlik", "QlikView", "Qlik Sense")},
}

# Términos que la semilla jamás debe traer por ambiguos (ver el docstring).
PROHIBIDOS = frozenset({"crystal"})


def terminos_semilla() -> list[tuple[str, str, str]]:
    """(marca_id, termino, tipo) de toda la semilla, en orden estable."""
    return [(marca, termino, tipo)
            for marca, por_tipo in SEMILLA.items()
            for tipo, terminos in por_tipo.items()
            for termino in terminos]


def filtrar_por_portafolio(terminos: Iterable[tuple[str, str, str]],
                           portafolio: Optional[set[str]]) -> tuple[list[tuple[str, str, str]], list[str]]:
    """Ajusta la semilla al portafolio del cliente.

    Los términos propios (fuerte/débil/excluye) solo se siembran si el cliente representa esa marca; los
    ``ajena`` solo si NO la representa (sembrar como ajena una marca del portafolio la descartaría).
    Con ``portafolio`` None (perfil ilegible) se siembra todo y se avisa.

    :return: (términos a sembrar, avisos).
    """
    lista = list(terminos)
    if portafolio is None:
        return lista, ["no se pudo leer el portafolio del cliente: se siembra la semilla completa"]
    salida, omitidas = [], set()
    for marca, termino, tipo in lista:
        representada = marca in portafolio
        if (tipo == "ajena") == representada:
            omitidas.add((marca, "ajena del portafolio" if representada else "no está en el portafolio"))
            continue
        salida.append((marca, termino, tipo))
    avisos = [f"se omite '{m}': {motivo}" for m, motivo in sorted(omitidas)]
    return salida, avisos


GENERICOS = frozenset("""
servidor servidores software licencia licencias licenciamiento soporte redes red nube cloud
seguridad almacenamiento respaldo respaldos equipo equipos sistema sistemas plataforma plataformas
power hardware servicios servicio soluciones solucion infraestructura tecnologia aplicaciones
aplicacion datos database networking backup ups
""".split())
OBSOLETOS = frozenset({("oracle", t, "debil")
                       for t in ("licenciamiento", "soporte técnico", "nube")})


def _normalizar(texto: str) -> str:
    return " ".join("".join(c for c in unicodedata.normalize("NFKD", texto)
                            if not unicodedata.combining(c)).casefold().split())


def terminos_catalogo(perfil: dict) -> list[tuple[str, str, str]]:
    """Alias públicos del catálogo, sin genéricos ni alias ajenos de otra marca."""
    ajenas = [(_normalizar(t), m) for m, t, tipo in terminos_semilla() if tipo == "ajena"]
    candidatos = []
    for categoria in (perfil.get("catalog") or {}).get("categories") or []:
        for marca in categoria.get("brands") or []:
            mid = _normalizar(str(marca.get("id") or ""))
            if not mid:
                continue
            nombre = marca.get("name") or ""
            alias = [nombre, *(marca.get("products") or [])]
            if mid != _normalizar(nombre):
                alias.append(mid)
            for termino in alias:
                if not isinstance(termino, str):
                    continue
                termino = " ".join(termino.split())
                normal = _normalizar(termino)
                if (len(normal) < 3 or len(termino.encode("utf-8")) > 120
                        or normal in GENERICOS or normal in PROHIBIDOS
                        or any(normal == ajena and mid != otra for ajena, otra in ajenas)):
                    continue
                candidatos.append((mid, termino, "fuerte"))
    vistos, salida = set(), []
    for fila in sorted(candidatos):
        clave = (fila[0], _normalizar(fila[1]), fila[2])
        if clave not in vistos:
            vistos.add(clave)
            salida.append(fila)
    return salida


def _sembrar_catalogo(conn, cid, terminos, actor):
    """MERGE solo de inserción; misma validación y auditoría que la semilla fija."""
    from src.aprendizaje import repo
    lote = [{"cliente_id": cid, "marca_id": m, "termino": repo._validar_termino(m, t, tipo),
             "tipo": tipo, "creado_por": actor[:64]} for m, t, tipo in terminos]
    if not lote:
        return 0
    repo._exigir_auditable(repo.ACCION_TERMINOS_SEMBRADOS)
    with conn.cursor() as cursor:
        cursor.executemany("""
            MERGE INTO marca_termino t
            USING (SELECT :cliente_id cliente_id, :marca_id marca_id, :termino termino, :tipo tipo
                     FROM dual) s
            ON (t.cliente_id=s.cliente_id AND t.marca_id=s.marca_id
                AND t.termino=s.termino AND t.tipo=s.tipo)
            WHEN NOT MATCHED THEN
              INSERT (cliente_id, marca_id, termino, tipo, estado, origen, activo, creado_por)
              VALUES (s.cliente_id, s.marca_id, s.termino, s.tipo, 'semilla', 'catalogo', 1, :creado_por)
        """, lote)
        cantidad = cursor.rowcount or 0
    if cantidad:
        repo._auditar(conn, cid, repo.ACCION_TERMINOS_SEMBRADOS, actor,
                      f"cliente={cid};insertados={cantidad};origen=catalogo")
    return cantidad


def _rechazar_obsoleto(conn, cid, fila, actor):
    """Revalida bajo bloqueo para respetar una edición humana concurrente."""
    from src.aprendizaje import repo
    with conn.cursor() as cursor:
        cursor.execute(
            "SELECT estado, origen, version, marca_id, termino, tipo FROM marca_termino "
            "WHERE cliente_id = :cliente_id AND termino_id = :termino_id FOR UPDATE",
            {"cliente_id": cid, "termino_id": fila["termino_id"]},
        )
        actual = cursor.fetchone()
    if (not actual or actual[0:3] != ("semilla", "semilla", 1)
            or tuple(actual[3:6]) not in OBSOLETOS):
        return False
    return repo.rechazar_termino(conn, fila["termino_id"], actor=actor)


def ejecutar(conn, args, portafolio, decir, quien):
    """Plan determinista y aplicación explícita de semilla y retiros."""
    from src.aprendizaje import repo
    from src.clientes import repo as clientes_repo
    cid = args.cliente
    fijas, avisos = filtrar_por_portafolio(terminos_semilla(), portafolio)
    activo = clientes_repo.activo(conn, cid)
    catalogo = terminos_catalogo(activo[1]) if activo else []
    for aviso in avisos:
        decir(f"aviso: {aviso}")
    existentes = repo.terminos_existentes(conn, cid)
    ajenas_existentes = [(_normalizar(t), m) for m, t, tipo in existentes if tipo == "ajena"]
    catalogo = [(m, t, tipo) for m, t, tipo in catalogo
                if not any(_normalizar(t) == alias and m != otra
                           for alias, otra in ajenas_existentes)]
    claves = {(m, _normalizar(t), tipo) for m, t, tipo in existentes}
    nuevos = {"semilla": [], "catalogo": []}
    candidatos = []
    vistos = set()
    for origen, filas in (("semilla", fijas), ("catalogo", catalogo)):
        for m, t, tipo in filas:
            clave = (m, _normalizar(t), tipo)
            if clave in vistos:
                continue
            vistos.add(clave)
            existe = clave in claves
            candidatos.append((m, t, tipo, existe))
            if not existe:
                nuevos[origen].append((m, t, tipo))
    cantidad = sum(map(len, nuevos.values()))
    decir(f"{cid}: {len(candidatos)} términos de semilla, "
          f"{len(candidatos) - cantidad} ya existen, {cantidad} por insertar")
    for m, t, tipo, existe in sorted(candidatos):
        decir(f"  {'=' if existe else '+'} {m:<12} {tipo:<8} {t}")
    retiros = []
    if args.retirar_obsoletos:
        retiros = [f for f in repo.listar_terminos_activos(conn, cid)
                   if f.get("estado") == "semilla" and f.get("origen") == "semilla"
                   and f.get("version") == 1
                   and (f["marca_id"], f["termino"], f["tipo"]) in OBSOLETOS]
        for f in sorted(retiros, key=lambda f: f["termino_id"]):
            decir(f"  - {f['marca_id']} {f['tipo']} {f['termino']} (id={f['termino_id']}; rechazar)")
    if not args.aplicar:
        decir("[dry-run] no se escribió nada (usar --aplicar)")
        return
    actor = quien()
    insertados = 0
    if nuevos["semilla"]:
        insertados += repo.sembrar_terminos(conn, cid, nuevos["semilla"], creado_por=actor)
    if nuevos["catalogo"]:
        insertados += _sembrar_catalogo(conn, cid, nuevos["catalogo"], actor)
    rechazados = sum(_rechazar_obsoleto(conn, cid, f, actor) for f in retiros)
    decir(f"{cid}: {insertados} términos insertados en estado 'semilla'; {rechazados} rechazados")
