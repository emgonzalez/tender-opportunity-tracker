# Rediseño del reporte de licitaciones · Prompt para Claude Code

## Contexto

Tenemos un sistema que **envía por correo electrónico un reporte diario** con las licitaciones IT detectadas en COMPRASAL para nuestros clientes (vendedores IT). El reporte actual es funcional pero tiene problemas serios de UX/UI: jerarquía confusa, tres scores compitiendo, falta el monto estimado, marcas como párrafo en vez de tags, y se rompe en móvil.

Diseñamos un nuevo formato — **"V1 · Hero de prioridad"** — donde cada oportunidad se renderiza como una **card de detalle completo**, todas con el mismo nivel de información, ordenadas por match score. El mockup React vive en este proyecto como referencia visual; tu tarea es **portar el diseño al renderer real del correo**.

## Tarea

Reemplaza el template HTML del correo de reporte diario por el nuevo diseño descrito abajo. El correo debe:

1. Renderizar correctamente en **Gmail (web + iOS + Android)**, **Outlook 2016+ (Windows)**, **Apple Mail** y **clientes web modernos**.
2. Mantener la lógica de datos existente: misma fuente, mismos campos, mismos enlaces a COMPRASAL.
3. Conservar **título y descripción originales** de cada licitación tal como vienen del scraper — no recortar, no reformatear.
4. Ser responsive: en viewport < 480px las cards apilan sus filas a una columna.

## Archivos a localizar y modificar

Antes de empezar:

- Busca el template del correo actual. Probables ubicaciones: `templates/email/`, `app/mailers/`, `src/emails/`, `resources/views/emails/`, `*.mjml`, `*.hbs`, `*.ejs`, `*.html.erb`. Si usamos un framework específico (Nodemailer + Handlebars, Laravel Mail, Django templates, MJML, React Email, etc.) **respeta esa convención**.
- Localiza dónde se construye el objeto de oportunidad que se pasa al template. Verifica que contenga todos los campos que el nuevo diseño necesita (ver "Modelo de datos" abajo). Si falta algo, propón la migración del modelo / scraper antes de tocar el template.

**No asumas un stack — explora primero, pregunta si dudas.**

## Modelo de datos esperado

```ts
type Report = {
  client: string;              // "Multicómputos El Salvador"
  date: string;                // "Miércoles 29 de Abril, 2026"
  time: string;                // "17:11 SV"
  reviewed: number;            // 78  — total procesos revisados hoy
  detected: number;            // 12  — oportunidades relevantes
  closingThisWeek: number;     // 3   — cierran en ≤ 7 días
  opportunities: Opportunity[];
};

type Opportunity = {
  institution: string;         // "Banco de Desarrollo de El Salvador" (nombre completo, no abreviado)
  code: string;                // "96-2026-P0011"
  title: string;               // Título oficial de la licitación, VERBATIM como aparece en COMPRASAL
  description: string;         // Descripción explicando por qué es relevante para el cliente
  category: string;            // "Ciberseguridad" | "Infraestructura" | "Software"
  method: string;              // "Subasta Inversa" | "Comparación de precios" | "Contratación directa" | ...
  estimate: string;            // "USD 28k – 45k"  (preformateado por el backend)
  closeDate: string;           // "06 May 2026"
  daysLeft: number;            // 7
  match: number;               // 0–100
  brands: Brand[];             // Marcas del portafolio sugeridas
  url: string;                 // Enlace directo al proceso en COMPRASAL
};

type Brand = {
  name: string;                // "Fortinet"
  role: "Recomendada" | "Alternativa";
  note: string;                // "Pruebas de penetración y evaluación perimetral"
};
```

Las oportunidades llegan al template **ya ordenadas por `match` descendente**.

## Estructura del correo

