"""src.oportunidades.service — cambiar el estado comercial de una oportunidad (Entrega 5).

UNA transacción: estado + evento + auditoría. Sin commit acá: si algo falla, quien llama hace rollback
y no queda un estado sin su evento. Devuelve un resultado y no levanta por los casos de negocio, para
que la ruta responda sin distinguir lo que no debe (T6: lo ajeno es un 404 como cualquier otro)."""
from __future__ import annotations

from decimal import Decimal
from typing import Any

from src.identidad import repo as identidad
from src.oportunidades import politica, repo

OK, CONFLICTO, INVALIDO, NO_ENCONTRADO = "ok", "conflicto", "invalido", "no_encontrado"
SIMULACION = "SIMULACION"  # «ver como» (E10): la vista simulada es de solo lectura


def cambiar_estado(conn: Any, alcance: Any, cliente_id: str, proceso_gid: str, nuevo: str, token: str,
                   motivo: str | None = None, monto: Decimal | None = None,
                   competidor: str | None = None) -> str:
    # Defensa en profundidad: la ruta ya lo rechaza, pero bajo «ver como» no se toca la base aunque
    # alguien llame al servicio por otro camino.
    if getattr(alcance, "simulado", None):
        return SIMULACION
    # Quién puede cambiar (P4) y a qué procesos (IDOR): lo ajeno es NO_ENCONTRADO.
    if not alcance.puede_cambiar_estado(cliente_id) or not repo.proceso_visible(conn, cliente_id, proceso_gid):
        return NO_ENCONTRADO

    actual = repo.obtener(conn, cliente_id, proceso_gid)
    anterior = actual["estado"] if actual else politica.ESTADO_INICIAL
    if (actual["token"] if actual else "") != (token or ""):
        return CONFLICTO  # la fila cambió desde que la persona abrió el formulario

    motivo = (motivo or "").strip()[:500] or None
    competidor = (competidor or "").strip()[:300] or None
    try:
        politica.validar(anterior, nuevo, motivo)
    except politica.TransicionInvalida:
        return INVALIDO

    if actual:
        escrito = repo.actualizar_estado(conn, cliente_id, proceso_gid, token, nuevo, alcance.usuario_id,
                                         motivo, monto, competidor)
    else:
        escrito = repo.crear_estado(conn, cliente_id, proceso_gid, nuevo, alcance.usuario_id,
                                    motivo, monto, competidor)
    if not escrito:
        return CONFLICTO
    repo.insertar_evento(conn, cliente_id, proceso_gid, anterior, nuevo, alcance.usuario_id, motivo)
    identidad.registrar_auditoria(
        conn, alcance.org_id, "ESTADO_CAMBIADO", usuario_id=alcance.usuario_id,
        recurso=f"{proceso_gid}:{anterior}>{nuevo}", alcance={"cliente_id": cliente_id})
    return OK
