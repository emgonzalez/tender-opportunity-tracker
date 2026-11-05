# Runbook: promover cambios de DEV a PROD

> **Estado:** procedimiento, aún sin ejecutar. Primera promoción pendiente. Cada paso marcado
> **[ORDEN]** exige que Emilio lo pida explícitamente en el chat, paso por paso: la autorización de
> uno no cubre el siguiente.

## Por qué existe
PROD (`multicomputos_prod`, compartment `AMALIA_PROD`) **no se actualiza solo**: no tiene `docker.channel`
en [`oci/tenants.yaml`](../../oci/tenants.yaml), así que solo cambia cuando una persona corre
`oci/deploy.sh multicomputos_prod`. DEV sí se actualiza con cada merge. Una promoción mueve **tres cosas
a la vez** y las tres deben coincidir:

| Capa | Dónde vive | Quién la mueve |
|---|---|---|
| Código | imagen de GHCR `sha-<40 hex>` | `oci/deploy.sh` |
| Esquema de la ADB | `db/schema_v2.sql` (DDL aditivo, idempotente) | aplicador manual, con OK |
| Datos y flags | `cliente`/`cliente_config`, semillas, `oci/tenants.yaml` | scripts de `scripts/` y `src/clientes`; PR para los flags |

Promover solo la imagen rompe la corrida de las 07:00 si el código espera tablas o columnas que la ADB
de PROD no tiene. Por eso el orden es: **medir → respaldar → esquema → datos → flags → imagen → verificar**.

## 0 · Reglas
- DDL sobre una ADB real, cada deploy a PROD y apagar `override_to` son **[ORDEN]**.
- Nunca: copiar el `.env` de DEV a PROD, tocar `oci/state/`, desplegar con el árbol sucio, lanzar replays
  de pago en PROD, activar nada en `activo` en la misma promoción que lo introduce.
- PROD conserva `notification.override_to` (los correos van a quien valida) hasta el paso 10.
- Todo se hace desde `develop` limpio, en el sha que ya corrió en DEV.

## 1 · Preflight de solo lectura
Anota el resultado; sin esto no se decide nada. En **cada** ambiente (ssh con la clave de `oci/state/`):
1. Sha en ejecución: `docker compose -f docker-compose.e2.yml run --rm -T amalia python -m src.main --version`.
2. `systemctl is-active amalia.timer`, y la última corrida en `journalctl -u amalia.service` (¿Traceback?).
3. Flags efectivos del `.env` de la VM (sin imprimir valores secretos).
4. **Diff de esquema DEV↔PROD**: `python -m scripts.preflight_diff_adb --exportar --etiqueta DEV` en la VM de
   DEV y `--etiqueta PROD` en la de PROD; luego `--comparar dev.json prod.json --base DEV --objetivo PROD`
   (código de salida 0 = iguales, 2 = diferencias). Solo lee `user_tables`, `user_tab_columns`,
   `user_constraints`, índices y conteos por `cliente_id`: ni filas, ni DSN, ni credenciales.
5. Del mismo informe: ids de `cliente` (¿sigue `multicomputos` o ya es `multicomputos_sv`?) y si existen
   `cuota_gasto`, `sesion_portal_compras` y las tablas de aprendizaje.

## 2 · Compuerta de entrada (DEV)
Todas deben cumplirse; si una falla, no se promueve.
- El sha a promover (`git rev-parse HEAD` de `develop`) está desplegado en DEV, con CI verde e imagen publicada.
- ≥3 corridas limpias de las 07:00 consecutivas (el contador cuenta desde 2026-11-05).
- Último replay/pilotos sin NO-GO nuevos ni avisos abiertos («mezcla de versiones», citas inválidas sobre meta).
- Anota **el sha a promover** y **el sha actual de PROD** (es el de la vuelta atrás).

## 3 · Respaldo previo
`scripts/copiar_adb.py --exportar` de la ADB de PROD y copia de `oci/state/multicomputos_prod.json`.
Sin un respaldo verificable (conteos por tabla) no se sigue.

## 4 · DDL en la ADB de PROD **[ORDEN, un bloque por vez]**
Aditivo e idempotente. Orden por dependencias; después de cada bloque se verifica contra el diff del paso 1
(existencia, CHECK, conteos) y se registra. Si un bloque falla, **se detiene todo**.
1. `cuota_gasto` (cuota y libro LLM).
2. `sesion_portal_compras`.
3. Memoria de aprendizaje (§7): `ejemplo_marca`, `marca_termino`, `marca_calibracion` y `decision_capa`, con sus índices.
4. CHECK de `decision_capa` con la variante `replay` (bloque PL/SQL que restaura el CHECK previo si falla).
5. `pagina_pliego` (caché de páginas de C2) y `senal_mercado` (señales de mercado C7), con sus índices.

Son las **8 tablas nuevas** de `db/schema_v2.sql` desde el 2026-10-29 (se obtuvo con
`git diff <sha de PROD> develop -- db/schema_v2.sql`; **repetirlo al promover**, porque la lista envejece). Como el
archivo es idempotente también vale aplicarlo entero, pero entonces el paso 1 debe mostrar un diff vacío al final. Son **8 tablas** (los índices IVF de PROD que DEV no tiene son ruido).

