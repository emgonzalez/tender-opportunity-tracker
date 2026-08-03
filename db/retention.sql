-- ============================================================================
-- db/retention.sql — Política de retención de licitaciones_chunks
-- ============================================================================
-- Fuente de verdad: docs/arquitectura/modelo-de-datos.md
--   Sección 2, subsección "Políticas de retención".
-- Tarea del plan: docs/planes/fase2-track-b.md — B7.1 "Job de retención".
--
-- Regla de negocio:
--   - PURGAR licitaciones_chunks de procesos cuya fecha_cierre tenga más
--     de 12 meses (memoria de trabajo, no institucional).
--   - CONSERVAR SIEMPRE licitaciones_metadata y licitaciones_analisis
--     (memoria institucional permanente: histórico de qué se vio, cuándo,
--     y qué se concluyó, incluso sin el texto/embeddings del pliego).
--   - Procesos vigentes (fecha_cierre NULL o dentro de los últimos 12
--     meses) NUNCA deben verse afectados por este script.
-- ============================================================================


-- ─────────────────────────────────────────────────────────────
-- 0) DRY-RUN — conteo previo (revisar ANTES de borrar, descomentar)
-- ─────────────────────────────────────────────────────────────
-- Ejecutar esta consulta primero y confirmar que el número de filas a
-- purgar tiene sentido (coincide con procesos realmente cerrados hace
-- más de 12 meses) antes de correr el DELETE de la sección 1.
--
-- SELECT COUNT(*) AS chunks_a_purgar
-- FROM   licitaciones_chunks c
-- WHERE  c.proceso_id IN (
--          SELECT m.proceso_id
--          FROM   licitaciones_metadata m
--          WHERE  m.fecha_cierre IS NOT NULL
--          AND    m.fecha_cierre < ADD_MONTHS(SYSDATE, -12)
--        );


-- ─────────────────────────────────────────────────────────────
-- 1) Purga idempotente de licitaciones_chunks (procesos cerrados > 12 meses)
-- ─────────────────────────────────────────────────────────────
-- Idempotente: si ya no quedan chunks de esos procesos (porque un run
-- anterior ya los purgó), el DELETE simplemente afecta 0 filas — no falla
-- ni duplica efectos.
-- Acotado: solo toca chunks cuyo proceso_id referencia una fecha_cierre
-- ya vencida (> 12 meses); fecha_cierre NULL o dentro de los últimos 12
-- meses queda explícitamente excluido, por lo que los procesos vigentes
-- nunca son alcanzados.
--
-- Nota: licitaciones_chunks tiene ON DELETE CASCADE hacia
-- licitaciones_metadata (ver db/schema.sql), pero aquí se borra
-- directamente de licitaciones_chunks — NO se borra la fila de
-- licitaciones_metadata ni de licitaciones_analisis, que se conservan
-- como memoria institucional permanente.
DELETE FROM licitaciones_chunks c
WHERE  c.proceso_id IN (
         SELECT m.proceso_id
         FROM   licitaciones_metadata m
         WHERE  m.fecha_cierre IS NOT NULL
         AND    m.fecha_cierre < ADD_MONTHS(SYSDATE, -12)
       );

COMMIT;


-- ─────────────────────────────────────────────────────────────
-- 2) Verificación post-purga (opcional)
-- ─────────────────────────────────────────────────────────────
-- Confirmar que ya no quedan chunks huérfanos de procesos vencidos, y que
-- metadata/analisis de esos mismos procesos siguen intactos.
--
-- SELECT COUNT(*) AS chunks_restantes_de_procesos_vencidos
-- FROM   licitaciones_chunks c
-- WHERE  c.proceso_id IN (
--          SELECT m.proceso_id
--          FROM   licitaciones_metadata m
--          WHERE  m.fecha_cierre IS NOT NULL
--          AND    m.fecha_cierre < ADD_MONTHS(SYSDATE, -12)
--        );
-- -- Esperado: 0
--
-- SELECT COUNT(*) AS metadata_conservada
-- FROM   licitaciones_metadata m
-- WHERE  m.fecha_cierre IS NOT NULL
-- AND    m.fecha_cierre < ADD_MONTHS(SYSDATE, -12);
-- -- Esperado: > 0 si hay histórico (se conserva, no se toca)


-- ─────────────────────────────────────────────────────────────
-- 3) Programación semanal (opcional, vía DBMS_SCHEDULER)
-- ─────────────────────────────────────────────────────────────
-- Este DELETE puede ejecutarse desde el pipeline de AMALIA (llamado desde
-- src/memoria/service.py) o programarse directamente en la BD con
-- DBMS_SCHEDULER para correr semanalmente sin depender del pipeline:
--
-- BEGIN
--   DBMS_SCHEDULER.CREATE_JOB(
--     job_name        => 'AMALIA_RETENTION_CHUNKS',
--     job_type        => 'PLSQL_BLOCK',
--     job_action      => q'[
--       BEGIN
--         DELETE FROM licitaciones_chunks c
--         WHERE  c.proceso_id IN (
--                  SELECT m.proceso_id
--                  FROM   licitaciones_metadata m
--                  WHERE  m.fecha_cierre IS NOT NULL
--                  AND    m.fecha_cierre < ADD_MONTHS(SYSDATE, -12)
--                );
--         COMMIT;
--       END;
--     ]',
--     repeat_interval => 'FREQ=WEEKLY; BYDAY=SUN; BYHOUR=3',
--     enabled         => TRUE,
--     comments        => 'Purga semanal de licitaciones_chunks > 12 meses (memoria de trabajo, no institucional)'
--   );
-- END;
-- /

-- ============================================================================
-- Fin de db/retention.sql
-- ============================================================================
