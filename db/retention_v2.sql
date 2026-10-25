-- ============================================================================
-- db/retention_v2.sql — Política de retención de `chunk` (esquema v2)
-- ============================================================================
-- Espejo de db/retention.sql para el esquema v2 (db/schema_v2.sql) — Fase
-- "switch" de la migración (plan del portal AMALIA §3.7 punto 4). Archivo
-- aparte, no una edición in-place de retention.sql: mismo criterio que
-- schema_v2.sql vs schema.sql (coexistencia hasta la fase "Contract"), y
-- mezclar un DELETE real v1/v2 en un solo archivo con comentarios "correr
-- esta sección, no esa" aumenta el radio de un error de copy-paste en un
-- script que borra datos — no vale el ahorro de un archivo.
--
-- Regla de negocio (idéntica a v1, MÁS SIMPLE de aplicar en v2):
--   - PURGAR `chunk` de procesos cuya fecha_limite_utc tenga más de 12
--     meses (memoria de trabajo, no institucional).
--   - CONSERVAR SIEMPRE `proceso`, `cliente_proceso` y `analisis` (memoria
--     institucional permanente).
--   - Procesos vigentes (fecha_limite_utc NULL o dentro de los últimos 12
--     meses) NUNCA deben verse afectados por este script.
--
-- Por qué es más simple que v1: `chunk` no tiene `cliente_id` (el pliego es
-- del proceso/portal, no de un cliente — ver db/schema_v2.sql, sección
-- `chunk`), así que la política de retención no depende en absoluto de qué
-- cliente lo vio. No hace falta ningún JOIN a `cliente_proceso` para decidir
-- qué purgar, solo a `proceso`.
--
-- Ninguno de los dos scripts (v1 ni v2) corre automatizado en producción
-- hoy — son manuales/opt-in, ejercidos solo por tests de integración
-- (ver tests/integration/test_oracle_local.py).
-- ============================================================================


-- ─────────────────────────────────────────────────────────────
-- 0) DRY-RUN — conteo previo (revisar ANTES de borrar, descomentar)
-- ─────────────────────────────────────────────────────────────
-- SELECT COUNT(*) AS chunks_a_purgar
-- FROM   chunk c
-- WHERE  c.proceso_gid IN (
--          SELECT p.proceso_gid
--          FROM   proceso p
--          WHERE  p.fecha_limite_utc IS NOT NULL
--          AND    p.fecha_limite_utc < ADD_MONTHS(SYSDATE, -12)
--        );


-- ─────────────────────────────────────────────────────────────
-- 1) Purga idempotente de chunk (procesos cerrados > 12 meses)
-- ─────────────────────────────────────────────────────────────
-- Idempotente: si ya no quedan chunks de esos procesos (porque un run
-- anterior ya los purgó), el DELETE simplemente afecta 0 filas.
-- Acotado: solo toca chunks cuyo proceso_gid referencia una
-- fecha_limite_utc ya vencida; fecha_limite_utc NULL o dentro de los
-- últimos 12 meses queda explícitamente excluido.
--
-- Nota: `chunk` tiene ON DELETE CASCADE hacia `proceso` (fk_chunk_proceso,
-- ver db/schema_v2.sql), pero aquí se borra directamente de `chunk` — NO se
-- borra la fila de `proceso`, `cliente_proceso` ni `analisis`, que se
-- conservan como memoria institucional permanente.
DELETE FROM chunk c
WHERE  c.proceso_gid IN (
         SELECT p.proceso_gid
         FROM   proceso p
         WHERE  p.fecha_limite_utc IS NOT NULL
         AND    p.fecha_limite_utc < ADD_MONTHS(SYSDATE, -12)
       );

COMMIT;


-- ─────────────────────────────────────────────────────────────
-- 1b) Identidad: sesiones y códigos de acceso vencidos hace más de 30 días
-- ─────────────────────────────────────────────────────────────
-- Espejo SQL de `python -m src.identidad purgar` (P5, SESION_PURGA_DIAS=30).
-- `auditoria` NO se purga aquí: es append-only.
DELETE FROM sesion
WHERE  expira_en < SYSTIMESTAMP - NUMTODSINTERVAL(30, 'DAY');

DELETE FROM codigo_acceso
WHERE  expira_en < SYSTIMESTAMP - NUMTODSINTERVAL(30, 'DAY');

COMMIT;


-- ─────────────────────────────────────────────────────────────
-- 2) Verificación post-purga (opcional)
-- ─────────────────────────────────────────────────────────────
-- SELECT COUNT(*) AS chunks_restantes_de_procesos_vencidos
-- FROM   chunk c
-- WHERE  c.proceso_gid IN (
--          SELECT p.proceso_gid
--          FROM   proceso p
--          WHERE  p.fecha_limite_utc IS NOT NULL
--          AND    p.fecha_limite_utc < ADD_MONTHS(SYSDATE, -12)
--        );
-- -- Esperado: 0
--
-- SELECT COUNT(*) AS proceso_conservado
-- FROM   proceso p
-- WHERE  p.fecha_limite_utc IS NOT NULL
-- AND    p.fecha_limite_utc < ADD_MONTHS(SYSDATE, -12);
-- -- Esperado: > 0 si hay histórico (se conserva, no se toca)


-- ─────────────────────────────────────────────────────────────
-- 3) Programación semanal (opcional, vía DBMS_SCHEDULER)
-- ─────────────────────────────────────────────────────────────
-- BEGIN
--   DBMS_SCHEDULER.CREATE_JOB(
--     job_name        => 'AMALIA_RETENTION_CHUNK_V2',
--     job_type        => 'PLSQL_BLOCK',
--     job_action      => q'[
--       BEGIN
--         DELETE FROM chunk c
--         WHERE  c.proceso_gid IN (
--                  SELECT p.proceso_gid
--                  FROM   proceso p
--                  WHERE  p.fecha_limite_utc IS NOT NULL
--                  AND    p.fecha_limite_utc < ADD_MONTHS(SYSDATE, -12)
--                );
--         COMMIT;
--       END;
--     ]',
--     repeat_interval => 'FREQ=WEEKLY; BYDAY=SUN; BYHOUR=3',
--     enabled         => TRUE,
--     comments        => 'Purga semanal de chunk > 12 meses (esquema v2, memoria de trabajo)'
--   );
-- END;
-- /

-- ============================================================================
-- Fin de db/retention_v2.sql
-- ============================================================================
