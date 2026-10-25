-- ============================================================================
-- db/schema_v2.sql — Esquema v2 multi-tenant y multi-país (Oracle ADB 23ai)
-- ============================================================================
-- Fase "Expandir" de la migración descrita en el plan del portal AMALIA
-- (§3 Modelo de datos multi-tenant y multi-país). Ver también
-- docs/arquitectura/modelo-de-datos.md.
--
-- Por qué este archivo existe APARTE de db/schema.sql: durante toda la
-- migración (expand → dual-write → backfill → switch → contract) ambos
-- esquemas coexisten. Nadie lee ni escribe estas tablas todavía — el
-- pipeline en producción sigue intacto sobre licitaciones_metadata,
-- licitaciones_analisis, licitaciones_chunks, licitaciones_reportadas y
-- licitaciones_feedback. Cuando la fase "Contraer" llegue, este contenido
-- se funde en db/schema.sql y este archivo se retira.
--
-- Qué resuelve: `licitaciones_metadata` tiene PK global por proceso y un
-- solo `cliente_id`, así que un segundo cliente que ve el mismo proceso le
-- pisa el cliente_id al primero (MERGE en repo.py); `licitaciones_analisis`
-- es una fila por proceso aunque el análisis depende del cliente que lo
-- pidió. El esquema v2 separa el HECHO del portal (`proceso`, sin
-- cliente_id) de la VISTA de cada cliente sobre ese hecho
-- (`cliente_proceso`, `analisis`, ambas con `cliente_id` en la clave).
--
-- Convenciones (las mismas que db/schema.sql): IDEMPOTENTE, cada CREATE
-- TABLE/INDEX envuelto en un bloque PL/SQL que traga ORA-00955 (ya existe)
-- y, en índices, también ORA-01408 (ya indexado). q'[...]' para no escapar
-- comillas en los CHECK. `GENERATED ALWAYS AS IDENTITY` solo donde no hay
-- clave natural. Nombres en español para evitar palabras reservadas de
-- Oracle en inglés (lección de `adjudicacion_item.datos_crudos`, que no se
-- llama `raw`: ORA-03050).
--
-- Ejecución: SQLcl, SQL Developer Web, o Python oracledb — igual que
-- db/schema.sql, y SIEMPRE validado primero contra db/docker-compose.oracle.yml
-- (ver tests/integration/test_oracle_local.py).
-- ============================================================================

-- ─────────────────────────────────────────────────────────────
-- 1) Catálogos (sin dependencias)
-- ─────────────────────────────────────────────────────────────
BEGIN
  EXECUTE IMMEDIATE q'[
    CREATE TABLE portal (
      portal_id       VARCHAR2(32)  CONSTRAINT pk_portal PRIMARY KEY,
      nombre          VARCHAR2(120) NOT NULL,
      pais            VARCHAR2(2)   NOT NULL,
      zona_horaria    VARCHAR2(64)  NOT NULL,
      moneda_default  VARCHAR2(3)   NOT NULL,
      idioma          VARCHAR2(8)   DEFAULT 'es' NOT NULL,
      base_url        VARCHAR2(255) NOT NULL,
      activo          NUMBER(1)     DEFAULT 1 NOT NULL,
      creado_en       TIMESTAMP     DEFAULT SYSTIMESTAMP NOT NULL,
      CONSTRAINT ck_portal_activo CHECK (activo IN (0,1))
    )
  ]';
EXCEPTION WHEN OTHERS THEN
  IF SQLCODE != -955 THEN RAISE; END IF;
END;
/

BEGIN
  EXECUTE IMMEDIATE q'[
    CREATE TABLE taxonomia_modalidad (
      portal_id       VARCHAR2(32)  NOT NULL,
      modalidad_raw   VARCHAR2(200) NOT NULL,
      modalidad_norm  VARCHAR2(32)  NOT NULL,
      CONSTRAINT pk_taxonomia_modalidad PRIMARY KEY (portal_id, modalidad_raw),
      CONSTRAINT fk_taxonomia_modalidad_portal FOREIGN KEY (portal_id)
        REFERENCES portal(portal_id),
      CONSTRAINT ck_taxonomia_modalidad_norm CHECK (modalidad_norm IN (
        'LICITACION_ABIERTA','COMPARACION_PRECIOS','SUBASTA_INVERSA',
        'CONTRATACION_DIRECTA','CONVENIO_MARCO','CONSULTORIA','OTRO'))
    )
  ]';
EXCEPTION WHEN OTHERS THEN
  IF SQLCODE != -955 THEN RAISE; END IF;
