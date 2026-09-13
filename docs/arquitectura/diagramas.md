# Diagramas HLD / LLD · AMALIA v2 (fuente Mermaid)

> Reemplaza a `diagramas_hld_lld_amalia.png` (obsoleto: mostraba Ollama local + ChromaDB/Qdrant residentes, ambos eliminados en v2).
> Fuente de verdad editable. Ver `docs/arquitectura/vision-general.md` e `docs/arquitectura/modelo-de-datos.md`.

**Principios v2 codificados en los diagramas:**
- La VM de 1 GB es un **orquestador stateless**: sin LLM local, sin vector DB residente.
- **Única API externa de pago = Anthropic** (Haiku 4.5 triaje/visión · Sonnet análisis).
- **Un solo vector store**: Oracle ADB 23ai (embeddings ONNX *in-database*).
- **Dos etapas efímeras aisladas en memoria**: Chromium y PDF/LLM nunca coexisten.

---

## 1. HLD — Arquitectura de solución (topología)

```mermaid
%%{init: {'theme':'base','themeVariables':{'fontFamily':'ui-monospace, monospace','primaryColor':'#E7EDEE','primaryBorderColor':'#12707D','primaryTextColor':'#0F2E33','lineColor':'#5C6B6E'}}}%%
flowchart TB
  NET([" Internet · COMPRASAL "])

  subgraph EXT[" APIs Externas — ÚNICA dependencia de pago "]
    CLAUDE["Anthropic Claude API<br/><b>Haiku 4.5</b> · triaje / visión<br/><b>Sonnet</b> · análisis profundo"]
  end

  subgraph OCI[" OCI Always Free Region "]
    direction TB
    subgraph VCN[" VCN · Public Subnet "]
      VM["<b>AMD E2.1.Micro VM</b><br/>1 OCPU · 1 GB RAM<br/>ZRAM + earlyoom + swap<br/>Orquestador STATELESS efímero<br/>— sin LLM local —<br/>— sin vector DB residente —"]
    end
    OBJ[("OCI Object Storage<br/>Data Lake temporal<br/>lifecycle: borra > 7d")]
    ADB[("Oracle ADB 23ai<br/>metadatos + estado/cola<br/>AI Vector Search<br/>embeddings ONNX in-DB")]
    VAULT["OCI Vault<br/>secretos · Instance Principal"]
    MAIL["OCI Email Delivery<br/>reportes + alertas"]
  end

  NET <-->|"443"| VM
  VM <-->|"HTTPS 443"| CLAUDE
  VM -->|"stream multipart"| OBJ
  VM <-->|"mTLS 1522"| ADB
  VM -.->|"runtime · tmpfs"| VAULT
  VM --> MAIL

  classDef box fill:#E7EDEE,stroke:#12707D,color:#123039;
  classDef ext fill:#FBE9E6,stroke:#B23A2E,color:#6E1E16,stroke-width:1.5px;
  classDef store fill:#E9EFF0,stroke:#12707D,color:#0F2E33;
  classDef sec fill:#EDE7F3,stroke:#5A3E8C,color:#2E1F52;
  class NET,VM,MAIL box;
  class CLAUDE ext;
  class OBJ,ADB store;
  class VAULT sec;
```

---

## 2. LLD — Flujo de ejecución en dos etapas efímeras aisladas