```
┌─────────────────────────────────────────┐
│  MASTHEAD                               │
│  · Eyebrow: "REPORTE DIARIO · COMPRASAL"│
│  · Eyebrow derecha: "MIÉ · 29 Abr 2026" │
│  · Título: "Licitaciones TI"            │
│            "para {client}"              │
│  · Stats (3 columnas):                  │
│       78 procesos revisados             │
│       12 oportunidades relevantes (azul)│
│        3 cierran ≤ 7 días (rojo)        │
├─────────────────────────────────────────┤
│  SECCIÓN HEAD                           │
│  "OPORTUNIDADES RELEVANTES"             │
│  derecha: "N · ordenadas por match"     │
├─────────────────────────────────────────┤
│  CARD por oportunidad (xN), en orden:   │
│  ┌──────────────────────────────────┐  │
│  │ [#01] BANCO DE DESARROLLO…   94  │  │  ← rank · institución · code  /  match
│  │       96-2026-P0011         match│  │
│  │                                  │  │
│  │ Contratación de Servicio de…    │  │  ← título VERBATIM
│  │ Licitación para servicio de…    │  │  ← descripción VERBATIM
│  │ ────────────────────────────────│  │
│  │ CATEGORÍA       MÉTODO          │  │
│  │ [Ciberseguridad] Subasta Inversa│  │
│  │ ESTIMADO        CIERRE          │  │
│  │ USD 28k – 45k   06 May · 7d Urg │  │
│  │ ────────────────────────────────│  │
│  │ MARCAS DEL PORTAFOLIO SUGERIDAS │  │
│  │ ┌ Fortinet   [Recomendada]    ┐│  │
│  │ │ Pruebas de penetración…     ││  │
│  │ ├─────────────────────────────┤│  │
│  │ │ Imperva                     ││  │
│  │ │ Protección de aplicaciones… ││  │
│  │ └─────────────────────────────┘│  │
│  │ ┌──────────────────────────────┐│  │
│  │ │  Abrir en COMPRASAL →        ││  │
│  │ └──────────────────────────────┘│  │
│  └──────────────────────────────────┘  │
│                                         │
│  (Card siguiente, mismo formato…)       │
│                                         │
├─────────────────────────────────────────┤
│  FOOTER                                 │
│  "Enviado automáticamente por Radar…"   │
│  Configurar filtros · Excluir institución│
│  Frecuencia · Dejar de recibir          │
└─────────────────────────────────────────┘
```

## Tokens de diseño

```css
/* Paleta */
--paper:       #F4F1EA   /* fondo de la columna */
--canvas-bg:   #ECE7DC   /* fondo fuera de la columna */
--ink:         #18181A
--ink-2:       #44423E
--ink-3:       #6F6B62
--ink-4:       #9A958A
--rule:        #E0DBD0
--rule-soft:   #EAE5D9
--card:        #FFFFFF
--accent:      #1B3A5C   /* azul corporativo — header card + CTA */
--accent-deep: #0F2742
--warn:        #A1281F   /* rojo urgencia */
--warn-soft:   #FCE6E2
--ok:          #0F7A4A   /* verde match alto */
--ok-soft:     #E8F4EC
--amber:       #7A5C00
--amber-soft:  #FAF1D6

/* Tipografía */
font-family-sans: "IBM Plex Sans", "Helvetica Neue", Arial, sans-serif;
font-family-mono: "IBM Plex Mono", ui-monospace, "SFMono-Regular", Menlo, monospace;
/* Si no es viable cargar Plex en el correo, fallback aceptable:
   sans → Helvetica/Arial; mono → Menlo/Consolas. Mantén el contraste sans vs. mono. */

/* Columna del correo */
ancho: 600px (max-width), centrada, fondo --paper
```

### Reglas de color condicional

**Match score** (color del número grande en cada card):

| Score    | Color    |
|----------|----------|
| ≥ 90     | `#0F7A4A` verde |
| 80–89    | `#7A5C00` ámbar |
| 70–79    | `#8A4A00` naranja |
| < 70     | `#7A1E1E` rojo  |

**Chip de días para cierre** (al lado de la fecha):

| Días     | Fondo / Texto                 | Etiqueta |
|----------|-------------------------------|----------|
| ≤ 5      | bg `#FCE6E2` / fg `#A1281F`   | "Urgente"|
| 6–10     | bg `#FAF1D6` / fg `#7A5C00`   | "Esta semana" |
| > 10     | bg `#ECEFE0` / fg `#3C4A2E`   | "Holgado"|

**Chip de categoría**:

| Categoría        | bg / fg               |
|------------------|-----------------------|
| Ciberseguridad   | `#F1E6F5` / `#5A1F6B` |
| Infraestructura  | `#E1ECF4` / `#1F4A6B` |
| Software         | `#E0EFE8` / `#1F5F4A` |
| Otros            | `#EEEEEE` / `#444444` |

## Especificaciones de cada bloque