END;
/

BEGIN
  EXECUTE IMMEDIATE q'[
    CREATE TABLE fx_tasa (
      moneda      VARCHAR2(3)   NOT NULL,
      mes         DATE          NOT NULL,
      tasa_a_usd  NUMBER(14,6)  NOT NULL,
      fuente      VARCHAR2(120),
      CONSTRAINT pk_fx_tasa PRIMARY KEY (moneda, mes)
    )
  ]';
EXCEPTION WHEN OTHERS THEN
  IF SQLCODE != -955 THEN RAISE; END IF;
END;
/

-- ─────────────────────────────────────────────────────────────
-- 2) Tenant y clientes
-- ─────────────────────────────────────────────────────────────
BEGIN
  EXECUTE IMMEDIATE q'[
    CREATE TABLE organizacion (
      org_id          VARCHAR2(32)  CONSTRAINT pk_organizacion PRIMARY KEY,
      nombre          VARCHAR2(200) NOT NULL,
      pais_sede       VARCHAR2(2)   NOT NULL,
      zona_horaria    VARCHAR2(64)  NOT NULL,
      locale_default  VARCHAR2(8)   DEFAULT 'es-SV' NOT NULL,
      moneda_reporte  VARCHAR2(3)   DEFAULT 'USD' NOT NULL,
      estado          VARCHAR2(16)  DEFAULT 'ACTIVO' NOT NULL,
      creado_en       TIMESTAMP     DEFAULT SYSTIMESTAMP NOT NULL,
      CONSTRAINT ck_organizacion_estado CHECK (estado IN ('ACTIVO','SUSPENDIDO'))
    )
  ]';
EXCEPTION WHEN OTHERS THEN
  IF SQLCODE != -955 THEN RAISE; END IF;
END;
/

-- `cliente` v2: mismo `cliente_id` que ya usan hoy los YAML de clients/ y
-- las tablas actuales (licitaciones_metadata.cliente_id, etc.) — el
-- backfill (Fase 3 de la migración) no tiene que traducir ningún valor.
BEGIN
  EXECUTE IMMEDIATE q'[
    CREATE TABLE cliente (
      cliente_id      VARCHAR2(64)  CONSTRAINT pk_cliente_v2 PRIMARY KEY,
      org_id          VARCHAR2(32)  NOT NULL,
      nombre          VARCHAR2(200) NOT NULL,
      activo          NUMBER(1)     DEFAULT 1 NOT NULL,
      locale          VARCHAR2(8)   DEFAULT 'es-SV' NOT NULL,
      zona_horaria    VARCHAR2(64)  DEFAULT 'America/El_Salvador' NOT NULL,
      creado_en       TIMESTAMP     DEFAULT SYSTIMESTAMP NOT NULL,
      CONSTRAINT ck_cliente_v2_activo CHECK (activo IN (0,1)),
      CONSTRAINT fk_cliente_v2_org FOREIGN KEY (org_id) REFERENCES organizacion(org_id)
    )
  ]';
EXCEPTION WHEN OTHERS THEN
  IF SQLCODE != -955 THEN RAISE; END IF;
END;
/

-- `cliente_config`: versiona lo que hoy es el resto del YAML (catalog,
-- rules, notification, tag_taxonomy...) como JSON. `activa` marca cuál
-- versión lee el pipeline; nunca se sobrescribe una fila, se inserta una
-- versión nueva y se apaga la anterior (append-only, igual que `auditoria`).
BEGIN
  EXECUTE IMMEDIATE q'[
    CREATE TABLE cliente_config (
      cliente_id      VARCHAR2(64)  NOT NULL,
      version         NUMBER(6)     NOT NULL,
      contenido       JSON          NOT NULL,
      activa          NUMBER(1)     DEFAULT 1 NOT NULL,
      creado_por      VARCHAR2(64),
      creado_en       TIMESTAMP     DEFAULT SYSTIMESTAMP NOT NULL,
      CONSTRAINT pk_cliente_config PRIMARY KEY (cliente_id, version),
      CONSTRAINT fk_cliente_config_cliente FOREIGN KEY (cliente_id)
        REFERENCES cliente(cliente_id),
      CONSTRAINT ck_cliente_config_activa CHECK (activa IN (0,1))
    )
  ]';
EXCEPTION WHEN OTHERS THEN
  IF SQLCODE != -955 THEN RAISE; END IF;
END;
/

