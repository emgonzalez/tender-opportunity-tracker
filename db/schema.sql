-- ============================================================================
-- db/schema.sql — Esquema AMALIA (Oracle Autonomous Database 23ai)
-- ============================================================================
-- Transcripción literal del DDL desde:
--   docs/arquitectura/modelo-de-datos.md
--   Sección 2: Diagrama Entidad-Relación (ERD) / Modelo de Datos
--
-- Este script es IDEMPOTENTE: cada CREATE TABLE e INDEX está envuelto en
-- un bloque PL/SQL anónimo que captura y suprime ORA-00955 (ya existe).
--
-- Quoting: usa q'[...]' para evitar duplicar comillas simples en
-- los CHECK constraints.
--
-- Nota: NO incluye CREATE VECTOR INDEX ivf_chunks (tarea aparte: index_ivf.sql)
--
-- Ejecución: SQLcl, SQL Developer Web, o Python oracledb.
-- ============================================================================

-- ─────────────────────────────────────────────────────────────
-- 1) Metadatos + estado del pipeline (tabla-cola durable)
-- ─────────────────────────────────────────────────────────────
BEGIN
  EXECUTE IMMEDIATE q'[
    CREATE TABLE licitaciones_metadata (
      proceso_id      VARCHAR2(64)  CONSTRAINT pk_licit PRIMARY KEY,
      cliente_id      VARCHAR2(64)  NOT NULL,
      institucion     VARCHAR2(400),
      titulo          VARCHAR2(1000),
      categoria       VARCHAR2(120),
      modalidad       VARCHAR2(120),
      monto_estimado  NUMBER(14,2),
      fecha_pub       DATE,
      fecha_cierre    DATE,
      url_comprasal   VARCHAR2(1000),
      objeto_uri      VARCHAR2(1000),
      formato         VARCHAR2(16),
      triaje          VARCHAR2(8),
      triaje_conf     NUMBER(3,2),
      estado          VARCHAR2(32)  DEFAULT 'DESCUBIERTO' NOT NULL,
      intentos        NUMBER(3)     DEFAULT 0 NOT NULL,
      ultimo_error    VARCHAR2(1000),
      worker_id       VARCHAR2(64),
      creado_en       TIMESTAMP     DEFAULT SYSTIMESTAMP NOT NULL,
      actualizado_en  TIMESTAMP     DEFAULT SYSTIMESTAMP NOT NULL,
      CONSTRAINT ck_estado CHECK (estado IN (
        'DESCUBIERTO','DESCARGADO','FORMATO_NATIVO','FORMATO_ESCANEADO',
        'DESCARTADO_REGEX','DESCARTADO_TRIAJE','DESCARTADO_TAMANO',
        'APROBADO','VECTORIZADO','ANALIZADO','NOTIFICADO',
        'REQUIERE_REVISION_MANUAL','ERROR_DESCARGA','ERROR_PARSEO',
        'ERROR_LLM','ERROR_BD')),
      CONSTRAINT ck_triaje CHECK (triaje IN ('GO','NO-GO'))
    )
  ]';
EXCEPTION WHEN OTHERS THEN
  IF SQLCODE != -955 THEN RAISE; END IF;
END;
/

BEGIN
  EXECUTE IMMEDIATE q'[
    CREATE INDEX ix_licit_estado ON licitaciones_metadata(estado, actualizado_en)
  ]';
EXCEPTION WHEN OTHERS THEN
  IF SQLCODE != -955 AND SQLCODE != -1408 THEN RAISE; END IF;
END;
/

BEGIN
  EXECUTE IMMEDIATE q'[
    CREATE INDEX ix_licit_cliente ON licitaciones_metadata(cliente_id, fecha_pub)
  ]';
EXCEPTION WHEN OTHERS THEN
  IF SQLCODE != -955 AND SQLCODE != -1408 THEN RAISE; END IF;
END;
/

-- ─────────────────────────────────────────────────────────────
-- 2) Chunks + vectores (Track B: memoria institucional)
-- ─────────────────────────────────────────────────────────────
BEGIN
  EXECUTE IMMEDIATE q'[
    CREATE TABLE licitaciones_chunks (
      chunk_id     VARCHAR2(80) CONSTRAINT pk_chunk PRIMARY KEY,
      proceso_id   VARCHAR2(64) NOT NULL,
      seq          NUMBER(6)    NOT NULL,
      pagina       NUMBER(5),
      chunk_text   CLOB         NOT NULL,
      embedding    VECTOR(384, FLOAT32),
      creado_en    TIMESTAMP    DEFAULT SYSTIMESTAMP NOT NULL,
      CONSTRAINT fk_chunk_proc FOREIGN KEY (proceso_id)
        REFERENCES licitaciones_metadata(proceso_id) ON DELETE CASCADE,
      CONSTRAINT uq_chunk UNIQUE (proceso_id, seq)
    )
  ]';
EXCEPTION WHEN OTHERS THEN
  IF SQLCODE != -955 THEN RAISE; END IF;
