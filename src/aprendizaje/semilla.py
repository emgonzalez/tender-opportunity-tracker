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

from typing import Iterable, Optional

SEMILLA: dict[str, dict[str, tuple[str, ...]]] = {
    "oracle": {
        "fuerte": (
            "Oracle", "OCI", "Oracle Cloud", "Exadata", "ZDLRA", "WebLogic", "Cloud@Customer", "NetSuite",
            "MySQL", "Java SE", "Solaris", "SPARC", "PeopleSoft", "JD Edwards", "Hyperion", "Siebel",
            "Primavera", "derechos de actualización por procesador",
        ),
        "debil": ("base de datos", "licenciamiento", "nube", "soporte técnico"),
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