BEGIN
  EXECUTE IMMEDIATE q'[
    CREATE TABLE cliente_portal (
      cliente_id      VARCHAR2(64)  NOT NULL,
      portal_id       VARCHAR2(32)  NOT NULL,
      activo          NUMBER(1)     DEFAULT 1 NOT NULL,
      credencial_ref  VARCHAR2(120),
      lookback_dias   NUMBER(3)     DEFAULT 1 NOT NULL,
      horario_cron    VARCHAR2(32)  DEFAULT '0 7 * * *' NOT NULL,
      CONSTRAINT pk_cliente_portal PRIMARY KEY (cliente_id, portal_id),
      CONSTRAINT fk_cliente_portal_cliente FOREIGN KEY (cliente_id)
        REFERENCES cliente(cliente_id),
      CONSTRAINT fk_cliente_portal_portal FOREIGN KEY (portal_id)
        REFERENCES portal(portal_id),
      CONSTRAINT ck_cliente_portal_activo CHECK (activo IN (0,1))
    )
  ]';
EXCEPTION WHEN OTHERS THEN
  IF SQLCODE != -955 THEN RAISE; END IF;
END;
/

-- ─────────────────────────────────────────────────────────────
-- 3) El hecho del portal y la vista de cada cliente sobre él
-- ─────────────────────────────────────────────────────────────
-- `proceso`: el HECHO, sin cliente_id — esto es lo que cierra el bug de
-- licitaciones_metadata (PK global reasignada por el último cliente que
-- la toca). `proceso_gid` = portal_id || ':' || numero_referencia,
-- calculado por la aplicación al escribir (no columna virtual: mismo
-- patrón que `chunk_id` en licitaciones_chunks, que también lo arma el
-- código, no la base).
BEGIN
  EXECUTE IMMEDIATE q'[
    CREATE TABLE proceso (
      proceso_gid       VARCHAR2(96)  CONSTRAINT pk_proceso PRIMARY KEY,
      portal_id         VARCHAR2(32)  NOT NULL,
      numero_referencia VARCHAR2(64)  NOT NULL,
      institucion       VARCHAR2(400),
      institucion_norm  VARCHAR2(400),
      titulo            VARCHAR2(1000),
      modalidad_raw     VARCHAR2(200),
      modalidad_norm    VARCHAR2(32),
      categoria_raw     VARCHAR2(120),
      fecha_pub_utc     TIMESTAMP,
      fecha_limite_utc  TIMESTAMP,
      tz_portal         VARCHAR2(64),
      moneda            VARCHAR2(3),
      monto_original    NUMBER(14,2),
      monto_usd         NUMBER(14,2),
      fx_fecha          DATE,
      url_portal        VARCHAR2(1000),
      idioma            VARCHAR2(8)   DEFAULT 'es' NOT NULL,
      creado_en         TIMESTAMP     DEFAULT SYSTIMESTAMP NOT NULL,
      actualizado_en    TIMESTAMP     DEFAULT SYSTIMESTAMP NOT NULL,
      CONSTRAINT uq_proceso_natural UNIQUE (portal_id, numero_referencia),
      CONSTRAINT fk_proceso_portal FOREIGN KEY (portal_id) REFERENCES portal(portal_id)
    )
  ]';
EXCEPTION WHEN OTHERS THEN
  IF SQLCODE != -955 THEN RAISE; END IF;
END;
/

BEGIN
  EXECUTE IMMEDIATE 'CREATE INDEX ix_proceso_portal_fecha ON proceso(portal_id, fecha_pub_utc)';
EXCEPTION WHEN OTHERS THEN
  IF SQLCODE != -955 AND SQLCODE != -1408 THEN RAISE; END IF;
END;
/

BEGIN
  EXECUTE IMMEDIATE 'CREATE INDEX ix_proceso_institucion_norm ON proceso(institucion_norm)';
EXCEPTION WHEN OTHERS THEN
  IF SQLCODE != -955 AND SQLCODE != -1408 THEN RAISE; END IF;
END;
/

