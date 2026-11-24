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

## Estado (2026-11-23)
El portal es **SICOP** y se lee **sin cuenta**: solo información pública, con un navegador real anónimo (la API
pública está detrás de un WAF que rechaza a los clientes que se identifican como automatizados y al Chromium
*headless*). El contrato, la medición del WAF, las modalidades y los pliegos están en
[`arquitectura/sicop-api.md`](../arquitectura/sicop-api.md); el diseño del núcleo portal-agnóstico, en
[`arquitectura/portales.md`](../arquitectura/portales.md).

| Paso | Estado |
|---|---|
| Registro de portales y cliente `multicomputos_cr` | hecho |
| Extractor de SICOP (`src/scraper/sicop.py`, `navegador_anonimo.py`) | hecho; `integrado=True` |
| Pliegos y competibilidad por modalidad (`src/drilldown/localizador_sicop.py`) | hecho |
| Moneda original con equivalente «≈», textos «cartel», PX en «No competibles» | hecho |
| Esquema v2 (moneda, modalidad) y semilla `sicop_cr` en la ADB de DEV | aplicado en DEV el 2026-11-23 |
| Mercado: `adjudicacion_partida`, GANADO por las dos cédulas (`3102813498`, `9000018410`) | tabla creada y validada en DEV |
| Timer semanal de mercado con SICOP | hecho, apagado (`features.enable_mercado_sicop`) |
| Chromium con pantalla virtual (Xvfb) para pasar el WAF | medido en la VM de DEV; en la imagen |
| `multicomputos_cr` activo en DEV con `override_to`; PROD lo omite (`portales_deshabilitados: sicop_cr`) | este PR |

## Lo que falta
1. **Desplegar DEV** (`oci/deploy.sh multicomputos_dev`) para que la VM reciba `clients/`, `SICOP_HEADLESS=false`
   y la imagen con Xvfb; revisar el primer correo `[VALIDACIÓN]` y el portal.
2. **`fx_tasa`**: sin tipo de cambio no hay equivalente «≈ USD» (hoy la tabla está vacía).
3. **Precisión de la clasificación** (fase 5): muestra etiquetada de ≥150 concursos y revisión del perfil
   `multicomputos_cr`, que aún copia el catálogo de El Salvador.
4. **Destinatarios** y moneda de reporte de Costa Rica; **promover a PROD** quitando `portales_deshabilitados`.
5. **Portal web (E4)**: selector de país según el alcance (hoy `PORTAL_CLIENTE_ID` es fijo).
6. Relleno de `proceso.moneda` de los procesos históricos de COMPRASAL (hoy NULL) y vistas del portal con datos de CR.

## Decisiones de Emilio
- **Acceso (2026-11-23):** navegador real anónimo, **sin falsificar el User-Agent**; el *headless* no pasa el WAF,
  con pantalla virtual sí. No se gestiona autorización con Hacienda.
- **Proveedores en SICOP:** ambas cédulas (`3102813498`, `9000018410`) son de Multicómputos.
- **Correo de Costa Rica:** «cartel»; montos en moneda original con equivalente «≈»; PX en «No competibles».
- **Infraestructura:** la misma VM; sin credenciales de portal (no hay login).
- **Marcas y categorías:** las mismas ([`clients/multicomputos_cr.yaml`](../../clients/multicomputos_cr.yaml) copia el
  catálogo de El Salvador).
