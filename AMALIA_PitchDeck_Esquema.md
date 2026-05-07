# AMALIA
## Agente de Multicómputos para el Análisis de Licitaciones con Inteligencia Artificial

> **Formato:** Esquema ejecutivo para presentación estratégica
> **Audiencia objetivo:** Gerente de Marca Oracle, Directores Comerciales, Alta Gerencia
> **Propósito:** Este documento sirve como entrada estructurada para un generador de presentaciones por IA. Cada sección es independiente y puede convertirse en una diapositiva o bloque visual.

---

## SECCIÓN 1 — RESUMEN EJECUTIVO
### El Qué

**Titular:**
> *AMALIA convierte el portal de compras públicas del gobierno en una ventaja competitiva permanente para Multicómputos.*

**Contexto del problema:**
El Estado de El Salvador publica diariamente decenas de procesos de licitación en COMPRASAL, el portal oficial de compras públicas. Identificar cuáles son relevantes para el portafolio de Multicómputos requiere revisar manualmente cada proceso, interpretar descripciones vagas, detectar categorías incorrectamente asignadas por las instituciones compradoras, y hacerlo todos los días hábiles sin falta.

**La solución:**
AMALIA es un agente de inteligencia artificial que opera de forma completamente autónoma. Se autentica en COMPRASAL, extrae todos los procesos publicados en las últimas 24 horas, los analiza con IA semántica calibrada con el catálogo real de Multicómputos, y entrega un reporte personalizado directamente en el correo del equipo comercial —todo sin intervención humana.

**Estado actual:**
- Operativo en producción: ejecuciones automáticas a las **7:00 AM y 2:00 PM** (hora El Salvador), de lunes a viernes
- Catálogo cubierto: **6 categorías de negocio, más de 40 líneas de producto y servicio**
- Historial analizado para calibración: **46 procesos históricos reales de Multicómputos**
- Infraestructura: contenedor Docker autogestionado, despliegue con un solo comando

---

## SECCIÓN 2 — PROPUESTA DE VALOR
### El Por Qué

**Titular:**
> *De la reactividad a la anticipación: AMALIA garantiza que Multicómputos llegue primero.*

---

### Beneficio 1 — Cobertura Total, Cero Esfuerzo Manual

Sin AMALIA, un analista revisa COMPRASAL manualmente, omite procesos, y llega tarde. Con AMALIA, **cada proceso publicado es analizado**, sin excepción, dos veces al día. El equipo comercial recibe solo lo que importa, ya filtrado y categorizado.

**Métrica clave:** Una ejecución cubre el 100% de los procesos publicados en las últimas 24 horas.

---

### Beneficio 2 — Inteligencia Semántica, No Solo Palabras Clave

Las instituciones del Estado salvadoreño frecuentemente asignan categorías incorrectas a sus procesos. Equipos de cómputo pueden aparecer bajo "Cría de animales" o "Material educativo". Los filtros por keyword tradicionales fallan aquí. AMALIA lee el *contenido* del proceso —no su categoría— y lo compara semánticamente contra el catálogo real de Multicómputos usando **Claude Sonnet 4.6** (Anthropic), el modelo de IA de clase empresarial más avanzado disponible hoy.

**Diferenciador:** El motor distingue entre procesos genuinamente relevantes, dudosos con potencial, e irrelevantes —con nivel de confianza explícito en cada caso.

---

### Beneficio 3 — Memoria Institucional que Mejora con el Tiempo

AMALIA aprende del historial real de participación de Multicómputos. Un módulo de entrenamiento analiza los procesos históricos ganados y perdidos, extrae patrones no obvios, y genera un perfil de inteligencia que refina la clasificación futura. Este perfil ya identifica, por ejemplo, que términos como *"conectividad de sitios públicos"* encubren proyectos de expansión de red complejos, y que *"mantenimiento preventivo"* frecuentemente incluye licenciamiento de software especializado.

**Métrica de costo:** Generar o actualizar el perfil de inteligencia para 200 procesos históricos cuesta menos de **USD 0.05**.

---

### Beneficio 4 — Arquitectura Multi-Cliente, Lista para Escalar