-- `cliente_proceso`: la VISTA DEL CLIENTE — TODOS los procesos
-- clasificados, no solo los que llegaron a ANALIZADO (esto es lo que
-- cierra el embudo real: hoy licitaciones_metadata sí guarda todos los
-- estados, pero licitaciones_analisis solo existe para ANALIZADO).
BEGIN
  EXECUTE IMMEDIATE q'[
    CREATE TABLE cliente_proceso (
      cliente_id          VARCHAR2(64)  NOT NULL,
      proceso_gid         VARCHAR2(96)  NOT NULL,
      estado              VARCHAR2(32)  DEFAULT 'DESCUBIERTO' NOT NULL,
      es_relevante        NUMBER(1),
      match_tecnico       NUMBER(3,2),
      match_categoria     NUMBER(3,2),
      factibilidad        NUMBER(3,2),
      match_overall       NUMBER(3,2),
      categoria_detectada VARCHAR2(120),
      tags                JSON,
      marcas              JSON,
      razon               VARCHAR2(2000),
      triaje              VARCHAR2(8),
      triaje_conf         NUMBER(3,2),
      formato             VARCHAR2(16),
      n_paginas           NUMBER(5),
      intentos            NUMBER(3)     DEFAULT 0 NOT NULL,
      ultimo_error        VARCHAR2(1000),
      clasificado_en      TIMESTAMP,
      actualizado_en      TIMESTAMP     DEFAULT SYSTIMESTAMP NOT NULL,
      CONSTRAINT pk_cliente_proceso PRIMARY KEY (cliente_id, proceso_gid),
      CONSTRAINT fk_cliente_proceso_cliente FOREIGN KEY (cliente_id)
        REFERENCES cliente(cliente_id),
      CONSTRAINT fk_cliente_proceso_proceso FOREIGN KEY (proceso_gid)
        REFERENCES proceso(proceso_gid) ON DELETE CASCADE,
      CONSTRAINT ck_cliente_proceso_relevante CHECK (es_relevante IN (0,1)),
      CONSTRAINT ck_cliente_proceso_triaje CHECK (triaje IN ('GO','NO-GO')),
      CONSTRAINT ck_cliente_proceso_estado CHECK (estado IN (
        'DESCUBIERTO','DESCARGADO','FORMATO_NATIVO','FORMATO_ESCANEADO',
        'DESCARTADO_REGEX','DESCARTADO_TRIAJE','DESCARTADO_TAMANO',
        'APROBADO','VECTORIZADO','ANALIZADO','NOTIFICADO',
        'REQUIERE_REVISION_MANUAL','ERROR_DESCARGA','ERROR_PARSEO',
        'ERROR_LLM','ERROR_BD'))
    )
  ]';
EXCEPTION WHEN OTHERS THEN
  IF SQLCODE != -955 THEN RAISE; END IF;
END;
/

BEGIN
  EXECUTE IMMEDIATE 'CREATE INDEX ix_cliente_proceso_estado ON cliente_proceso(cliente_id, estado)';
EXCEPTION WHEN OTHERS THEN
  IF SQLCODE != -955 AND SQLCODE != -1408 THEN RAISE; END IF;
END;
/

BEGIN
  EXECUTE IMMEDIATE 'CREATE INDEX ix_cliente_proceso_proceso ON cliente_proceso(proceso_gid)';
EXCEPTION WHEN OTHERS THEN
  IF SQLCODE != -955 AND SQLCODE != -1408 THEN RAISE; END IF;
END;
/

-- `analisis` v2: YA por (cliente, proceso) — a diferencia de
-- licitaciones_analisis, que es una fila por proceso aunque dos clientes
-- distintos lo analicen (el segundo cliente en pisar la fila gana).
BEGIN
  EXECUTE IMMEDIATE q'[
    CREATE TABLE analisis (
      cliente_id      VARCHAR2(64)  NOT NULL,
      proceso_gid     VARCHAR2(96)  NOT NULL,
      matriz_riesgos  JSON,
      severidad_max   VARCHAR2(16),
      resumen         VARCHAR2(4000),
      modelo_llm      VARCHAR2(64),
      monto_estimado  NUMBER(14,2),
      generado_en     TIMESTAMP     DEFAULT SYSTIMESTAMP NOT NULL,
      CONSTRAINT pk_analisis_v2 PRIMARY KEY (cliente_id, proceso_gid),
      CONSTRAINT fk_analisis_cliente_proceso FOREIGN KEY (cliente_id, proceso_gid)
        REFERENCES cliente_proceso(cliente_id, proceso_gid) ON DELETE CASCADE
    )
  ]';
EXCEPTION WHEN OTHERS THEN
  IF SQLCODE != -955 THEN RAISE; END IF;
END;
/

-- `chunk` v2: el texto vectorizado del pliego es del PROCESO (un pliego
-- público no es de un cliente en particular) — a diferencia de
-- licitaciones_chunks, que hoy cuelga de licitaciones_metadata.
BEGIN
  EXECUTE IMMEDIATE q'[
    CREATE TABLE chunk (
      chunk_id      NUMBER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      proceso_gid   VARCHAR2(96)  NOT NULL,
      seq           NUMBER(6)     NOT NULL,
      pagina        NUMBER(5),
      texto         CLOB,
      embedding     VECTOR(384, FLOAT32),
      idioma        VARCHAR2(8)   DEFAULT 'es' NOT NULL,
      creado_en     TIMESTAMP     DEFAULT SYSTIMESTAMP NOT NULL,
      CONSTRAINT fk_chunk_proceso FOREIGN KEY (proceso_gid)
        REFERENCES proceso(proceso_gid) ON DELETE CASCADE,
      CONSTRAINT uq_chunk_v2 UNIQUE (proceso_gid, seq)
    )
  ]';