END;
/

-- ─────────────────────────────────────────────────────────────
-- 3) Presupuesto de tokens (guardia de cuota)
-- ─────────────────────────────────────────────────────────────
BEGIN
  EXECUTE IMMEDIATE q'[
    CREATE TABLE presupuesto_diario (
      dia               DATE CONSTRAINT pk_presup PRIMARY KEY,
      tokens_consumidos NUMBER(12) DEFAULT 0 NOT NULL,
      limite_tokens     NUMBER(12) NOT NULL
    )
  ]';
EXCEPTION WHEN OTHERS THEN
  IF SQLCODE != -955 THEN RAISE; END IF;
END;
/

-- ─────────────────────────────────────────────────────────────
-- 4) Resultado del análisis (matriz de riesgos + anexos)
-- ─────────────────────────────────────────────────────────────
BEGIN
  EXECUTE IMMEDIATE q'[
    CREATE TABLE licitaciones_analisis (
      proceso_id     VARCHAR2(64) CONSTRAINT pk_analisis PRIMARY KEY,
      matriz_riesgos JSON,
      anexos         JSON,
      modelo_llm     VARCHAR2(64),
      generado_en    TIMESTAMP DEFAULT SYSTIMESTAMP NOT NULL,
      CONSTRAINT fk_analisis_proc FOREIGN KEY (proceso_id)
        REFERENCES licitaciones_metadata(proceso_id) ON DELETE CASCADE
    )
  ]';
EXCEPTION WHEN OTHERS THEN
  IF SQLCODE != -955 THEN RAISE; END IF;
END;
/

-- ─────────────────────────────────────────────────────────────
-- 5) Feedback de relevancia (botones Relevante/Irrelevante del correo)
-- ─────────────────────────────────────────────────────────────
-- `licitaciones_reportadas` es la foto de cada proceso EN EL MOMENTO de
-- enviarlo, por (proceso, cliente). No cuelga de `licitaciones_metadata`:
-- esa tabla es por proceso (PK global) y solo se llena con ENABLE_MEMORIA.
BEGIN
  EXECUTE IMMEDIATE q'[
    CREATE TABLE licitaciones_reportadas (
      proceso_id       VARCHAR2(64)  NOT NULL,
      cliente_id       VARCHAR2(64)  NOT NULL,
      institucion      VARCHAR2(400),
      titulo           VARCHAR2(1000),
      categoria        VARCHAR2(120),
      match_tecnico    NUMBER(4,3),
      match_categoria  NUMBER(4,3),
      factibilidad     NUMBER(4,3),
      tags             JSON,
      url_comprasal    VARCHAR2(1000),
      primer_envio_en  TIMESTAMP DEFAULT SYSTIMESTAMP NOT NULL,
      ultimo_envio_en  TIMESTAMP DEFAULT SYSTIMESTAMP NOT NULL,
      veces_enviada    NUMBER(4)     DEFAULT 1 NOT NULL,
      CONSTRAINT pk_reportadas PRIMARY KEY (proceso_id, cliente_id)
    )
  ]';
EXCEPTION WHEN OTHERS THEN
  IF SQLCODE != -955 THEN RAISE; END IF;
END;
/

-- Un voto por (proceso, cliente, destinatario): cambiar de opinión sobrescribe,
-- y los votos de personas distintas conviven (el desacuerdo es dato).
-- Sin FK a `licitaciones_reportadas` a propósito: si la foto no se pudo escribir
-- al enviar, el voto de quien hizo clic no se debe perder por eso.
BEGIN
  EXECUTE IMMEDIATE q'[
    CREATE TABLE licitaciones_feedback (
      proceso_id         VARCHAR2(64)  NOT NULL,
      cliente_id         VARCHAR2(64)  NOT NULL,
      destinatario_id    VARCHAR2(16)  NOT NULL,
      destinatario_email VARCHAR2(320),
      voto               VARCHAR2(12)  NOT NULL
                         CONSTRAINT ck_feedback_voto CHECK (voto IN ('RELEVANTE','IRRELEVANTE')),
      motivos            JSON,
      comentario         VARCHAR2(4000),
      creado_en          TIMESTAMP DEFAULT SYSTIMESTAMP NOT NULL,
      actualizado_en     TIMESTAMP DEFAULT SYSTIMESTAMP NOT NULL,
      CONSTRAINT pk_feedback PRIMARY KEY (proceso_id, cliente_id, destinatario_id)
    )
  ]';
EXCEPTION WHEN OTHERS THEN
  IF SQLCODE != -955 THEN RAISE; END IF;
END;
/

BEGIN
  EXECUTE IMMEDIATE 'CREATE INDEX ix_feedback_cliente_voto ON licitaciones_feedback (cliente_id, voto)';
EXCEPTION WHEN OTHERS THEN
  IF SQLCODE != -955 AND SQLCODE != -1408 THEN RAISE; END IF;
END;
/

