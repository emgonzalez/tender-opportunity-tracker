"""Texto por página del pliego; repositorio tolerante de la capa C2.

Las páginas son 1-based. La conexión y su transacción pertenecen al caller.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def guardar_paginas(conn: Any, proceso_gid: str, paginas: list[dict], *,
                     hash_pdf: str | None = None, modelo: str | None = None) -> int:
    """Actualiza por (proceso_gid, pagina), sin duplicar páginas al reintentar."""
    cursor = None
    try:
        lote = []
        for pagina in paginas:
            n = int(pagina.get("pagina", pagina.get("n", 0)))
            fuente = pagina.get("fuente", "vision_llm")
            calidad = pagina.get("calidad")
            if n < 1 or fuente not in {"capa_texto", "vision_llm", "tesseract"}:
                continue
            if calidad is not None and not 0 <= float(calidad) <= 1:
                continue
            lote.append({"gid": proceso_gid, "pagina": n, "texto": pagina.get("texto", ""),
                         "fuente": fuente, "modelo": pagina.get("modelo", modelo),
                         "calidad": calidad, "hash_pdf": pagina.get("hash_pdf", hash_pdf)})
        if not lote:
            return 0
        cursor = conn.cursor()
        cursor.executemany("""
            MERGE INTO pagina_pliego p
            USING (SELECT :gid proceso_gid, :pagina pagina FROM dual) s
            ON (p.proceso_gid = s.proceso_gid AND p.pagina = s.pagina)
            WHEN MATCHED THEN UPDATE SET p.texto = :texto, p.fuente = :fuente,
                p.modelo = :modelo, p.calidad = :calidad, p.hash_pdf = :hash_pdf,
                p.creado_en = SYSTIMESTAMP
            WHEN NOT MATCHED THEN INSERT
                (proceso_gid, pagina, texto, fuente, modelo, calidad, hash_pdf)
                VALUES (:gid, :pagina, :texto, :fuente, :modelo, :calidad, :hash_pdf)
        """, lote)
        return len(lote)
    except Exception as exc:  # noqa: BLE001 — una ADB caída nunca interrumpe el análisis
        logger.warning("No se pudieron guardar páginas (%s)", type(exc).__name__)
        return 0
    finally:
        _cerrar(cursor)


def _cerrar(cursor: Any) -> None:
    try:
        if cursor is not None:
            cursor.close()
    except Exception:  # noqa: BLE001
        logger.warning("No se pudo cerrar cursor de páginas")


def leer_paginas(conn: Any, proceso_gid: str, *, hash_pdf: str | None = None) -> list[dict]:
    """Lee las páginas en orden; hash_pdf permite descartar una versión antigua."""
    cursor = None
    try:
        cursor = conn.cursor()
        cursor.execute("""SELECT pagina, texto, fuente, modelo, calidad, hash_pdf
            FROM pagina_pliego WHERE proceso_gid = :gid
            AND (:hash_pdf IS NULL OR hash_pdf = :hash_pdf) ORDER BY pagina""",
                       {"gid": proceso_gid, "hash_pdf": hash_pdf})
        resultado = []
        for n, texto, fuente, modelo, calidad, huella in cursor.fetchall():
            resultado.append({"pagina": int(n), "texto": texto.read() if hasattr(texto, "read") else texto or "",
                              "fuente": fuente, "modelo": modelo, "calidad": calidad, "hash_pdf": huella})
        return resultado
    except Exception as exc:  # noqa: BLE001
        logger.warning("No se pudieron leer páginas (%s)", type(exc).__name__)
        return []
    finally:
        _cerrar(cursor)


def existe(conn: Any, hash_pdf: str) -> bool:
    """Indica si hay al menos una página guardada para la huella del PDF."""
    cursor = None
    try:
        if not hash_pdf:
            return False
        cursor = conn.cursor()
        cursor.execute("SELECT 1 FROM pagina_pliego WHERE hash_pdf = :h FETCH FIRST 1 ROWS ONLY",
                       {"h": hash_pdf})
        return cursor.fetchone() is not None
    except Exception as exc:  # noqa: BLE001
        logger.warning("No se pudo consultar huella de páginas (%s)", type(exc).__name__)
        return False
    finally:
        _cerrar(cursor)