EXCEPTION WHEN OTHERS THEN
  IF SQLCODE != -955 THEN RAISE; END IF;
END;
/

BEGIN
  EXECUTE IMMEDIATE 'CREATE INDEX ix_chunk_v2_proceso ON chunk(proceso_gid)';
EXCEPTION WHEN OTHERS THEN
  IF SQLCODE != -955 AND SQLCODE != -1408 THEN RAISE; END IF;
END;
/

-- ─────────────────────────────────────────────────────────────
-- 4) Correo, feedback y pipeline comercial
-- ─────────────────────────────────────────────────────────────
BEGIN
  EXECUTE IMMEDIATE q'[
    CREATE TABLE reportada (
      cliente_id    VARCHAR2(64)  NOT NULL,
      proceso_gid   VARCHAR2(96)  NOT NULL,
      reportado_en  TIMESTAMP     DEFAULT SYSTIMESTAMP NOT NULL,
      CONSTRAINT pk_reportada_v2 PRIMARY KEY (cliente_id, proceso_gid)
    )
  ]';
EXCEPTION WHEN OTHERS THEN
  IF SQLCODE != -955 THEN RAISE; END IF;
END;
/

BEGIN
  EXECUTE IMMEDIATE 'CREATE INDEX ix_reportada_v2_proceso ON reportada(proceso_gid)';
EXCEPTION WHEN OTHERS THEN
  IF SQLCODE != -955 AND SQLCODE != -1408 THEN RAISE; END IF;
END;
/

BEGIN
  EXECUTE IMMEDIATE q'[
    CREATE TABLE feedback (
      cliente_id    VARCHAR2(64)  NOT NULL,
      proceso_gid   VARCHAR2(96)  NOT NULL,
      destinatario  VARCHAR2(255) NOT NULL,
      voto          VARCHAR2(16)  NOT NULL,
      motivo        VARCHAR2(64),
      comentario    VARCHAR2(2000),
      votado_en     TIMESTAMP     DEFAULT SYSTIMESTAMP NOT NULL,
      CONSTRAINT pk_feedback_v2 PRIMARY KEY (cliente_id, proceso_gid, destinatario),
      CONSTRAINT ck_feedback_v2_voto CHECK (voto IN ('RELEVANTE','IRRELEVANTE'))
    )
  ]';
EXCEPTION WHEN OTHERS THEN
  IF SQLCODE != -955 THEN RAISE; END IF;
END;
/

BEGIN
  EXECUTE IMMEDIATE 'CREATE INDEX ix_feedback_v2_proceso ON feedback(proceso_gid)';
EXCEPTION WHEN OTHERS THEN
  IF SQLCODE != -955 AND SQLCODE != -1408 THEN RAISE; END IF;
END;
/

-- `motivos`: TODOS los motivos del voto (v1 los guarda en JSON; `motivo` solo cabe
-- uno). `motivo` sigue siendo el primero, por compatibilidad con lo ya escrito.
-- Aditivo e idempotente: ORA-01430 = la columna ya existe.
BEGIN
  EXECUTE IMMEDIATE 'ALTER TABLE feedback ADD (motivos JSON)';
EXCEPTION WHEN OTHERS THEN
  IF SQLCODE != -1430 THEN RAISE; END IF;
END;
/

