"""src.oportunidades.service — escrituras del pipeline comercial: cambiar el estado (Entrega 5), asignar
responsable y recuperar un descartado de AMALIA (E11).

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
RESPONSABLE_INVALIDO = "responsable_invalido"  # E11: no está en `usuarios_asignables` de ese cliente
NO_RECUPERABLE = "no_recuperable"  # E11: AMALIA no lo descartó (es relevante): no hay nada que recuperar
YA_RECUPERADO = "ya_recuperado"    # E11: ya tiene fila en `oportunidad_estado` (recuperado o trabajado)


def _marcas(alcance: Any) -> frozenset[str]:
    """E14: la restricción por marca viaja en el alcance (también el simulado). Un gid de otra marca del mismo
    cliente se trata EXACTAMENTE como inexistente (NO_ENCONTRADO), sin distinguirlo."""
    return frozenset(getattr(alcance, "solo_marcas", None) or ())


def cambiar_estado(conn: Any, alcance: Any, cliente_id: str, proceso_gid: str, nuevo: str, token: str,
                   motivo: str | None = None, monto: Decimal | None = None,
                   competidor: str | None = None) -> str:
    # Defensa en profundidad: la ruta ya lo rechaza, pero bajo «ver como» no se toca la base aunque
    # alguien llame al servicio por otro camino.
    if getattr(alcance, "simulado", None):
        return SIMULACION
    # Quién puede cambiar (P4) y a qué procesos (IDOR, también por marca): lo ajeno es NO_ENCONTRADO.
    bloqueo = _puede_escribir(conn, alcance, cliente_id, proceso_gid)
    if bloqueo:
        return bloqueo

    actual = repo.obtener(conn, cliente_id, proceso_gid, solo_marcas=_marcas(alcance))
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


def _puede_escribir(conn: Any, alcance: Any, cliente_id: str, proceso_gid: str) -> str | None:
    """Las dos primeras barreras de toda escritura, en orden: simulación y luego permiso + IDOR (por cliente y,
    con `alcance.solo_marcas`, por marca)."""
    if getattr(alcance, "simulado", None):
        return SIMULACION
    if not alcance.puede_cambiar_estado(cliente_id) or not repo.proceso_visible(
            conn, cliente_id, proceso_gid, solo_marcas=_marcas(alcance)):
        return NO_ENCONTRADO
    return None


def asignar_responsable(conn: Any, alcance: Any, cliente_id: str, proceso_gid: str, responsable_id: str | None,
                        token: str | None, ip_hash: str | None = None) -> str:
    """Asigna (o con ``responsable_id`` None/vacío, quita) el responsable de una oportunidad (E11).

    El responsable debe poder cambiar el estado en ese cliente (`identidad.repo.usuarios_asignables`).
    Sin fila en `oportunidad_estado` se crea NUEVO (origen MANUAL) con ese responsable. Deja un evento
    con el mismo estado antes y después y la auditoría `RESPONSABLE_ASIGNADO`, en la misma transacción."""
    bloqueo = _puede_escribir(conn, alcance, cliente_id, proceso_gid)
    if bloqueo:
        return bloqueo

    responsable_id = (responsable_id or "").strip() or None
    nombre = None
    if responsable_id is not None:
        asignables = identidad.usuarios_asignables(conn, alcance.org_id, cliente_id)
        nombre = next((u["nombre"] for u in asignables if u["usuario_id"] == responsable_id), None)
        if nombre is None:
            return RESPONSABLE_INVALIDO

    marcas = _marcas(alcance)
    actual = repo.obtener(conn, cliente_id, proceso_gid, solo_marcas=marcas)
    if actual is None and repo.es_descartado_por_amalia(conn, cliente_id, proceso_gid, solo_marcas=marcas):
        return NO_RECUPERABLE  # un descartado se rescata con `recuperar_descartado` (con su auditoría)
    if (actual["token"] if actual else "") != (token or ""):
        return CONFLICTO
    if (actual.get("responsable") if actual else None) == responsable_id:
        return OK  # nada cambia (incluye quitar donde no había nadie): sin evento ni auditoría

    estado = actual["estado"] if actual else politica.ESTADO_INICIAL
    if actual:
        escrito = repo.fijar_responsable(conn, cliente_id, proceso_gid, responsable_id, token or "")
    else:
        escrito = repo.crear_estado(conn, cliente_id, proceso_gid, estado, responsable_id, None, None, None)
    if not escrito:
        return CONFLICTO
    nota = f"Asignada a {nombre}" if responsable_id else "Responsable quitado"
    repo.insertar_evento(conn, cliente_id, proceso_gid, estado, estado, alcance.usuario_id, nota)
    identidad.registrar_auditoria(
        conn, alcance.org_id, "RESPONSABLE_ASIGNADO", usuario_id=alcance.usuario_id,
        recurso=f"cliente={cliente_id};proceso={proceso_gid};responsable={responsable_id or ''}",
        alcance={"cliente_id": cliente_id}, ip_hash=ip_hash)
    return OK


def recuperar_descartado(conn: Any, alcance: Any, cliente_id: str, proceso_gid: str, token: str | None = None,
                         ip_hash: str | None = None) -> str:
    """Rescata un proceso que AMALIA descartó: crea la oportunidad NUEVO (origen MANUAL, sin responsable) (E11).

    NO toca `cliente_proceso`: la clasificación original se conserva para medir al clasificador, y el
    proceso entra a la lista por tener fila en `oportunidad_estado`."""
    bloqueo = _puede_escribir(conn, alcance, cliente_id, proceso_gid)
    if bloqueo:
        return bloqueo

    marcas = _marcas(alcance)
    descartado = repo.es_descartado_por_amalia(conn, cliente_id, proceso_gid, solo_marcas=marcas)
    if descartado is None:
        return NO_ENCONTRADO
    if not descartado:
        return YA_RECUPERADO if repo.obtener(conn, cliente_id, proceso_gid, solo_marcas=marcas) else NO_RECUPERABLE
    if token:
        return CONFLICTO  # el formulario vio una fila que ya no existe

    nuevo = politica.ESTADO_INICIAL
    if not repo.crear_estado(conn, cliente_id, proceso_gid, nuevo, None, None, None, None):
        return CONFLICTO  # otra persona lo recuperó en paralelo
    repo.insertar_evento(conn, cliente_id, proceso_gid, None, nuevo, alcance.usuario_id,
                         "Recuperada de descartados de AMALIA")
    identidad.registrar_auditoria(
        conn, alcance.org_id, "DESCARTADO_RECUPERADO", usuario_id=alcance.usuario_id,
        recurso=f"cliente={cliente_id};proceso={proceso_gid}", alcance={"cliente_id": cliente_id}, ip_hash=ip_hash)
    return OK