AMALIA no es solo para Multicómputos. La plataforma ya soporta múltiples clientes con un único despliegue: un solo proceso de extracción se comparte entre todos los perfiles activos, y cada cliente recibe su propio reporte personalizado con su marca, catálogo y destinatarios. Agregar un nuevo cliente es crear un archivo de configuración.

**Implicación estratégica:** AMALIA puede ofrecerse como servicio a otras empresas tecnológicas del ecosistema Oracle en El Salvador, generando un ingreso recurrente nuevo para Multicómputos.

---

## SECCIÓN 3 — ARQUITECTURA Y FLUJO DE TRABAJO
### El Cómo

**Titular:**
> *Un agente autónomo de cinco pasos que opera mientras el equipo duerme.*

---

### Paso 1 — Autenticación Automática en COMPRASAL

AMALIA gestiona de forma autónoma el acceso al portal gubernamental, incluyendo el inicio de sesión con autenticación de dos factores (2FA/OTP). Si las credenciales están configuradas con TOTP, el proceso es 100% desatendido. El sistema detecta cuándo la sesión expira y notifica al equipo para renovarla.

---

### Paso 2 — Extracción Inteligente de Procesos

Un navegador web automatizado navega COMPRASAL, extrae todos los procesos publicados en las últimas 24 horas y captura sus atributos: institución compradora, descripción, modalidad de contratación, montos estimados y fechas clave. Esta extracción ocurre una sola vez por ejecución y los datos se reutilizan para todos los clientes activos.

---

### Paso 3 — Clasificación Semántica con IA

Cada proceso es evaluado por **Claude Sonnet 4.6** contra el perfil de catálogo del cliente. El motor considera señales de inclusión (términos que aumentan relevancia), señales de exclusión (términos que descartan el proceso), y el perfil de inteligencia histórica. La salida es un nivel de relevancia (relevante / dudoso / irrelevante) con un puntaje de confianza.

Los umbrales configurables garantizan control editorial: solo los procesos que superen el umbral de relevancia (0.60) y confianza (0.70) se marcan como oportunidades confirmadas.

---

### Paso 4 — Reporte Personalizado por Email

Los resultados se empaquetan en un reporte HTML con la marca del cliente y se envían automáticamente a los destinatarios configurados. El reporte incluye procesos relevantes, procesos dudosos con contexto explicativo, y el conteo total revisado para transparencia. Todos los resultados quedan registrados en un historial auditado por cliente.

---

### Paso 5 — Resiliencia y Monitoreo Incorporados

AMALIA incluye mecanismos de seguridad automáticos: un *circuit breaker* que detecta fallos consecutivos y envía alertas al equipo antes de que el problema escale, y un sistema de *catch-up* que detecta y ejecuta ejecuciones perdidas (por apagón del servidor, por ejemplo) al momento de reiniciarse.

---

**Diagrama conceptual del flujo:**

```
COMPRASAL (portal gubernamental)
         │
         ▼
[1] Autenticación automática (2FA/OTP)
         │
         ▼
[2] Extracción de procesos últimas 24h
         │
    ┌────┴────┐
    │         │
    ▼         ▼
[3] Clasificación IA    [3] Clasificación IA
    Cliente A               Cliente B
    │                       │
    ▼                       ▼
[4] Reporte email       [4] Reporte email
    personalizado A         personalizado B
         │
         ▼
[5] Historial auditado + Circuit breaker
```

---

## SECCIÓN 4 — IMPACTO EN EL DÍA A DÍA
### Lo que Cambia para el Equipo

**Titular:**
> *El equipo comercial llega al trabajo con las oportunidades ya identificadas.*

---

**Antes de AMALIA:**

| Situación | Descripción |
|---|---|
| Revisión manual diaria | Un analista dedica tiempo cada mañana a revisar COMPRASAL |
| Cobertura parcial | Solo se revisan los procesos visibles en las primeras páginas |
| Clasificación subjetiva | La relevancia depende del criterio de quien revisa ese día |
| Sin memoria institucional | Cada semana se empieza desde cero |
| Sin alertas | Las oportunidades se pierden en vacaciones o días de ausencia |

**Después de AMALIA:**