-- `oportunidad_estado`: el Pipeline comercial. `origen='AUTOMATICO_MERCADO'`
-- son filas que src/mercado/service.py prepuebla al resolver un resultado
-- (ver db/schema.sql §6 y el plan §3.5); `origen='MANUAL'` son las que
-- captura una persona desde el portal (Fase 1, todavía no existe).
BEGIN
  EXECUTE IMMEDIATE q'[
    CREATE TABLE oportunidad_estado (
      cliente_id             VARCHAR2(64)  NOT NULL,
      proceso_gid            VARCHAR2(96)  NOT NULL,
      estado                 VARCHAR2(16)  DEFAULT 'NUEVO' NOT NULL,
      responsable_usuario_id VARCHAR2(64),
      motivo                 VARCHAR2(500),
      monto_ofertado         NUMBER(14,2),
      monto_adjudicado       NUMBER(14,2),
      competidor             VARCHAR2(300),
      origen                 VARCHAR2(24)  DEFAULT 'MANUAL' NOT NULL,
      actualizado_en         TIMESTAMP     DEFAULT SYSTIMESTAMP NOT NULL,
      CONSTRAINT pk_oportunidad_estado PRIMARY KEY (cliente_id, proceso_gid),
      CONSTRAINT ck_oportunidad_estado_estado CHECK (estado IN (
        'NUEVO','EVALUANDO','OFERTADO','GANADO','PERDIDO','DESCARTADO')),
      CONSTRAINT ck_oportunidad_estado_origen CHECK (origen IN
        ('MANUAL','AUTOMATICO_MERCADO'))
    )
  ]';
EXCEPTION WHEN OTHERS THEN
  IF SQLCODE != -955 THEN RAISE; END IF;
END;
/

BEGIN
  EXECUTE IMMEDIATE 'CREATE INDEX ix_oportunidad_estado_estado ON oportunidad_estado(cliente_id, estado)';
EXCEPTION WHEN OTHERS THEN
  IF SQLCODE != -955 AND SQLCODE != -1408 THEN RAISE; END IF;
END;
/

-- `oportunidad_evento`: historial append-only de transiciones. Nunca se
-- actualiza una fila existente, solo se inserta (mismo espíritu que
-- `auditoria` y que `ejecucion` en db/schema.sql).
BEGIN
  EXECUTE IMMEDIATE q'[
    CREATE TABLE oportunidad_evento (
      evento_id       NUMBER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      cliente_id      VARCHAR2(64)  NOT NULL,
      proceso_gid     VARCHAR2(96)  NOT NULL,
      estado_anterior VARCHAR2(16),
      estado_nuevo    VARCHAR2(16)  NOT NULL,
      usuario_id      VARCHAR2(64),
      nota            VARCHAR2(1000),
      ts              TIMESTAMP     DEFAULT SYSTIMESTAMP NOT NULL,
      CONSTRAINT fk_oportunidad_evento_estado FOREIGN KEY (cliente_id, proceso_gid)
        REFERENCES oportunidad_estado(cliente_id, proceso_gid)
    )
  ]';
EXCEPTION WHEN OTHERS THEN
  IF SQLCODE != -955 THEN RAISE; END IF;
END;
/

BEGIN
  EXECUTE IMMEDIATE 'CREATE INDEX ix_oportunidad_evento_proceso ON oportunidad_evento(cliente_id, proceso_gid, ts)';
EXCEPTION WHEN OTHERS THEN
  IF SQLCODE != -955 AND SQLCODE != -1408 THEN RAISE; END IF;
END;
/

-- ─────────────────────────────────────────────────────────────
-- 5) Salud operativa
-- ─────────────────────────────────────────────────────────────
-- `ejecucion_etapa`: detalle por etapa de una corrida — `ejecucion`
-- (db/schema.sql §8) queda como el resumen de la corrida completa; esta
-- tabla es nueva, no reemplaza nada.
BEGIN
  EXECUTE IMMEDIATE q'[
    CREATE TABLE ejecucion_etapa (
      ejecucion_id       NUMBER        NOT NULL,
      portal_id          VARCHAR2(32)  NOT NULL,
      etapa              VARCHAR2(32)  NOT NULL,
      inicio             TIMESTAMP,
      fin                TIMESTAMP,
      estado             VARCHAR2(16),
      procesos_revisados NUMBER(6)     DEFAULT 0 NOT NULL,
      relevantes         NUMBER(6)     DEFAULT 0 NOT NULL,
      errores            NUMBER(6)     DEFAULT 0 NOT NULL,
      CONSTRAINT pk_ejecucion_etapa PRIMARY KEY (ejecucion_id, portal_id, etapa),
      CONSTRAINT fk_ejecucion_etapa_ejecucion FOREIGN KEY (ejecucion_id)
        REFERENCES ejecucion(id),
      CONSTRAINT ck_ejecucion_etapa_etapa CHECK (etapa IN
        ('SCRAPE','CLASIFICAR','ANALIZAR','NOTIFICAR')),
      CONSTRAINT ck_ejecucion_etapa_estado CHECK (estado IN ('OK','ERROR','PARCIAL'))
    )
  ]';
EXCEPTION WHEN OTHERS THEN
  IF SQLCODE != -955 THEN RAISE; END IF;
END;
/

