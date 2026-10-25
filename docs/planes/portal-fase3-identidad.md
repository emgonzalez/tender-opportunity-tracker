# Portal AMALIA · Fase 3 — identidad, roles y pipeline comercial manual

> **Qué es este documento:** el plan de construcción de la Fase 3 que
> [`portal-amalia.md`](portal-amalia.md) enuncia en una línea (§3 *Fases de
> implementación* → «Fase 3 — construir identidad y pipeline comercial manual»). **Nada
> de lo que sigue está implementado.** Las decisiones de negocio que bloquean cada
> entrega están juntas al final, en [§9 Preguntas abiertas](#9-preguntas-abiertas-para-emilio).
>
> **Qué no repite:** la audiencia, la regla de acceso textual, los 11 códigos de rol
> propuestos y el catálogo de KPIs viven en [`portal-amalia.md`](portal-amalia.md) §1,
> §1.2 y §3. Acá se enlazan; si cambian, se cambian allá.
>
> **Línea base:** rama `develop` en `2c8d2f0` (PR #77, KPIs operativos 11-13). Cada
> afirmación sobre el código cita archivo y línea leídos en ese commit; lo que no se
> pudo comprobar desde el repo (filas reales en la ADB, estado de la VM) está marcado
> **No verificable**.

---

## 0. Punto de partida verificado

### 0.1 Lo que ya existe y se reutiliza

| Pieza | Dónde | Qué aporta a la Fase 3 |
|---|---|---|
| Fábrica de la app | [`src/web/app.py:26-59`](../../src/web/app.py) | `crear_app(cfg)` con routers montables; el portal ya está montado (`:40`) |
| 404 único | [`src/web/app.py:51-57`](../../src/web/app.py), [`src/web/feedback.py:55-58`](../../src/web/feedback.py) | Ruta inexistente y token inválido responden idéntico — el mismo contrato sirve para "no autorizado" |
| Middlewares ASGI puros | [`src/web/seguridad.py:35-107`](../../src/web/seguridad.py) | `EncabezadosSeguros` (CSP, `no-store`, `no-referrer`, `nosniff`) y `LimiteCuerpo` (16 KB). Patrón a copiar para el middleware de sesión |
| CSP del portal | [`src/web/seguridad.py:31-32`](../../src/web/seguridad.py) | `script-src 'self'`, `form-action 'self'`; se aplica a todo `/portal*` (`:46`) |
| Jinja2 con autoescape siempre | [`src/web/plantillas.py:21-28`](../../src/web/plantillas.py) | Única defensa XSS actual; vale también para los formularios nuevos |
| Sin log de acceso | [`src/web/__main__.py:17-20`](../../src/web/__main__.py) | `access_log=False` y **un solo worker** — relevante para el rate-limit en memoria (§4) |
| Gate del portal | [`src/web/portal.py:35-40`](../../src/web/portal.py), [`src/config.py:277-278`](../../src/config.py) | Hoy `ENABLE_PORTAL` + un único `PORTAL_CLIENTE_ID` fijo: es exactamente lo que la Fase 3 reemplaza |
| KPIs por cliente | [`src/analytics/pais.py:55-215`](../../src/analytics/pais.py) | Toda función ya recibe `cliente_id` como parámetro: la autorización se resuelve **antes** de llamarlas |
| KPIs operativos | [`src/analytics/operativo.py:23-81`](../../src/analytics/operativo.py) | **Sin `cliente_id`**: son de plataforma; sólo roles técnicos |
| Envío de correo | [`src/notifier/email_sender.py:44-61`](../../src/notifier/email_sender.py), [`:112-127`](../../src/notifier/email_sender.py) | `resolve_recipients` + `_send` — el código de acceso sale por acá (ver amenaza T9) |
| Firma HMAC | [`src/feedback/tokens.py:51-53`](../../src/feedback/tokens.py) | Patrón de HMAC + `compare_digest` a reutilizar para el hash del código |
| Escritor automático del pipeline | [`src/mercado/repo.py:258-300`](../../src/mercado/repo.py) | `prepoblar_oportunidad_estado_v2`: MERGE con `origen='AUTOMATICO_MERCADO'` que **nunca pisa** una fila `MANUAL` (`:286`) |
| Test de no-cruce | [`tests/integration/test_oracle_local.py:1860`](../../tests/integration/test_oracle_local.py) | `test_buscar_precedentes_v2_no_cruza_clientes`: el molde del test de aislamiento por alcance |
| Proxy TLS | [`oci/caddy/Caddyfile:9-17`](../../oci/caddy/Caddyfile) | Sólo reenvía `/f/*` y `/healthz`; todo lo demás es 404 en el borde |

### 0.2 Esquema de identidad: qué hay y qué falta

Las seis tablas de identidad **ya existen** en [`db/schema_v2.sql`](../../db/schema_v2.sql)
§6 (`:523-662`), marcadas «sin lectores todavía» (`:524`). Si tienen filas en la ADB de
`multicomputos_dev`: **No verificable** desde el repo (ninguna línea de `src/` las
escribe — `grep` de `usuario`/`membresia`/`sesion`/`codigo_acceso` en `src/` sin
resultados de escritura); la Entrega 1 lo comprueba antes de alterar nada.

| Tabla | Línea | Estado | Lo que le falta para la Fase 3 |
|---|---|---|---|
| `usuario` | `:528-539` | Sirve tal cual | `UNIQUE (org_id, email)`: el login busca por email **sin** saber la org → con una sola organización no hay ambigüedad; con la Fase 4 (SaaS) sí. Se documenta, no se cambia ahora |
| `membresia` | `:548-559` | **Insuficiente** | (a) `CHECK rol` con 9 códigos (`:557-558`) vs. los 11 de [`portal-amalia.md`](portal-amalia.md) §1.2; (b) PK `(usuario_id, cliente_id)` (`:554`) obliga a una fila por país para los 6 roles regionales. `alcance JSON` (`:552`) existe pero sin semántica definida |
| `sesion` | `:571-582` | Sirve tal cual | `sesion_id` = hash del token (comentario `:566-568`). Falta un índice o job de purga de vencidas (no está en [`db/retention_v2.sql`](../../db/retention_v2.sql)) |
| `codigo_acceso` | `:599-608` | **Defecto de diseño** | PK = `codigo_hash` (`:600`): dos usuarios no pueden tener a la vez códigos con el mismo hash, y si el hash es un SHA-256 simple de 8 caracteres se invierte offline por fuerza bruta en segundos si la tabla se filtra. Ver Entrega 1 |
| `invitacion` | `:616-629` | Sirve, no se usa en la Fase 3 mínima | El alta inicial es por CLI (Entrega 2); la UI de invitaciones es la Entrega 8, opcional |
| `auditoria` | `:641-650` | Sirve tal cual | Append-only, 24 meses (`:637`); falta decidir **qué** se audita (pregunta P6) |
| `oportunidad_estado` | `:411-427` | Sirve | `responsable_usuario_id` (`:415`) ya está; no tiene FK a `usuario` (deliberado o no: No verificable) |
| `oportunidad_evento` | `:446-457` | Sirve, **sin escritor** | Ningún módulo de `src/` ni `scripts/` la escribe (`grep oportunidad_evento` vacío fuera de `db/`) |

### 0.3 Dos hallazgos que condicionan el diseño

1. **La captura manual congela el resultado automático.** El MERGE de
   [`src/mercado/repo.py:282-286`](../../src/mercado/repo.py) actualiza sólo si
   `origen = 'AUTOMATICO_MERCADO'`. En cuanto una persona toca una fila, pasa a
   `MANUAL`, y si después COMPRASAL publica la adjudicación, el GANADO/PERDIDO real **no
   llega nunca**. Además `prepoblar_oportunidad_estado_v2` no escribe
   `oportunidad_evento`, así que los KPI 15-16 (conversión y tiempo por etapa) no verían
   las transiciones automáticas. Ambos se resuelven en la Entrega 6 (pregunta P8).
2. **El servicio web sólo existe en la variante E2.** `web` y `caddy` están en
   [`docker-compose.e2.yml:41-80`](../../docker-compose.e2.yml) con `mem_limit: 160m`;
   [`docker-compose.a1.yml`](../../docker-compose.a1.yml) no los declara. Si la Fase M
   migra a A1 antes de la Entrega 7, el portal no tiene dónde correr. Coordinar con
   quien lleve la Fase M (dueño de `oci/`).

---

## 1. Modelo de autorización

### 1.1 Alcance efectivo

Toda petición autenticada resuelve, **una vez**, un objeto inmutable:

```text
Alcance(usuario_id, org_id, rol, regional: bool, clientes: frozenset[str])
```

- `regional=True` ⇔ el rol es uno de los 6 regionales o `AO`
  ([`portal-amalia.md`](portal-amalia.md) §1.2). Su `clientes` = **todos los `cliente`
  activos de su `org_id`**, leídos de la tabla `cliente` en el momento — así dar de alta
  un noveno país no obliga a tocar membresías.
- `regional=False` ⇔ `GP`/`VE`. Su `clientes` = las filas de `membresia` del usuario.
- `PA`/`OP` (técnicos): acceso a la vista operativa; acceso a vistas de negocio sólo si
  además tienen una membresía de negocio (pregunta P3).

**Regla dura:** ninguna función de `src/analytics/` lee `cliente_id` de la petición. El
router valida `?cliente=` contra `alcance.clientes` y sólo entonces llama. Un
`cliente_id` fuera del alcance responde el **404 único** (no un 403: un 403 confirma que
el país existe).

### 1.2 Matriz rol × capacidad (propuesta, a confirmar en P2/P4)

| Capacidad | EJ DE DC GM PV EX AO | GP | VE | OP | PA |
|---|---|---|---|---|---|
| Vista regional (R1-R6) | ✅ | — | — | — | — |
| Vista de país (KPIs 1-10, 14-16) | ✅ todos los países | ✅ su país | ✅ su país | — | — |
| Vista operativa (11-13) | — | — | — | ✅ | ✅ |
| Cambiar `oportunidad_estado` | DC ✅, resto lectura | ✅ | ✅ | — | — |
| Alta/baja de usuarios | AO ✅ | — | — | — | ✅ |

### 1.3 Cambio de esquema recomendado

Entre las dos opciones de [`portal-amalia.md`](portal-amalia.md) §1.2 se recomienda
**`membresia_org(usuario_id, org_id, rol)`** separada, no `cliente_id` NULLABLE: en
Oracle una columna de la PK no puede ser NULL, así que relajarla obliga a cambiar la PK
de `membresia` por una clave sustituta — más DDL y más riesgo que una tabla nueva.

---

## 2. Entregas (una PR cada una, en este orden)

Cada entrega deja `develop` desplegable y con `ENABLE_PORTAL` apagado en producción
hasta la Entrega 7. Lo que se agrega a `src/` entra también al árbol de
[`CLAUDE.md`](../../CLAUDE.md) en la misma PR (lo exige
[`tests/test_documentacion.py`](../../tests/test_documentacion.py)).

### Entrega 0 — Decisiones y ADR (sin código)

- **Alcance:** responder §9 y registrar las decisiones de seguridad en
  `docs/decisiones/0006-identidad-del-portal.md` (passwordless, cookie, TTLs, no-403).
- **Hecho:** ADR aceptado; P1-P8 respondidas por Emilio.

### Entrega 1 — DDL de identidad (Expandir, sin lectores)

- **Alcance:** en [`db/schema_v2.sql`](../../db/schema_v2.sql): ampliar
  `ck_membresia_rol` a 11 códigos; crear `membresia_org`; rehacer `codigo_acceso` con
  PK sustituta (`codigo_id` identity) + índice `(usuario_id, expira_en)`, conservando
  `codigo_hash` sin `UNIQUE`; agregar índice `sesion(expira_en)` para la purga; purga
  de `sesion`/`codigo_acceso` vencidos en [`db/retention_v2.sql`](../../db/retention_v2.sql).
- **Precondición (No verificable hoy):** `SELECT COUNT(*)` de `usuario`, `membresia`,
  `codigo_acceso` en `multicomputos_dev` = 0. Si es 0, `DROP`+`CREATE`; si no, migración
  con copia.
- **Patrón:** el DDL del repo es idempotente tragando `ORA-00955`
  ([`db/schema_v2.sql:24-30`](../../db/schema_v2.sql)); un `ALTER ... CHECK` no encaja en
  ese patrón → bloque PL/SQL que consulta `user_constraints` antes de dropear/recrear.
- **Tests:** contrapartes en [`tests/integration/test_oracle_local.py`](../../tests/integration/test_oracle_local.py):
  aplicar el DDL dos veces; `ORA-02290` con rol inventado; dos usuarios con el mismo
  `codigo_hash` no chocan.
- **Riesgo:** aplicar DDL a la ADB real es una operación humana (mismo patrón que el
  despliegue del esquema v2 del 2026-09-27); no lo hace un agente.

### Entrega 2 — `src/identidad/` (capa de datos, sin HTTP)

- **Alcance:** paquete nuevo, mismo corte que `src/feedback/` (repo + service, sin
  FastAPI):
  - `repo.py` — SQL de `usuario`, `membresia`, `membresia_org`, `sesion`,
    `codigo_acceso`, `auditoria`.
  - `codigos.py` — generar código (8 caracteres de un alfabeto de 32 sin ambiguos =
    40 bits, `secrets`), `hash = HMAC-SHA256(IDENTIDAD_SECRET, usuario_id ∥ código)`,
    verificar con `hmac.compare_digest` (patrón de
    [`src/feedback/tokens.py:51-53`](../../src/feedback/tokens.py)).
  - `sesiones.py` — token opaco `secrets.token_urlsafe(32)`; en BD sólo
    `sha256(token)` (cabe en `VARCHAR2(64)`, `:572`).
  - `alcance.py` — `resolver_alcance(conn, usuario_id) -> Alcance` (§1.1).
  - `__main__.py` — CLI de alta/baja: `python -m src.identidad alta --email … --rol GP
    --cliente multicomputos_sv`. Es el onboarding hasta que exista la Entrega 8.
- **Config nueva:** `IDENTIDAD_SECRET` (distinto de `FEEDBACK_HMAC_SECRET`: rotar uno no
  debe invalidar el otro), `SESION_TTL_HORAS`, `CODIGO_TTL_MIN` (valores en P5).
- **Tests:** unitarios con conexión mock (patrón de
  [`tests/test_feedback_repo_sql.py`](../../tests/test_feedback_repo_sql.py)); en Oracle
  local: `resolver_alcance` de un `GP` devuelve exactamente su país; de un `EJ`, todos
  los `cliente` activos de la org, incluido uno dado de alta después.
- **Hecho:** un usuario creado por CLI obtiene un `Alcance` correcto sin HTTP.

### Entrega 3 — Login passwordless y sesión (HTTP)

- **Alcance:** router `src/web/acceso.py` y middleware `src/web/sesion.py`.

  | Ruta | Escribe | Comportamiento |
  |---|---|---|
  | `GET /acceso` | no | Formulario de email |
  | `POST /acceso` | sí | Si el email tiene cuenta activa: invalida códigos previos, emite uno, lo envía. **Siempre** responde la misma página («si el correo tiene acceso, te llegó un código») y en el mismo tiempo (envío en `BackgroundTasks`) |
  | `POST /acceso/codigo` | sí | Verifica email + código; incrementa `intentos` (`:605`); al 5.º fallo invalida el código; si acierta, marca `usado_en`, crea `sesion`, rota la cookie, redirige a `/portal` |
  | `POST /salir` | sí | Marca `revocada=1` (`:578`) y borra la cookie |

- **Cookie:** `__Host-amalia_sesion`, `Secure; HttpOnly; SameSite=Lax; Path=/`, sin
  `Domain`. Se emite un token nuevo en cada login (anti-fijación).
- **Middleware:** ASGI puro como los de [`src/web/seguridad.py`](../../src/web/seguridad.py);
  sólo **resuelve** la cookie → `scope["state"]["alcance"]` (o nada). No bloquea: el
  bloqueo lo hace una dependencia `requiere_alcance` en cada ruta, para que `/f/*` y
  `/healthz` sigan sin sesión.
- **CSRF:** token sincronizado por sesión en un campo oculto de cada formulario POST +
  verificación de `Origin`/`Referer` contra `FEEDBACK_BASE_URL`. `SameSite=Lax` solo no
  alcanza (no cubre subdominios del mismo sitio).
- **Reutiliza:** `pagina()` y el 404 único; la plantilla base del portal
  (`templates/portal_base.html`).
- **Tests:** `tests/test_web_acceso.py` con `TestClient`: misma respuesta y mismo
  status para email existente/inexistente; código vencido, usado, de otro usuario;
  quinto intento invalida; cookie con los cuatro atributos; POST sin token CSRF → 403;
  `/f/*` sigue funcionando sin cookie (regresión de
  [`tests/test_web_feedback.py`](../../tests/test_web_feedback.py)).
- **Hecho:** login completo contra Oracle local con un usuario creado por la CLI.

### Entrega 4 — Autorización por rol y por país

- **Alcance:** [`src/web/portal.py`](../../src/web/portal.py) deja de leer
  `cfg.portal_cliente_id` (`:40`) y toma el país de `?cliente=` validado contra el
  alcance (un rol de país no ve selector). `/portal/operativo` exige `OP`/`PA`. Nueva
  vista regional `/portal/regional` (R1, R6; R2-R5 siguen bloqueados por datos según
  [`portal-amalia.md`](portal-amalia.md) §3) con funciones nuevas en `src/analytics/`
  que reciben `clientes: Sequence[str]` ya filtrado.
- **Auditoría mínima:** `LOGIN_OK`, `LOGIN_FALLIDO`, `SESION_REVOCADA`, `ESTADO_CAMBIADO`
  (alcance según P6).
- **Tests:** el test exigido por [`portal-amalia.md`](portal-amalia.md) §Verificación —
  parametrizado sobre **todas** las funciones públicas de `src/analytics/pais.py` y de
  la vista regional: con dos clientes sembrados, un alcance de un solo país nunca recibe
  filas del otro (molde: [`test_oracle_local.py:1860`](../../tests/integration/test_oracle_local.py)).
  En HTTP: `?cliente=` ajeno → 404 idéntico al de ruta inexistente; `VE` en
  `/portal/operativo` → 404.
- **Riesgo:** olvidar el filtro en una función futura. Mitigación: el test itera las
  funciones por introspección del módulo, no por lista escrita a mano.

### Entrega 5 — Captura manual del pipeline comercial (KPI 14-16)

- **Alcance:** en la tabla de procesos de la vista de país, un control por fila para
  cambiar el estado (`NUEVO → EVALUANDO → OFERTADO → GANADO|PERDIDO`, `DESCARTADO` desde
  cualquiera; transiciones válidas en una constante, no en la BD). `POST
  /portal/oportunidad/{proceso_gid}/estado` con CSRF, que en **una transacción**: MERGE
  en `oportunidad_estado` con `origen='MANUAL'`, `responsable_usuario_id`, `motivo`,
  `monto_ofertado`, `competidor`; INSERT en `oportunidad_evento` con
  `estado_anterior`/`estado_nuevo`/`usuario_id`; INSERT en `auditoria`.
- **Concurrencia:** el formulario lleva `actualizado_en` leído; el UPDATE exige que no
  haya cambiado (si cambió, se recarga con aviso).
- **IDOR:** el `proceso_gid` de la URL se acepta sólo si existe
  `cliente_proceso(cliente_id ∈ alcance, proceso_gid)`.
- **Analytics:** KPI 15 (conversión) y 16 (tiempo por etapa) sobre `oportunidad_evento`.
- **Tests:** transición inválida rechazada; cambio sobre proceso de otro país → 404;
  evento escrito con el estado anterior correcto; rollback si falla el evento.
- **Hecho:** un `GP` mueve una oportunidad y el embudo comercial la refleja.

### Entrega 6 — Convivencia con `src/mercado` (hallazgo 0.3.1)

- **Alcance (P8, resuelta):** un **resultado terminal** de mercado (GANADO/PERDIDO) hace avanzar una
  fila `MANUAL` en etapa intermedia; una fila `MANUAL` ya final y distinta **no se pisa** y queda como
  conflicto, derivado al leer en `src/mercado/repo.py` (`listar_conflictos_oportunidad_v2`). Toda
  transición automática inserta su `oportunidad_evento` con `usuario_id` NULL. **Implementado en #88**
  (`prepoblar_oportunidad_estado_v2`). **Pendiente:** mostrar los conflictos en la UI (E5) y quién los resuelve.
- **Por qué separado:** `src/mercado/` lo trabaja otro frente; esta entrega se coordina
  con su dueño y va después de la 5 para tener ya el escritor de eventos.

### Entrega 7 — Exposición pública tras Caddy

- **Alcance:**
  - [`oci/caddy/Caddyfile:10`](../../oci/caddy/Caddyfile): `@expuesto path /f/* /healthz
    /acceso /acceso/* /salir /portal /portal/*`.
  - [`tests/test_oci_web.py:59-64`](../../tests/test_oci_web.py): hoy **afirma que
    `/portal` nunca se publica** (`:63`); se reescribe en la misma PR para afirmar la
    lista nueva y que el resto sigue en `respond 404`.
  - [`src/web/seguridad.py:26`](../../src/web/seguridad.py) y `:31-32`: agregar
    `frame-ancestors 'none'; base-uri 'none'` a ambas CSP — `default-src` **no** cubre
    `frame-ancestors`, así que hoy no hay protección contra clickjacking — y
    `Strict-Transport-Security` (en Caddy o en el middleware; ninguno lo emite hoy).
  - [`src/web/__main__.py:20`](../../src/web/__main__.py): `forwarded_allow_ips` para la
    IP del contenedor de Caddy; sin eso, toda petición llega con la IP de Caddy y el
    rate-limit por IP limita a todos a la vez.
  - `ENABLE_PORTAL=true` en el tenant (archivo de `oci/`, área de Emilio).
- **Orden:** sólo después de que 3-4 estén en `develop` y probadas contra Oracle local.
- **Verificación en vivo:** `curl -I` de `/portal` sin cookie → redirección a `/acceso`;
  con `?cliente=` ajeno → 404; medir RSS del contenedor `web` contra los 160 MB.
- **Hecho:** un usuario real entra desde fuera de la VM con un código recibido por correo.

### Entrega 8 — Invitaciones y administración (opcional)

UI para `AO`/`PA` sobre `invitacion` (`:616-629`): invitar, revocar, listar sesiones
activas de un usuario y revocarlas. Sólo si el volumen de usuarios lo justifica (P1).

### Entrega 9 — KPI 17 (vista ejecutiva)

Primero sin `kpi_snapshot`: calcular en vivo los 3-4 números del CEO a partir de las
funciones ya filtradas. Materializar en `kpi_snapshot` (`:507-516`) sólo si la
latencia medida lo exige — mismo criterio de diferimiento que `agg_dia_*`
([`db/schema_v2.sql:667-668`](../../db/schema_v2.sql)).

---

## 3. Grafo de dependencias

```text
E0 ─► E1 ─► E2 ─► E3 ─► E4 ─► E5 ─► E6
                         │      └──► E9
                         └────────────► E7 (requiere E3+E4; E5 recomendable)
E8 opcional, después de E4
```

E5 y E7 pueden ir en paralelo con aislamiento: no comparten archivos salvo
[`src/web/seguridad.py`](../../src/web/seguridad.py), que sólo toca E7.

---

## 4. Modelo de amenazas

| # | Amenaza | Control | Entrega |
|---|---|---|---|
| T1 | **Fuerza bruta del código** (40 bits) | 5 intentos por código (`intentos`, `:605`); 3 códigos por usuario y hora; limitador por IP en memoria (vale porque hay **un solo worker**, [`src/web/__main__.py:19-20`](../../src/web/__main__.py); se pierde al reiniciar, aceptable); TTL corto | E2-E3 |
| T2 | **Enumeración de cuentas** | Misma página, mismo status y mismo tiempo para email con y sin cuenta; el envío SMTP va a `BackgroundTasks` para que su latencia no delate | E3 |
| T3 | **CSRF** | Token sincronizado + `Origin`; `SameSite=Lax`; GET nunca escribe (misma regla que [`src/web/feedback.py:8-10`](../../src/web/feedback.py)) | E3, E5 |
| T4 | **Robo/fijación de cookie** | `__Host-`, `Secure`, `HttpOnly`; token nuevo en cada login; en BD sólo el hash (`:566-568`); revocación server-side | E3 |
| T5 | **Filtración de la tabla de códigos** | HMAC con secreto del servidor, no SHA-256 simple; el código también se invalida al usarse | E1-E2 |
| T6 | **Fuga entre países (IDOR)** | `Alcance` resuelto una vez; 404 único; test por introspección sobre todo `src/analytics/` | E4-E5 |
| T7 | **Clickjacking** | `frame-ancestors 'none'` (hoy ausente) | E7 |
| T8 | **XSS** | Autoescape siempre ([`src/web/plantillas.py:28`](../../src/web/plantillas.py)); CSP sin `unsafe-inline` en scripts | ya existe |
| T9 | **Códigos redirigidos en modo validación** | `resolve_recipients` redirige **todo** correo al override ([`src/notifier/email_sender.py:49-61`](../../src/notifier/email_sender.py)): en DEV, el operador recibiría los códigos de todos. Decisión explícita en P7 | E3 |
| T10 | **Correo comprometido = cuenta comprometida** | Inherente a passwordless. Para `PA`, segundo factor TOTP sobre `usuario.mfa_totp_secret` (`:533`, ya existe) — P4 | E3 o posterior |
| T11 | **Open redirect** tras login | Sólo rutas relativas internas en `?siguiente=` | E3 |
| T12 | **Logs con secretos** | Ni código, ni token, ni cookie al log (mismo criterio que [`src/web/feedback.py:119-122`](../../src/web/feedback.py)) | E3 |
| T13 | **DoS de memoria** | `LimiteCuerpo` 16 KB ya aplica a todo POST ([`src/web/seguridad.py:15`](../../src/web/seguridad.py)); medir RSS con sesiones | E7 |

---

## 5. Archivos críticos

| Crear | Modificar |
|---|---|
| `src/identidad/{__init__,repo,codigos,sesiones,alcance,__main__}.py` | [`db/schema_v2.sql`](../../db/schema_v2.sql), [`db/retention_v2.sql`](../../db/retention_v2.sql) |
| `src/web/acceso.py`, `src/web/sesion.py` | [`src/web/app.py`](../../src/web/app.py), [`src/web/portal.py`](../../src/web/portal.py), [`src/web/seguridad.py`](../../src/web/seguridad.py), [`src/web/__main__.py`](../../src/web/__main__.py) |
| `templates/acceso_*.html`, `templates/portal_regional.html` | [`src/config.py`](../../src/config.py), [`.env.example`](../../.env.example) |
| `tests/test_identidad_*.py`, `tests/test_web_acceso.py` | [`tests/integration/test_oracle_local.py`](../../tests/integration/test_oracle_local.py), [`tests/test_oci_web.py`](../../tests/test_oci_web.py) |
| `docs/decisiones/0006-identidad-del-portal.md` | [`oci/caddy/Caddyfile`](../../oci/caddy/Caddyfile) (área de Emilio), [`src/mercado/repo.py`](../../src/mercado/repo.py) (E6), [`CLAUDE.md`](../../CLAUDE.md) (árbol) |

---

## 6. Verificación

- Cada PR: `ruff check . && pytest -q` y, si toca SQL, el job de Oracle 23ai de
  [`.github/workflows/ci.yml`](../../.github/workflows/ci.yml).
- Antes de la Entrega 7: prueba manual de los 13 controles de §4 contra el servicio
  levantado localmente con Oracle local.
- Después de la Entrega 7: los tres `curl` de la entrega y un login real de Emilio.

## 7. Riesgos

- **Pocos usuarios, mucho mecanismo.** Si P1 responde «5 personas», las Entregas 8-9
  sobran y se cierran como no-hacer.
- **SMTP es el único canal del código.** Gmail App Password (ver
  [`CLAUDE.md`](../../CLAUDE.md) → *SMTP Auth Failures*): si el SMTP cae, nadie entra.
  [`src/notifier/ops_alert.py`](../../src/notifier/ops_alert.py) no sirve para esto (es
  para el operador). Aceptar o prever una sesión de larga duración para el operador.
- **Memoria de la VM:** 160 MB para `web` en E2.1.Micro; sin medir con sesiones y
  consultas regionales.
- **Esquema v2 todavía en «Expandir»:** si la fase «Contract» funde `schema_v2.sql` en
  `schema.sql` en medio de la Fase 3, las rutas de esta guía cambian.

## 8. Fuera de alcance

Scraper multi-portal (los otros 7 países, [`portal-amalia.md`](portal-amalia.md) §1.1);
SSO/OIDC corporativo; facturación SaaS (Fase 4 del portal); hallazgos 4-6 de
[`portal-amalia.md`](portal-amalia.md) §3 (monto, fecha límite, servicio vs. producto).

## 9. Preguntas abiertas para Emilio

| # | Pregunta | Bloquea |
|---|---|---|
| P1 | ¿Quién recibe cuenta en la primera ola (nombres/roles) y cuántas personas son en total? ¿Sólo El Salvador al inicio? | E2 (CLI de alta), E8 |
| P2 | ¿Confirmás los 11 códigos de rol de [`portal-amalia.md`](portal-amalia.md) §1.2 y `membresia_org` para los regionales? | E1 |
| P3 | ¿El Director de Estrategia que además es Gerente de País necesita algo distinto de "regional"? ¿Los roles técnicos ven datos de negocio? | E4 |
| P4 | ¿Quién puede cambiar el estado comercial (sólo GP/VE/DC, o también regionales)? ¿Segundo factor TOTP obligatorio para `PA`/`AO`? | E3, E5 |
| P5 | ~~Retención y duración~~ **Resuelta (2026-10-24), parámetros implementados en #88** (`CODIGO_TTL_MIN=10`, `SESION_TTL_HORAS=12` deslizante, `SESION_MAX_DIAS=7`, `SESION_PURGA_DIAS=30`; `src/identidad/repo.py`: `renovar_sesion`, `purgar_sesiones_vencidas`; `python -m src.identidad purgar`). El login que los usa llega en E2-E3. | E1-E3 |
| P6 | ~~¿Qué se audita?~~ **Resuelta (2026-10-24):** login y cierre de sesión, códigos fallidos o vencidos, cambios de estado (ya van a `oportunidad_evento`), altas/bajas de usuarios y membresías, cambios de rol, y los **404 por `cliente_id` fuera de alcance**. **No** se audita cada vista consultada; si hiciera falta, se enciende por rol sin cambiar el esquema. **Implementada en #88:** `src.identidad.politica.ACCIONES_AUDITADAS` + `repo.registrar_auditoria` (rechaza acciones fuera de la lista); los escritores se conectan en E3-E5. | E4 |
| P7 | En DEV con `NOTIFICATION_OVERRIDE_TO`: ¿los códigos van al override (el operador entra como cualquiera) o el correo de acceso queda exento del override? | E3 |
| P8 | ~~Conflicto mercado vs. persona~~ **Resuelta (2026-10-24), implementada en #88:** el resultado final de COMPRASAL hace avanzar una fila `MANUAL` en etapa intermedia (NUEVO/EVALUANDO/OFERTADO); si la persona ya dejó un estado final (GANADO/PERDIDO/DESCARTADO) distinto del de COMPRASAL, **no se pisa** y se muestra como conflicto (`listar_conflictos_oportunidad_v2`). | E6 |
| P9 | ~~Dominio público del portal~~ **Resuelta (2026-10-23): el mismo `dev.amalia.multicomputos.com` del feedback.** `Origin` se verifica contra `FEEDBACK_BASE_URL`; publicar `/portal*` en Caddy queda para E7. | E7 |