| Situación | Descripción |
|---|---|
| Reporte en bandeja de entrada | A las 7:00 AM y 2:00 PM el equipo recibe un email con los resultados |
| Cobertura del 100% | Ningún proceso publicado pasa sin ser analizado |
| Clasificación calibrada | La IA usa el catálogo real y el historial de Multicómputos como referencia |
| Memoria histórica activa | El sistema mejora con cada ciclo de entrenamiento |
| Alertas proactivas | Si algo falla, el equipo recibe una notificación antes de que escale |

---

**Para el Director Comercial:**
Visibilidad completa sobre el mercado de compras públicas TI, sin depender de que alguien recuerde revisar el portal.

**Para el Gerente de Marca Oracle:**
Cada licitación con componente Oracle (licencias ERP, bases de datos, Java, Cloud) es detectada y categorizada automáticamente, incluso cuando aparece bajo denominaciones genéricas como "adquisición de sistemas informáticos".

**Para el Equipo de Ventas:**
Llegan a cada reunión con contexto: qué institución está comprando, qué necesita, y cuál es la ventana de tiempo para presentar una propuesta.

---

## SECCIÓN 5 — ROADMAP / SIGUIENTES PASOS
### Hacia Dónde Va AMALIA

**Titular:**
> *La base ya está construida. Las siguientes mejoras son incrementales y de alto valor.*

---

### Mejora 1 — Panel de Control Web (Dashboard)

**Qué es:** Una interfaz visual accesible desde el navegador que muestra el historial de licitaciones detectadas, el rendimiento del clasificador, y métricas de cobertura por institución compradora.

**Por qué ahora:** El historial auditado en formato JSONL ya existe y acumula datos por cliente. Visualizarlo es el siguiente paso lógico para convertir AMALIA de una herramienta operativa en un activo estratégico de inteligencia de mercado.

**Impacto:** El gerente comercial puede ver en segundos cuántas oportunidades hubo este mes, cuáles instituciones están comprando más TI, y cuál fue el porcentaje de relevancia detectada.

---

### Mejora 2 — Extracción de Detalles del Proceso (Drill-Down)

**Qué es:** Actualmente AMALIA extrae los metadatos del proceso (título, institución, monto, fecha). La mejora consiste en acceder automáticamente a cada proceso relevante y extraer el pliego de condiciones completo —documentos, especificaciones técnicas, requisitos de habilitación.

**Por qué ahora:** La arquitectura de navegación automatizada ya existe (Playwright + Chromium). Extender el scraper a los detalles del proceso es una extensión natural sin cambios de infraestructura.

**Impacto:** El equipo comercial recibe no solo la alerta, sino el resumen ejecutivo del pliego: qué se pide, qué requisitos hay, y si Multicómputos puede participar sin necesidad de descargar el PDF manualmente.

---

### Mejora 3 — Oferta como Servicio (SaaS Multi-Tenant)

**Qué es:** Convertir AMALIA en un servicio ofrecido a otras empresas tecnológicas —distribuidores, integradores, fabricantes— que también quieran monitorear COMPRASAL con inteligencia de negocio propia.

**Por qué ahora:** La arquitectura multi-cliente ya está implementada. Agregar un nuevo cliente es cuestión de configuración, no de desarrollo. El modelo de negocio es suscripción mensual por cliente activo.

**Impacto estratégico para Multicómputos:** Una nueva línea de ingreso recurrente basada en un activo digital ya construido, posicionando a Multicómputos como empresa de soluciones tecnológicas —no solo distribuidora de hardware.

---

## METADATOS DE LA PRESENTACIÓN

| Campo | Valor |
|---|---|
| Nombre del sistema | AMALIA |
| Nombre técnico del proyecto | tender-opportunity-tracker |
| Empresa | Multicómputos El Salvador |
| Versión del documento | 1.0 |
| Fecha | Abril 2026 |
| Estado del sistema | Operativo en producción |
| Motor de IA (clasificación) | Claude Sonnet 4.6 — Anthropic |
| Motor de IA (entrenamiento) | Claude Haiku 4.5 — Anthropic |
| Portal monitoreado | COMPRASAL (comprasal.gob.sv) |
| Frecuencia de ejecución | 2 veces/día hábil (7:00 AM y 2:00 PM, UTC-6) |
| Categorías de catálogo cubiertas | 6 (hardware, software, redes, ciberseguridad, servicios TI, audiovisual) |

---

*Este documento es confidencial y de uso interno de Multicómputos El Salvador.*