-- `kpi_snapshot`: la única agregada que se crea por anticipado (el resto,
-- `agg_dia_*`, se diferían hasta que una consulta concreta lo exija — ver
-- el plan §2.2/§3.2). Historia diaria, no optimización de lectura.
BEGIN
  EXECUTE IMMEDIATE q'[
    CREATE TABLE kpi_snapshot (
      cliente_id    VARCHAR2(64)  NOT NULL,
      dia           DATE          NOT NULL,
      kpi_id        VARCHAR2(64)  NOT NULL,
      valor         NUMBER(20,6)  NOT NULL,
      dimensiones   JSON,
      calculado_en  TIMESTAMP     DEFAULT SYSTIMESTAMP NOT NULL,
      CONSTRAINT pk_kpi_snapshot PRIMARY KEY (cliente_id, dia, kpi_id)
    )
  ]';
EXCEPTION WHEN OTHERS THEN
  IF SQLCODE != -955 THEN RAISE; END IF;
END;
/

-- ─────────────────────────────────────────────────────────────
-- 6) Identidad y auditoría (diseñadas ahora, activadas en la Fase 1 del
--    portal — ver el plan §5.2/§5.3; sin lectores todavía)
-- ─────────────────────────────────────────────────────────────
BEGIN
  EXECUTE IMMEDIATE q'[
    CREATE TABLE usuario (
      usuario_id      VARCHAR2(64)  CONSTRAINT pk_usuario PRIMARY KEY,
      org_id          VARCHAR2(32)  NOT NULL,
      email           VARCHAR2(255) NOT NULL,
      nombre          VARCHAR2(200),
      mfa_totp_secret VARCHAR2(64),
      activo          NUMBER(1)     DEFAULT 1 NOT NULL,
      creado_en       TIMESTAMP     DEFAULT SYSTIMESTAMP NOT NULL,
      CONSTRAINT uq_usuario_email UNIQUE (org_id, email),
      CONSTRAINT fk_usuario_org FOREIGN KEY (org_id) REFERENCES organizacion(org_id),
      CONSTRAINT ck_usuario_activo CHECK (activo IN (0,1))
    )
  ]';
EXCEPTION WHEN OTHERS THEN
  IF SQLCODE != -955 THEN RAISE; END IF;
END;
/

BEGIN
  EXECUTE IMMEDIATE q'[
    CREATE TABLE membresia (
      usuario_id   VARCHAR2(64)  NOT NULL,
      cliente_id   VARCHAR2(64)  NOT NULL,
      rol          VARCHAR2(32)  NOT NULL,
      alcance      JSON,
      creado_en    TIMESTAMP     DEFAULT SYSTIMESTAMP NOT NULL,
      CONSTRAINT pk_membresia PRIMARY KEY (usuario_id, cliente_id),
      CONSTRAINT fk_membresia_usuario FOREIGN KEY (usuario_id) REFERENCES usuario(usuario_id),
      CONSTRAINT fk_membresia_cliente FOREIGN KEY (cliente_id) REFERENCES cliente(cliente_id),
      CONSTRAINT ck_membresia_rol CHECK (rol IN
        ('ceo','director_estrategia','director_comercial','gerente_marca_oracle',
         'director_regional_ventas_oracle','director_servicios_oracle','gerente_pais',
         'equipo_ventas','admin_plataforma','admin_organizacion','operador'))
    )
  ]';
EXCEPTION WHEN OTHERS THEN
  IF SQLCODE != -955 THEN RAISE; END IF;
END;
/

-- `sesion_id` es el HASH del token opaco, nunca el token: si la tabla se
-- filtrara, no alcanza para reconstruir una sesión (mismo principio que
-- feedback/tokens.py con los enlaces firmados).
BEGIN
  EXECUTE IMMEDIATE q'[
    CREATE TABLE sesion (
      sesion_id   VARCHAR2(64)  CONSTRAINT pk_sesion PRIMARY KEY,
      usuario_id  VARCHAR2(64)  NOT NULL,
      creado_en   TIMESTAMP     DEFAULT SYSTIMESTAMP NOT NULL,
      expira_en   TIMESTAMP     NOT NULL,
      ip_hash     VARCHAR2(64),
      user_agent  VARCHAR2(255),
      revocada    NUMBER(1)     DEFAULT 0 NOT NULL,
      CONSTRAINT fk_sesion_usuario FOREIGN KEY (usuario_id) REFERENCES usuario(usuario_id),
      CONSTRAINT ck_sesion_revocada CHECK (revocada IN (0,1))
    )
  ]';
EXCEPTION WHEN OTHERS THEN
  IF SQLCODE != -955 THEN RAISE; END IF;