### Masthead
- Padding: `28px 22px 24px`
- Fondo: degradado `linear-gradient(180deg, #FFFFFF, #F4F1EA)` — en clientes que no soporten gradiente, dejar plano `#FFFFFF`.
- Eyebrow: 11px, uppercase, letter-spacing 0.12em, color `--ink-3`, monoespaciado
- Título: 30px / 1.08 / 600, "Licitaciones TI" en `--ink`, "para {client}" en `--ink-3` peso 400, separados por `<br>`.
- Stats: 3 columnas iguales, tabla `<table>` con celdas bordeadas (`--rule`), fondo blanco, esquinas redondeadas 8px. Número 26px mono 600 + etiqueta 11px en dos líneas.

### Section head ("OPORTUNIDADES RELEVANTES")
- Margin top 22px, padding lateral 22px
- H2 11px / 600 / uppercase / tracking 0.14em / color `--ink-3`
- Contador derecha 11px mono `--ink-4`

### Card de oportunidad
- Background `#FFFFFF`, border `1px solid --rule`, radius 10px, padding `18px 18px 16px`
- **Franja superior**: 3px de `--accent` (`#1B3A5C`) pegada al borde superior, ancho completo. En Outlook usar una fila de tabla de 3px alto con bg-color en vez de pseudo-elemento.
- **Top row**: flex / table row con `[rank-chip] [institución + code]` a la izquierda y `[match-number]` a la derecha alineado arriba.
  - Rank chip: `#XX` mono 11px, padding `3px 7px`, bg `--paper`, border `--rule`, radius 4px.
  - Institución: 12px / 600 / uppercase / tracking 0.02em / color `--ink-3`, **nombre completo, sin truncar**.
  - Code: 11px mono `--ink-4`, margin-top 2px.
  - Match: número 28px / 600 mono, color según escala (ver tabla arriba). Etiqueta "match" debajo 10px uppercase tracking 0.1em.
- **Título**: 17px / 1.32 / 600, color `--ink`, margin `12px 0 8px`. **VERBATIM como llega del backend, sin reformatear mayúsculas/minúsculas.**
- **Descripción**: 13px / 1.5, color `--ink-2`, margin `0 0 14px`.
- **Data grid (2×2)**: separado arriba y abajo por borde dasheado o sólido `--rule-soft`. Padding vertical 14px. Gap 12px vertical × 24px horizontal.
  - Cada celda: label 10px uppercase tracking 0.1em `--ink-4` + valor 13px `--ink`.
  - Valor de "Cierre" lleva chip de días al lado (color según tabla).
- **Marcas**: label "MARCAS DEL PORTAFOLIO SUGERIDAS" (mismo estilo de label que el grid). Lista vertical, gap 8px entre items.
  - Item normal: bg `#FCFBF7`, border `--rule`, radius 6px, padding `10px 12px`.
  - Item **Recomendada**: bg `#F5FAF6`, border `#BFD9C6`. Pill verde "Recomendada" a la derecha (bg `#E8F4EC` fg `#0F7A4A`, 11px).
  - Nombre marca: 13px / 600 / `--ink`. Nota: 12px / `--ink-3`, margin-top 2px.
- **CTA**: un solo botón a todo el ancho ("Abrir en COMPRASAL →"), bg `--accent`, color blanco, padding `10px 14px`, radius 6px, 13px / 500, margin-top 14px. **`href` debe apuntar a `opportunity.url`**.

> **Nada de "Marcar como visto", "Ver proceso" ni botones secundarios. Un CTA por card.**

### Footer
- Border-top `1px solid --rule`, padding `20px 22px 26px`, font-size 11px, color `--ink-3`, line-height 1.6
- Texto: "Enviado automáticamente por **Radar de Licitaciones**. Los scores son sugerencias basadas en tu portafolio activo y el histórico de adjudicaciones; revisa el proceso completo en COMPRASAL antes de cotizar."
- Línea inferior con enlaces separados por punto: Configurar filtros · Excluir institución · Frecuencia: diario 17:00 · Dejar de recibir. Mono, color `--ink-4`, gap 14px.

### Subject line del correo

```
Licitaciones TI · {detected} oportunidades · {closingThisWeek} cierran esta semana
```

### Preheader (preview text del cliente)

```
Hoy revisamos {reviewed} procesos en COMPRASAL. {detected} encajan con tu portafolio — la más prioritaria cierra en {minDaysLeft} días.
```

## Requisitos email-safe (CRÍTICOS)

El correo HTML **debe** seguir estas restricciones, no importa qué framework uses:

