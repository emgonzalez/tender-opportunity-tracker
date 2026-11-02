# Precisión por marca y aprendizaje continuo

Plan vivo para salir de sombra. Línea base: `develop`, PR #163; la configuración
versionada no acredita por sí sola que una VM haya ejecutado esas capas.
El historial de entregas vive en [CHANGELOG](../../CHANGELOG.md).

## Motivo y alcance

El diagnóstico que origina el trabajo es el proceso público del BCR 99-2027-P0016,
«Renovación y soporte de licencias COGNOS»: IBM Cognos, contratación directa,
proveedor único por marca y carta del fabricante. El LLM recomendó Oracle como marca
«comodín» con match 86; el análisis del pliego detectó proveedor único pero no corrigió
la marca. La línea base aportada para este plan es una precisión inicial aproximada
del 35 % sobre 23 procesos con marca Oracle en DEV y un 57 % de procesos marcados
«relevantes». Son cifras diagnósticas del encargo, no una evaluación reproducida aquí
ni métricas actuales de producción. El diseño de evaluación general está en
[evaluación de precisión del triaje](evaluacion-precision-triaje.md).

## Principios y límites

- Lo determinista manda; lo aprendido solo calibra. Scores y vecinos no bastan para excluir.
- Toda afirmación del LLM que autorice una decisión debe llevar una cita validada por
  código contra la fuente. La procedencia y las negaciones importan: una transcripción
  visual sin corroboración independiente no autoriza una exclusión.
- `fuera` exige evidencia determinista o cita validada. Lo dudoso queda en «Por confirmar»;
  una entrada antigua sin evidencia tampoco se oculta por un score bajo.
- Todo producto Oracle cuenta: NetSuite, MySQL, Java, WebLogic, Solaris/Sun,
  PeopleSoft y las demás equivalencias del diccionario. «Migración fuera de Oracle»
  queda por confirmar, con motivo.
- Cada capa es optativa y reversible. La corrida diaria debe continuar si fallan la
  ADB o las dependencias opcionales del LLM; no se aprende de un error de transporte
  como si fuera una etiqueta comercial.

El veredicto actual también devuelve `fuera` cuando no existe entrada ni señal de
la marca. Esa regla determinista de ausencia está en la función pura; al evaluar
recall hay que auditar especialmente los procesos que nunca recibieron candidata.
Los principios anteriores son metas de aceptación, no una garantía de cobertura
universal de todas las respuestas del sistema antiguo.

## Capas y estado

Los flags y sus valores admitidos tienen su hogar en [Config](../../src/config.py);
la traducción de configuración por tenant está en [04-app.sh](../../oci/lib/04-app.sh).
Estado DEV declarado en [tenants.yaml](../../oci/tenants.yaml):