-- ─────────────────────────────────────────────────────────────
-- 6) Datos de mercado: adjudicaciones (terceros incluidos) y resultado
--    propio, para entrenar a AMALIA (plan del portal, §3.5)
-- ─────────────────────────────────────────────────────────────
-- `adjudicacion_item`: una fila = un renglón/lote de una adjudicación
-- publicada por COMPRASAL (un mismo codigo_proceso puede tener muchas filas,
-- cada una con su propio proveedor y monto — verificado). Fuente: el
-- registro público `GET /api/v1/publico/obtener/procesos/publicos`, que NO
-- requiere sesión. Aditiva sobre el esquema actual: NO depende de
-- proceso_gid/cliente_proceso (esquema v2, todavía sin arrancar).
BEGIN
  EXECUTE IMMEDIATE q'[
    CREATE TABLE adjudicacion_item (
      id                         NUMBER        CONSTRAINT pk_adjudicacion_item PRIMARY KEY,
      codigo_proceso             VARCHAR2(64)  NOT NULL,
      nombre_proceso             VARCHAR2(1000),
      id_institucion             NUMBER,
      institucion                VARCHAR2(400),
      fecha_adjudicacion         DATE,
      monto                      NUMBER(14,2),
      id_proveedor               NUMBER,
      proveedor_nombre           VARCHAR2(400),
      proveedor_nombre_comercial VARCHAR2(400),
      es_multicomputos           NUMBER(1)     DEFAULT 0 NOT NULL,
      datos_crudos               JSON,
      cargado_en                 TIMESTAMP     DEFAULT SYSTIMESTAMP NOT NULL,
      actualizado_en             TIMESTAMP     DEFAULT SYSTIMESTAMP NOT NULL,
      CONSTRAINT ck_adj_es_mc CHECK (es_multicomputos IN (0,1))
    )
  ]';
EXCEPTION WHEN OTHERS THEN
  IF SQLCODE != -955 THEN RAISE; END IF;
END;
/

BEGIN
  EXECUTE IMMEDIATE 'CREATE INDEX ix_adj_codigo ON adjudicacion_item(codigo_proceso)';
EXCEPTION WHEN OTHERS THEN
  IF SQLCODE != -955 AND SQLCODE != -1408 THEN RAISE; END IF;
END;
/

BEGIN
  EXECUTE IMMEDIATE 'CREATE INDEX ix_adj_institucion ON adjudicacion_item(id_institucion)';
EXCEPTION WHEN OTHERS THEN
  IF SQLCODE != -955 AND SQLCODE != -1408 THEN RAISE; END IF;
END;
/

BEGIN
  EXECUTE IMMEDIATE 'CREATE INDEX ix_adj_es_mc ON adjudicacion_item(es_multicomputos)';
EXCEPTION WHEN OTHERS THEN
  IF SQLCODE != -955 AND SQLCODE != -1408 THEN RAISE; END IF;
END;
/

-- `proceso_participado`: procesos en los que el cliente efectivamente
-- presentó oferta (API autenticada `participacion=true`), con el resultado
-- resuelto cruzando contra `adjudicacion_item` por `codigo_proceso`.
BEGIN
  EXECUTE IMMEDIATE q'[
    CREATE TABLE proceso_participado (
      cliente_id                  VARCHAR2(64)  NOT NULL,
      codigo_proceso               VARCHAR2(64)  NOT NULL,
      nombre_proceso               VARCHAR2(1000),
      institucion                  VARCHAR2(400),
      forma_contratacion           VARCHAR2(120),
      fecha_publicacion            DATE,
      resultado                    VARCHAR2(16)  DEFAULT 'PENDIENTE' NOT NULL,
      monto_adjudicado_a_nosotros  NUMBER(14,2),
      proveedor_ganador            VARCHAR2(400),
      monto_ganador                NUMBER(14,2),
      resuelto_en                  TIMESTAMP,
      cargado_en                   TIMESTAMP     DEFAULT SYSTIMESTAMP NOT NULL,
      actualizado_en               TIMESTAMP     DEFAULT SYSTIMESTAMP NOT NULL,
      CONSTRAINT pk_proceso_participado PRIMARY KEY (cliente_id, codigo_proceso),
      CONSTRAINT ck_participado_resultado CHECK (resultado IN
        ('PENDIENTE','GANADO','PERDIDO','DESIERTO'))
    )
  ]';
EXCEPTION WHEN OTHERS THEN
  IF SQLCODE != -955 THEN RAISE; END IF;
END;
/

BEGIN
  EXECUTE IMMEDIATE 'CREATE INDEX ix_participado_resultado ON proceso_participado(resultado)';
EXCEPTION WHEN OTHERS THEN
  IF SQLCODE != -955 AND SQLCODE != -1408 THEN RAISE; END IF;
END;
/

-- ============================================================================
-- Fin del DDL idempotente.
-- Nota: El índice vectorial (CREATE VECTOR INDEX ivf_chunks ...) está
-- en index_ivf.sql (tarea aparte, opt-in para Track B).
-- ============================================================================