1. **Layout con `<table role="presentation">`**, no flexbox ni grid. Outlook 2016+ en Windows todavía renderiza con motor Word y rompe casi todo lo moderno.
2. **CSS inline en cada elemento** (no `<style>` para el layout). Permitido `<style>` adicional dentro de `<head>` solo para media queries y `:hover` (que muchos clientes ignoran).
3. **Sin position, transform, gradient complejo, flex, grid**. Para la franja de color superior de las cards, usa una `<tr>` con `height="3"` y `bgcolor`.
4. **Imágenes con `width`/`height` explícitos** y `alt` siempre. Si no hace falta imagen, no la pongas.
5. **Botones como tabla anidada o `<a>` con padding inline**, con conditional `<!--[if mso]>` VML para que se vean en Outlook (puedes usar `mso-padding-alt` y bulletproof button pattern de buttons.cm).
6. **Web fonts opcionales**: incluye `@import` de IBM Plex en `<head>` con fallback nativo (`font-family: "IBM Plex Sans", Helvetica, Arial, sans-serif`). No bloquees el render si falla la carga.
7. **Dark mode**: agrega `<meta name="color-scheme" content="light">` y `<meta name="supported-color-schemes" content="light">` para forzar light mode en Apple Mail/iOS (el diseño está pensado en light).
8. **Responsive con media query** `@media only screen and (max-width: 480px)`: stack columnas del data-grid y del stats-strip a una sola columna; reducir paddings laterales a 14px.
9. **Soporte VML para esquinas redondeadas en Outlook**: las cards pueden quedar con esquinas rectas en Outlook — está OK, no perseguir VML para esto.
10. **No usar `<button>` para el CTA — siempre `<a>` con `display:inline-block`** y `mso-padding-alt`.

## Acceptance criteria

- [ ] El correo se ve idéntico al mockup `Reporte Licitaciones.html` en Gmail web (Chrome) y en Gmail iOS.
- [ ] En Outlook 2019 / Outlook.com el layout no se quiebra: las cards mantienen orden vertical, el data-grid no se desborda, los chips se ven.
- [ ] Pruebas con **0, 1, 4, 12 y 30 oportunidades** — el correo sigue siendo escaneable, no hay cards rotas, el placeholder "+ N oportunidades más" aparece cuando se trunca.
- [ ] Pruebas con **descripciones largas (>200 chars) y títulos en MAYÚSCULAS** — no se rompe el ancho de la card, el title hace wrap limpio, no aparecen scrollbars horizontales en móvil.
- [ ] Cuando una oportunidad tiene **una sola marca**, el bloque "Marcas del portafolio" sigue alineado.
- [ ] Subject y preheader correctos.
- [ ] Enlaces de cada card apuntan a `opportunity.url` y los del footer a las URLs de configuración existentes.
- [ ] Lighthouse "email-friendly" / Litmus / Email on Acid pasan sin warnings críticos. Si no tenemos Litmus, valida al menos con **htmltest** y con render manual en Outlook + Gmail + Apple Mail.
- [ ] Tamaño total del HTML inline < 102KB (Gmail trunca a partir de ahí).

## Referencias en este repo

- `Reporte Licitaciones.html` — mockup en el navegador (React + Babel), abre con cualquier servidor estático
- `variants/v1-priority.jsx` — componentes del mockup (estructura JSX)
- `variants/v1-priority.css` — todos los estilos del mockup, con los nombres de clase espejando el JSX
- `email.css` — tokens y estilos compartidos (colores, chips, botones, footer)
- `data.js` — fixture de datos con la misma forma que el modelo real

> El mockup usa **flexbox/grid** porque es para vista web. **Tu output es un correo HTML con tablas inline.** Toma el diseño visual y los tokens del mockup, pero implementa con email-safe HTML.

## Antes de implementar

1. Identifica el stack de mailing (busca dependencias, configuración, templates existentes) y dime qué encontraste.
2. Lista los archivos que vas a tocar.
3. Confirma si el modelo `Opportunity` actual tiene todos los campos requeridos o si hace falta migrar el scraper / el ETL.
4. Pregúntame cualquier ambigüedad antes de escribir código.

Luego implementa, agrega tests de snapshot del HTML generado (al menos uno con 0 oportunidades, uno con 4, uno con descripción larga), y muéstrame el resultado renderizado enviándo las pruebas únicamente al correo electrónico emilio.gonzalez@multicomputos.com