| Capa / flag | Función y fuente autoritativa | Estado |
|---|---|---|
| C0 · `INTEL_C0` | Contrato estructurado, temperatura/semilla, esquema y trazabilidad: [contrato](../../src/llm/contrato.py), [pipeline](../../src/drilldown/pipeline.py) | Implementada; sombra en DEV |
| `INTEL_CLASIF` | Candidatas, diccionario, citas y calibración por vecinos: [marcas](../../src/classifier/marcas.py), [engine](../../src/classifier/engine.py), [vecinos](../../src/aprendizaje/vecinos.py) | Implementada; sombra en DEV |
| C1 · `INTEL_C1` | Contexto compacto de proceso y antecedentes para triaje: [contexto](../../src/drilldown/contexto.py), [pipeline](../../src/drilldown/pipeline.py) | Implementada; apagada |
| C2 · `INTEL_C2` | Selección y transcripción dirigida de páginas, con presupuesto: [lectura](../../src/drilldown/lectura.py), [páginas](../../src/aprendizaje/paginas.py) | Implementada; sombra en DEV |
| C3 · `INTEL_C3` | Verificación selectiva con cita, segunda fuente para negativas visuales e indicios de inyección: [verificación](../../src/drilldown/verificacion.py), [citas](../../src/drilldown/citas.py), [inyección](../../src/drilldown/inyeccion.py), [pipeline](../../src/drilldown/pipeline.py) | Implementada y conectada; apagada |
| C4 · `INTEL_C4` | Caché por huella, capa, versión, cliente y TTL: [cache](../../src/aprendizaje/cache.py) | Componente implementado; apagada, sin consumidor diario del flag |
| C5 · `INTEL_C5` | Rescate semántico acotado hacia revisión; nunca confirma una marca: [rescate](../../src/classifier/rescate.py) | Implementada; apagada |
| C6 · `INTEL_C6` | Features, logística L2 y Platt; exploración con propensiones: [features](../../src/aprendizaje/features.py), [modelo](../../src/aprendizaje/modelo.py), [exploración](../../src/aprendizaje/exploracion.py) | Componentes puros implementados; apagada, sin activación diaria por flag |
| C7 · `INTEL_C7` | Señales léxicas y concentración de proveedores: [señales](../../src/mercado/senales.py) | Componente manual implementado; apagada, sin activación diaria por flag |
| C8 · `INTEL_C8` | Evaluación offline y puertas de promoción: [evaluación](../../src/aprendizaje/evaluacion.py), [evaluar_marca](../../scripts/evaluar_marca.py) | Herramientas implementadas; apagada, sin ejecución diaria por flag |
| `MARCA_VEREDICTO` | Reconciliación y consumo compartido por correo y portal | Implementada; sombra en DEV |

Cambiar un flag de un componente sin consumidor no lo conecta automáticamente.
La revectorización E5 es una operación separada: [revectorizar](../../src/memoria/revectorizar.py)
y [RAG](../../src/memoria/rag.py). `EMBEDDING_PREFIJOS=off|on` es otra perilla,
apagada por defecto; no usa los tres modos de inteligencia.

## Un veredicto para correo y portal

La función pura [veredicto_marca](../../src/notifier/veredicto_marca.py) devuelve
`confirmada`, `por_confirmar`, `fuera` o `no_competible`, con motivo, evidencia,
fuente y competibilidad. Su orden de precedencia es autoritativo en el código:
exclusiones deterministas/verificadas, bloqueo comercial, migración, confirmaciones
y dudas. Una calibración activa puede ajustar umbrales; nunca elimina las reglas.

La [reconciliación](../../src/drilldown/reconciliar.py) valida de nuevo las citas del
análisis y persiste el veredicto en las entradas existentes de marcas. No añade
marcas ausentes que alterarían destinatarios en sombra. El correo consume
[por_marca](../../src/notifier/por_marca.py) y [agrupamiento](../../src/notifier/agrupamiento.py);
el portal aplica el filtro SQL de [relevancia](../../src/memoria/relevancia.py).
Con modo activo conservan confirmadas, por confirmar y no competibles, separando
estas últimas; excluyen `fuera`. Entradas antiguas sin veredicto conservan el trato
compatible. En sombra se conserva la selección anterior y se registran diferencias.

## Aprender sin convertir ruido en verdad

La administración [learning](../../src/web/admin_aprendizaje.py), bajo
`/portal/admin/learning`, permite revisar etiquetas, términos, calibraciones y
exploración con alcance por cliente y escrituras auditadas. El esquema exacto y las
restricciones de `ejemplo_marca`, `marca_termino`, `marca_calibracion`,
`decision_capa`, `pagina_pliego` y `senal_mercado` viven exclusivamente en
[db/schema_v2.sql, §7](../../db/schema_v2.sql). Su presencia en el repositorio no
prueba que ese DDL esté aplicado en una ADB.

El ciclo optativo se invoca así (no se ejecutó en esta tarea):

```bash
python -m src.aprendizaje ciclo --cliente multicomputos_sv --segundos 300
```

Por defecto es dry-run: no entrena ni conserva escrituras. `--aplicar` habilita
cosecha, minado, promoción y reporte de deriva dentro de una transacción. Hay bloqueo
por cliente, presupuesto temporal y rollback completo ante fallo. El entrenamiento
requiere un entrenador inyectado: la CLI actual no lo proporciona y lo omite incluso
con `--aplicar`. No existe timer semanal del ciclo.