Se aplica con `oracledb` dentro del contenedor de la VM (`docker compose run --rm -T`), como se hizo en DEV:
corre el bloque, `COMMIT`, y vuelve a consultar el catálogo.

## 5 · Datos **[ORDEN, dry-run primero y luego `--aplicar`]**
1. `scripts/renombrar_cliente.py --de multicomputos --a multicomputos_sv`, solo si el diff muestra el id viejo.
2. `scripts/semilla_esquema_v2.py`, solo si faltan `portal`/`organizacion`/`cliente`.
3. `scripts/sincronizar_perfiles.py` → `cliente_config` con el perfil base (`clients/base/multicomputos.yaml`).
4. `python -m src.aprendizaje semilla` (términos públicos de marcas).
5. `python -m src.aprendizaje recalcular-veredicto` (dry-run, luego `--aplicar`): rellena el veredicto por marca del
   histórico de PROD. Hay que hacerlo **antes** de pasar `marca_veredicto` a `activo` (paso 6); en `sombra` no hace falta.
6. Re-vectorización E5 (`python -m src.memoria.revectorizar`, dry-run primero): obligatoria **antes** de encender
   `embedding_prefijos` en PROD, con los escritores detenidos y el flag aún apagado; después se enciende el flag. Si no
   se hace, el flag queda apagado.
Verificar: `python -m src.clientes mostrar multicomputos_sv` en PROD == el de DEV (perfil resuelto).

## 6 · Flags en `oci/tenants.yaml` (PR a `develop`)
`oci/` es área de Emilio: PR aparte, con revisión humana. **Decisión del 2026-11-05: PROD lleva los mismos flags que DEV**
(un test, `tests/test_oci_inteligencia.py`, lo exige): `intel_c0`, `intel_c1`, `intel_c2`, `intel_c3`, `intel_c5`,
`intel_clasif`, `intel_filtro_ruido` = `sombra`; `marca_veredicto` = `activo`; `embedding_prefijos` = `on`;
`clientes_fuente`, `estado_fuente` y `cuota_fuente` = `sombra`. Quedan en su default `intel_c4`, `intel_c6`-`c8`,
`llm_temperatura`, `sesion_fuente`, `enable_aprendizaje_cron` y `aprendizaje_aplicar`.
Dos de esos flags dependen de datos y deben quedar listos **entre el despliegue y la primera corrida**:
`marca_veredicto: activo` pide `recalcular-veredicto` (paso 5) y `embedding_prefijos: on` pide la re-vectorización
E5, que se corre con `-e EMBEDDING_PREFIJOS=off` y los escritores detenidos. Si se despliega sin cumplirlo,
la corrida mezcla vectores con y sin prefijo.

## 7 · Despliegue **[ORDEN]**
Desde `develop` limpio y con la etiqueta fijada (nunca una móvil):
`echo y | AMALIA_IMAGE_TAG=sha-<40hex> ./oci/deploy.sh multicomputos_prod`.
Verificar al terminar: el deploy cierra con `✓`, `systemctl is-active amalia.timer`, `/healthz` 200 en
`amalia.multicomputos.com` y que `python -m src.main --version` en la VM da el sha prometido.
`oci/deploy.sh` ahora también corre `aprendizaje_deploy` (`oci/lib/10-aprendizaje.sh`): con el flag apagado **no debe
quedar** `amalia-aprendizaje.timer` en la VM de PROD (`systemctl list-timers | grep aprendizaje` vacío); si aparece,
es un fallo del deploy.

## 8 · Primera corrida de PROD (las 07:00 siguientes, solo lectura)
- Corrida sin Traceback; el correo llega a `override_to`.
- Crecen los conteos de `decision_capa` y del libro LLM; costo del día bajo el tope (`DAILY_BUDGET_USD`).
- Sin aviso «mezcla de versiones»; sombra registrando en C0, C1, C2, C3 y C5.
- Mismos procesos clasificados que DEV el mismo día, con diferencias explicadas.

## 9 · Vuelta atrás
- **Imagen:** `echo y | AMALIA_IMAGE_TAG=sha-<anterior> ./oci/deploy.sh multicomputos_prod`.
- **Flags:** revertir el PR del paso 6 y redesplegar.
- **DDL y datos:** son aditivos; el código viejo los ignora, no se revierten. `renombrar_cliente.py` se revierte
  invirtiendo `--de` y `--a`, o desde el respaldo del paso 3.
- **Abortar si:** Traceback en la corrida, ADB inalcanzable, costo sobre el tope.

## 10 · Cuándo se apaga `override_to`
Decisión aparte de Emilio, tras corridas de PROD validadas. No forma parte de la promoción técnica: es el
momento en que los correos empiezan a llegar a los clientes reales.

## Bitácora de promociones
| Fecha | Sha | Pasos hechos | Resultado |
|---|---|---|---|
| — | — | — | primera promoción pendiente |
