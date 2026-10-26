# Plan: Multicómputos Costa Rica

Costa Rica es el siguiente país. Este documento separa lo que ya quedó preparado (paso 1, sin acceso
al portal de Costa Rica) de lo que falta (pasos 2-4).

## Punto de partida
El esquema v2 y la identidad (E1/E2 de [`portal-fase3-identidad.md`](portal-fase3-identidad.md)) ya son
multi-país: `portal`, `cliente` y `cliente_portal`; los roles de país van por cliente y los regionales ven
todos los países activos sin tocar sus membresías. Lo que seguía atado a El Salvador era el pipeline:
una sola extracción de COMPRASAL para todos los clientes, y `"comprasal_sv"` escrito fijo en cinco módulos.

## Paso 1 — preparación (hecho en este PR)
| Qué | Dónde |
|---|---|
| Registro de portales: `comprasal_sv` (integrado) y `sicop_cr` (declarado, **sin extractor**), con país, zona horaria, moneda y URL. Única fuente del `portal_id` por defecto | [`src/portales/__init__.py`](../../src/portales/__init__.py) |
| Un cliente declara su portal en el YAML (`client.portal`); sin el campo es COMPRASAL, así que `multicomputos` no cambia | `ClientProfile.portal_id` |
| El pipeline agrupa a los clientes por portal, extrae **una vez por portal integrado** y omite con aviso a los de un portal sin extractor (sin alerta de sesión ni fallo del circuito) | `src/main.py` (`agrupar_por_portal`) |
| Las etapas `--stage scrape\|analyze` aceptan **un solo** portal integrado; con dos fallan con un mensaje explícito | `src/main.py` (`_un_solo_portal`) |
| Memoria y feedback arman el `proceso_gid` con el portal **del cliente**, no con el default | `memoria/service.py`, `feedback/service.py` |
| Las cinco copias de `"comprasal_sv"` importan del registro | `memoria/{repo,rag,vectorizer}.py`, `feedback/repo.py`, `mercado/repo.py` |
| Semilla opcional del portal `sicop_cr` y del cliente `multicomputos_cr` (**inactivo**) | `scripts/semilla_esquema_v2.py --con-costa-rica` |

**Decisiones**
- La zona horaria no se movió al portal: El Salvador y Costa Rica son ambos UTC−6 sin horario de verano, y las
  constantes fijas actuales sirven a los dos. `Portal.zona_horaria` ya existe para cuando un portal difiera.
- `src/mercado/` conserva su default COMPRASAL: sus adjudicaciones son específicas de ese portal.
- El cliente `multicomputos_cr` se siembra **inactivo**: no entra en el alcance de ningún usuario ni en ninguna corrida
  hasta activarlo a propósito.
- `clients/multicomputos_cr.yaml` existe pero **inactivo** y sin destinatarios (ver «Decisiones de Emilio»).

## Lo que falta
2. **Extractor de SICOP** (`src/scraper/`): login, listado y detalle de procesos; depende de cómo se acceda al portal
   (API o scraping autenticado). Al integrarlo: `integrado=True` en el registro y credenciales propias.
3. **Activar `multicomputos_cr`**: destinatarios, `active: true`, activarlo en la semilla (`cliente.activo`) y definir
   moneda de reporte. El YAML ya existe (catálogo copiado de El Salvador).
4. **Credenciales por portal**: hoy `COMPRASAL_*` son variables globales del `.env`; cada portal necesita las suyas
   (`cliente_portal.credencial_ref` ya existe en el esquema).
5. **Portal web (E4)**: selector de país según el alcance; hoy `PORTAL_CLIENTE_ID` es fijo.
6. **Etapas `scrape/analyze` con varios portales** y `src/mercado/` para SICOP, cuando haya extractor.

## Decisiones de Emilio (2026-10-26)
- **Portal: TBD.** Se cree que es SICOP, pero no está confirmado; `sicop_cr` queda como nombre provisional.
- **Credenciales: por gestionar.** Todavía no hay acceso; sin ellas no se puede probar un extractor.
- **Infraestructura: la misma VM.** Las credenciales de cada portal van como variables propias en el `.env`
  (por ejemplo `SICOP_CR_*`; el nombre exacto se fija al integrar el portal, hoy `COMPRASAL_*` son globales).
- **Marcas y categorías: las mismas.** [`clients/multicomputos_cr.yaml`](../../clients/multicomputos_cr.yaml) copia
  el catálogo de El Salvador. Queda **inactivo** y con `email_to` vacío (no se copian los destinatarios de El Salvador)
  hasta tener extractor, credenciales y destinatarios.

## Pendiente de Emilio
- Confirmar el portal y gestionar las credenciales de acceso.
- Destinatarios del reporte de Costa Rica.
