"""Puerta conservadora de calibraciones; la transacción pertenece al ciclo.

Rollback operativo: repo.promover(conn, version_archivada, actor=...) restaura
la anterior con auditoría, dentro de otra transacción explícita del operador.
"""
from __future__ import annotations

import logging

from src.aprendizaje import evaluacion, repo
from src.notifier import alerts

logger = logging.getLogger(__name__)


def ejecutar(conn, cliente_id, aplicar=False, cfg=None):
    """Métricas con formato de evaluar(); predicciones críticas por ejemplo_id.

    El entrenador debe guardar parametros.predicciones_criticas como un mapa
    {str(ejemplo_id): etiqueta}. Se exige cubrir todos los críticos vigentes;
    campos ausentes o errores jamás autorizan promoción.
    """
    filas = repo._todos(
        conn, "SELECT version FROM marca_calibracion WHERE cliente_id = :cliente_id "
        "AND estado = 'candidata' ORDER BY version DESC", {"cliente_id": cliente_id},
    )
    resultados = []
    vistas = set()
    for (version,) in filas:
        candidata = repo.obtener_calibracion(conn, int(version))
        marca = candidata['marca_id']
        if marca in vistas:
            continue
        vistas.add(marca)
        activa = repo.calibracion_activa(conn, cliente_id, marca)
        criticos = repo.listar_ejemplos(conn, cliente_id, marca, split='critico', limite=100000)
        predicciones = (candidata.get('parametros') or {}).get('predicciones_criticas', {})
        correctos = [predicciones.get(str(e['ejemplo_id'])) == e['etiqueta'] for e in criticos]
        if len(criticos) >= 100000:
            correctos = []  # Sin cobertura completa jamás se promueve.
        puerta = evaluacion.puede_promover(
            candidata.get('metricas') or {}, (activa or {}).get('metricas') or {}, correctos,
        )
        resultado = {'version': int(version), **puerta, 'promovida': False, 'archivada': None}
        if puerta['puede_promover'] and aplicar:
            resultado['archivada'] = repo.promover(conn, int(version), actor='ciclo')
            resultado['promovida'] = True
        elif not puerta['puede_promover']:
            logger.warning('Calibración %s retenida: %s', version, '; '.join(puerta['motivos']))
            if aplicar and cfg is not None:
                alerts.notify(cfg, 'Aprendizaje: promoción bloqueada',
                              '; '.join(puerta['motivos']), client_id=cliente_id)
        resultados.append(resultado)
    return {'calibraciones': resultados}