Las guardas están en [cosecha](../../src/aprendizaje/cosecha.py),
[minado](../../src/aprendizaje/minado.py), [promoción](../../src/aprendizaje/promocion.py)
y [deriva](../../src/aprendizaje/deriva.py): protegen etiquetas humanas y reservas de
prueba/críticos, exigen corroboración de señales débiles y validación temporal de
nuevos términos. Una promoción exige cobertura completa y comparable, críticos
correctos y ausencia de regresión frente a la activa. Las alertas externas no son
transaccionales. `borrador-oracle` propone ejemplos para revisión; un borrador no es
una etiqueta de referencia.

## Medición y salida de sombra

Metas de aceptación, medidas sobre etiquetas humanas y reservas separadas:

| Medida | Meta |
|---|---|
| Precisión de `confirmada` | ≥ 0,95; límite inferior Wilson al 95 % ≥ 0,85 |
| Recall de lo enviado (`confirmada` + `por_confirmar`) | ≥ 0,98 |
| Casos críticos | 0 fallos, con cobertura completa |
| ECE | ≤ 0,05 |
| Precisión de `competible=False` | ≥ 0,95 |

La puerta implementada en [evaluación](../../src/aprendizaje/evaluacion.py) exige
Wilson inferior para Oracle; la precisión de `competible=False` debe evaluarse
aparte, porque esa puerta no la calcula. Reportar tamaño y estratos, faltantes,
cobertura de probabilidades y casos críticos; la ausencia de medición no es éxito.
Para explorar descartados, registrar propensiones y usar IPS solo donde sean positivas.

1. Observar 2–3 semanas en DEV, sin calendario de promoción automático. Comparar
   diariamente la selección antigua y la propuesta, motivos y evidencia, incluyendo
   IBM Cognos, productos Oracle, migraciones y entradas antiguas sin evidencia.
2. Usar «Ver como» para revisar roles restringidos de Oracle y contrastar correo y
   portal. En sombra la interfaz conserva la selección antigua: esa revisión se
   complementa con [scripts/sombra_marca.py](../../scripts/sombra_marca.py), que lee
   el registro diario de diferencias o una foto ADB de solo lectura. La foto ADB
   compara filtros; el JSONL incluye además cambios de grupo.
3. Etiquetar desacuerdos y una muestra de lo excluido; medir las metas anteriores.
   No promover por el mero transcurso de las semanas.
4. Promover una capa conectada cada vez: `off` → `sombra` → `activo`, cambiando su
   `features.intel_cN`, `features.intel_clasif` o `features.marca_veredicto` por tenant
   y desplegando mediante el procedimiento habitual. Repetir comparación y casos
   críticos tras cada cambio. Para C4/C6/C7/C8 falta integrar los consumidores antes
   de que el flag pueda gobernar el flujo diario.
5. Revertir la capa concreta a `sombra` para seguir midiendo con selección anterior,
   o a `off` para omitirla. Una calibración se restaura por versión con auditoría
   según [promoción](../../src/aprendizaje/promocion.py); revertir el modo no deshace
   escrituras históricas ni una migración de embeddings.

## Decisiones pendientes del operador

- Autorizar revectorización: escritores detenidos, dry-run, lotes reanudables;
  completar ambos esquemas antes de activar prefijos conjuntamente en lectores y
  escritores. No mezclar corpus con y sin prefijo.
- Encargar el timer semanal del ciclo en `oci/`, definiendo presupuesto y entrenador.
- Resolver saldo de Anthropic y uso del juez offline; los canarios y evaluadores no
  acreditan una evaluación ejecutada ni financian llamadas.
- Decidir migración a A1 y Tesseract para disponer de OCR independiente. La segunda
  fuente requerida para exclusiones visuales no se sustituye por otra paráfrasis del LLM.

No se requiere un ADR nuevo mientras estas decisiones sigan pendientes.