```mermaid
%%{init: {'theme':'base','themeVariables':{'fontFamily':'ui-monospace, monospace','primaryColor':'#E7EDEE','primaryBorderColor':'#12707D','primaryTextColor':'#0F2E33','lineColor':'#5C6B6E'}}}%%
flowchart TB
  TIMER["Systemd Timer · 07:00<br/>TZ America/El_Salvador"]

  subgraph A[" Etapa A · amalia-scrape  (docker run --rm) "]
    direction TB
    PW["Playwright / Chromium<br/>LOW_MEMORY_MODE"]
    DL["Stream download → Object Storage<br/>Content-Length ≤ 100 MB"]
    STA["estado = DESCARGADO (ADB)"]
    PW --> DL --> STA
  end

  subgraph B[" Etapa B · amalia-analyze  (docker run --rm · SIN Chromium) "]
    direction TB
    Q["Cola durable ADB<br/>SELECT … FOR UPDATE SKIP LOCKED<br/>1 pliego a la vez"]
    PDF["PyMuPDF · fitz.open(path)<br/>mmap lazy (nunca bytes en RAM)"]
    BIF{"¿capa de texto?"}
    REGEX["RegEx vs Diccionario<br/>de Capacidades"]
    VIS["Visión página-a-página<br/>≤ 1568 px · JPEG q80 · libera pixmap"]
    TRI{"Triaje · Haiku 4.5<br/>GO / NO-GO + confianza"}
    TRACKB["[Track B · opcional]<br/>chunks → embeddings in-DB (ONNX)"]
    RAG["RAG SQL VECTOR_DISTANCE<br/>— o — Track A contexto largo"]
    SON["Análisis · Sonnet<br/>Matriz de Riesgos + anexos F9/F11"]
    NOT["estado = NOTIFICADO → Email"]
    DESC["DESCARTADO_TRIAJE"]
    REV["REQUIERE_REVISION_MANUAL"]

    Q --> PDF --> BIF
    BIF -->|"sí · nativo"| REGEX
    BIF -->|"no · escaneado"| VIS
    REGEX --> TRI
    VIS --> TRI
    TRI -->|"NO-GO"| DESC
    TRI -->|"confianza baja"| REV
    TRI -->|"GO"| TRACKB --> RAG --> SON --> NOT
  end

  TIMER --> A
  A -->|"OnSuccess · RAM → baseline"| B

  classDef box fill:#E7EDEE,stroke:#12707D,color:#123039;
  classDef disc fill:#F1E4E2,stroke:#B23A2E,color:#6E1E16,stroke-dasharray:4 3;
  classDef human fill:#FAF1D6,stroke:#9A6B00,color:#4A3400;
  classDef llm fill:#FBE9E6,stroke:#B23A2E,color:#6E1E16;
  class TIMER,PW,DL,STA,Q,PDF,BIF,REGEX,TRACKB,RAG,NOT box;
  class DESC disc;
  class REV human;
  class TRI,VIS,SON llm;
```

> Franja roja = llamadas a Claude (única API de pago). Cada `docker run --rm` arranca en baseline: el pico de Chromium (Etapa A) y el de PDF+LLM (Etapa B) **nunca se suman**.

---

## 3. Máquina de estados del pliego (ciclo de vida · idempotente)

```mermaid
%%{init: {'theme':'base','themeVariables':{'fontFamily':'ui-monospace, monospace','lineColor':'#5C6B6E','primaryColor':'#E7EDEE','primaryBorderColor':'#12707D','primaryTextColor':'#123039','tertiaryColor':'#FCFCFB','tertiaryTextColor':'#123039','labelColor':'#123039'}}}%%
stateDiagram-v2
  classDef default fill:#E7EDEE,stroke:#12707D,color:#123039
  [*] --> DESCUBIERTO
  DESCUBIERTO --> DESCARGADO: descarga OK → Object Storage
  DESCARGADO --> DESCARTADO_TAMANO: > 100 MB
  DESCARGADO --> FORMATO_NATIVO: hay capa de texto
  DESCARGADO --> FORMATO_ESCANEADO: sin texto
  FORMATO_NATIVO --> DESCARTADO_REGEX: sin match RegEx
  FORMATO_NATIVO --> triaje: match
  FORMATO_ESCANEADO --> triaje: visión Haiku
  state triaje <<choice>>
  triaje --> DESCARTADO_TRIAJE: NO-GO
  triaje --> REQUIERE_REVISION_MANUAL: confianza baja
  triaje --> APROBADO: GO
  APROBADO --> VECTORIZADO: chunks + embeddings in-DB
  VECTORIZADO --> ANALIZADO: Sonnet · matriz de riesgos
  ANALIZADO --> NOTIFICADO: Email / Webhook
  NOTIFICADO --> [*]
  DESCARTADO_TAMANO --> [*]
  DESCARTADO_REGEX --> [*]
  DESCARTADO_TRIAJE --> [*]
  REQUIERE_REVISION_MANUAL --> [*]

  note right of NOTIFICADO
    Estados ERROR_* (DESCARGA/PARSEO/LLM/BD)
    reintentables hasta intentos < 5.
    La Etapa B reanuda cualquier estado
    no-terminal vía la cola SKIP LOCKED.
  end note
```

---

*Documento interno de Multicómputos El Salvador. Diagramas fuente en Mermaid — editar aquí, no regenerar PNG a mano.*