END;
/

BEGIN
  EXECUTE IMMEDIATE 'CREATE INDEX ix_sesion_usuario ON sesion(usuario_id, expira_en)';
EXCEPTION WHEN OTHERS THEN
  IF SQLCODE != -955 AND SQLCODE != -1408 THEN RAISE; END IF;
END;
/

-- `codigo_hash` es el hash del código de 8 caracteres enviado por correo
-- (acceso sin contraseña, ver el plan §5.2) — nunca el código en claro.
BEGIN
  EXECUTE IMMEDIATE q'[
    CREATE TABLE codigo_acceso (
      codigo_hash  VARCHAR2(64)  CONSTRAINT pk_codigo_acceso PRIMARY KEY,
      usuario_id   VARCHAR2(64)  NOT NULL,
      creado_en    TIMESTAMP     DEFAULT SYSTIMESTAMP NOT NULL,
      expira_en    TIMESTAMP     NOT NULL,
      usado_en     TIMESTAMP,
      intentos     NUMBER(2)     DEFAULT 0 NOT NULL,
      CONSTRAINT fk_codigo_acceso_usuario FOREIGN KEY (usuario_id) REFERENCES usuario(usuario_id)
    )
  ]';
EXCEPTION WHEN OTHERS THEN
  IF SQLCODE != -955 THEN RAISE; END IF;
END;
/

BEGIN
  EXECUTE IMMEDIATE q'[
    CREATE TABLE invitacion (
      invitacion_id  VARCHAR2(64)  CONSTRAINT pk_invitacion PRIMARY KEY,
      org_id         VARCHAR2(32)  NOT NULL,
      email          VARCHAR2(255) NOT NULL,
      rol_propuesto  VARCHAR2(32)  NOT NULL,
      invitado_por   VARCHAR2(64)  NOT NULL,
      estado         VARCHAR2(16)  DEFAULT 'PENDIENTE' NOT NULL,
      creado_en      TIMESTAMP     DEFAULT SYSTIMESTAMP NOT NULL,
      expira_en      TIMESTAMP     NOT NULL,
      CONSTRAINT fk_invitacion_org FOREIGN KEY (org_id) REFERENCES organizacion(org_id),
      CONSTRAINT fk_invitacion_invitador FOREIGN KEY (invitado_por) REFERENCES usuario(usuario_id),
      CONSTRAINT ck_invitacion_estado CHECK (estado IN
        ('PENDIENTE','ACEPTADA','EXPIRADA','REVOCADA'))
    )
  ]';
EXCEPTION WHEN OTHERS THEN
  IF SQLCODE != -955 THEN RAISE; END IF;
END;
/

-- `auditoria`: append-only, nunca se actualiza ni se borra una fila
-- (retención 24 meses — ver el plan §5.4). `id` es identity porque un
-- evento de auditoría no tiene ninguna clave natural propia.
BEGIN
  EXECUTE IMMEDIATE q'[
    CREATE TABLE auditoria (
      auditoria_id  NUMBER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      usuario_id    VARCHAR2(64),
      org_id        VARCHAR2(32)  NOT NULL,
      accion        VARCHAR2(64)  NOT NULL,
      recurso       VARCHAR2(200),
      alcance       JSON,
      ip_hash       VARCHAR2(64),
      ts            TIMESTAMP     DEFAULT SYSTIMESTAMP NOT NULL
    )
  ]';
EXCEPTION WHEN OTHERS THEN
  IF SQLCODE != -955 THEN RAISE; END IF;
END;
/

BEGIN
  EXECUTE IMMEDIATE 'CREATE INDEX ix_auditoria_org_ts ON auditoria(org_id, ts)';
EXCEPTION WHEN OTHERS THEN
  IF SQLCODE != -955 AND SQLCODE != -1408 THEN RAISE; END IF;
END;
/

-- ============================================================================
-- Fin del DDL idempotente de la Fase "Expandir".
--
-- Deliberadamente NO incluido en este archivo (ver el plan §3.2/§3.3):
--   * agg_dia_* — diferidas hasta que una consulta concreta lo exija.
--   * finops_*/focus_* — programa aparte, su propio db/finops.sql.
--   * CREATE VECTOR INDEX sobre chunk.embedding — tarea aparte, opt-in,
--     mismo criterio que db/index_ivf.sql sobre licitaciones_chunks.
--   * Semilla de datos (portal, organizacion, cliente, cliente_config) —
--     va en scripts/semilla_esquema_v2.py, idempotente, no en DDL estático.
-- ============================================================================
