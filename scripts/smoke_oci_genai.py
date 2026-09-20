"""scripts.smoke_oci_genai — prueba de humo BLOQUEANTE contra OCI Generative AI.

**Por qué existe.** Antes de escribir una sola línea del adaptador de OCI hay
cinco cosas que ninguna documentación responde con certeza y que, si salen mal,
cambian el diseño o lo cancelan. Este script las responde midiendo, no leyendo:

1. **Auth + IAM + región.** Un chat trivial. Ya encontró el primer modo de falla:
   OCI GenAI **no sirve inferencia en `us-ashburn-1`**, que es donde viven las
   VMs; sí en `us-chicago-1`, a la que `multicomputos` está suscrito. El tenant
   `emiliogonzalez` solo tiene Ashburn, así que no puede usar el servicio.
2. **Tope real de `max_tokens`.** La documentación dice ~4.000 para on-demand y
   no aclara si aplica a todos los modelos. `analysis` pide 16.000 y `engine`
   16.384: si el tope es real, el lote del clasificador hay que partirlo.
3. **Imágenes base64 por data-URI.** ⚠️ **La pregunta que decide el proyecto.**
   El 66% del corpus es escaneado y se manda al triaje como JPEG. No hay OCR en
   el repo (PyMuPDF sin Tesseract no lee escaneados). Si el modelo no acepta
   data-URI, el drill-down no es portable a OCI.
4. **RSS de `import oci`.** La VM de producción corre con `mem_limit: 800m` y
   Chromium al lado. Se mide en un subproceso para no contaminar al padre.
5. **Calibración carácter↔token.** `quota.py` razona en USD/MTok y OCI cobra por
   transacción (caracteres). La conversión de `quota` es una SUPOSICIÓN; esto la
   reemplaza por una medición.

Uso:
    python -m scripts.smoke_oci_genai --perfil MULTICOMPUTOS
    python -m scripts.smoke_oci_genai --modelo meta.llama-4-maverick-17b-128e-instruct-fp8

Imprime un informe legible por stderr y **JSON por stdout**, para poder hacer
`python -m scripts.smoke_oci_genai > resultado.json`.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# ── Valores por defecto ──────────────────────────────────────────────────────

# us-chicago-1 y NO us-ashburn-1: ver el punto 1 del docstring. Es el hallazgo
# más caro de este script y el que hay que llevar a config.py como default.
REGION_GENAI = "us-chicago-1"

# AMALIA_DEV. El compartment importa para la facturación y para la política IAM.
COMPARTMENT_AMALIA_DEV = (
    "ocid1.compartment.oc1..aaaaaaaanhp32bzkqu4u2g626rtlsbtczsv65cbf6bkztpvgawxmwdco32pa"
)

MODELO_DEFAULT = "meta.llama-4-scout-17b-16e-instruct"

# El tope que la documentación atribuye a on-demand. Es la hipótesis a falsar.
TOPE_DOCUMENTADO = 4000


def log(msg: str, *args: object) -> None:
    """Informe humano por stderr; stdout queda limpio para el JSON."""
    print(msg % args if args else msg, file=sys.stderr, flush=True)


# ── Cliente ──────────────────────────────────────────────────────────────────


def _cliente(perfil: str, region: str):
    import oci

    cfg = oci.config.from_file(profile_name=perfil)
    cfg["region"] = region
    # (connect, read): el análisis es la llamada larga del pipeline.
    return oci.generative_ai_inference.GenerativeAiInferenceClient(cfg, timeout=(15, 600))


def _chat(cliente, compartment: str, modelo: str, contenido: list, max_tokens: int):
    """Una llamada GENERIC. `contenido` son objetos ChatContent ya construidos."""
    from oci.generative_ai_inference.models import (
        ChatDetails,
        GenericChatRequest,
        OnDemandServingMode,
        UserMessage,
    )

    peticion = GenericChatRequest(
        api_format=GenericChatRequest.API_FORMAT_GENERIC,
        messages=[UserMessage(role="USER", content=contenido)],
        max_tokens=max_tokens,
    )
    detalles = ChatDetails(
        compartment_id=compartment,
        serving_mode=OnDemandServingMode(serving_type="ON_DEMAND", model_id=modelo),
        chat_request=peticion,
    )
    t0 = time.monotonic()
    resp = cliente.chat(detalles)
    return resp.data.chat_response, time.monotonic() - t0


def _texto_de(respuesta) -> str:
    partes = []
    for choice in respuesta.choices or []:
        for bloque in (getattr(choice.message, "content", None) or []):
            texto = getattr(bloque, "text", None)
            if texto:
                partes.append(texto)
    return "".join(partes)


def _uso_de(respuesta) -> dict:
    """⚠️ OCI usa prompt_tokens/completion_tokens, NO input/output_tokens."""
    u = getattr(respuesta, "usage", None)
    if u is None:
        return {}
    return {
        "prompt_tokens": getattr(u, "prompt_tokens", None),
        "completion_tokens": getattr(u, "completion_tokens", None),
        "total_tokens": getattr(u, "total_tokens", None),
    }


def _finish_reason(respuesta) -> str | None:
    for choice in respuesta.choices or []:
        fr = getattr(choice, "finish_reason", None)
        if fr:
            return str(fr)
    return None


# ── 1. Auth + IAM + región ───────────────────────────────────────────────────


def prueba_auth(cliente, compartment: str, modelo: str) -> dict:
    from oci.generative_ai_inference.models import TextContent

    log("  1/5 auth+IAM+región ...")
    try:
        r, seg = _chat(cliente, compartment, modelo,
                       [TextContent(text="Responde exactamente: OK")], 20)
    except Exception as exc:  # noqa: BLE001 — cualquier fallo acá aborta el resto
        log("      X %s", exc)
        return {"ok": False, "error": str(exc)[:300]}
    log("      OK en %.1fs — %r", seg, _texto_de(r)[:40])
    return {"ok": True, "segundos": round(seg, 2), "texto": _texto_de(r)[:120],
            "uso": _uso_de(r)}


# ── 2. Tope real de max_tokens ───────────────────────────────────────────────


def prueba_tope_salida(cliente, compartment: str, modelo: str) -> dict:
    """Pide MUCHO más que el tope documentado y mira dónde corta de verdad."""
    from oci.generative_ai_inference.models import TextContent

    log("  2/5 tope de max_tokens (pidiendo 16000) ...")
    prompt = ("Escribí un inventario numerado de 400 riesgos posibles en una "
              "licitación pública de infraestructura TI. Uno por línea, "
              "detallado. No te detengas antes del 400.")
    try:
        r, seg = _chat(cliente, compartment, modelo, [TextContent(text=prompt)], 16000)
    except Exception as exc:  # noqa: BLE001
        # Un 400 acá significa que el tope se valida en el REQUEST, no al generar.
        log("      X rechazado al enviar: %s", str(exc)[:150])
        return {"ok": False, "tope_es_de_request": True, "error": str(exc)[:300]}

    uso = _uso_de(r)
    salida = uso.get("completion_tokens")
    fr = _finish_reason(r)
    log("      aceptó max_tokens=16000 · generó %s tokens · finish_reason=%s", salida, fr)
    veredicto = "sin_tope_aparente"
    if salida and salida <= TOPE_DOCUMENTADO + 50:
        veredicto = f"cortado_cerca_de_{TOPE_DOCUMENTADO}"
    return {"ok": True, "tope_es_de_request": False, "tokens_generados": salida,
            "finish_reason": fr, "segundos": round(seg, 2), "veredicto": veredicto}


# ── 3. Imágenes base64 por data-URI — la pregunta que decide el proyecto ─────


def _pdf_sintetico(destino: Path) -> str:
    """Un PDF de una página con una frase reconocible, para pedir transcripción."""
    import fitz

    frase = "OBJETO DE LA CONTRATACION: suministro de firewalls perimetrales"
    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    page.insert_text((60, 120), frase, fontsize=16)
    doc.save(str(destino))
    doc.close()
    return frase


def prueba_imagen(cliente, compartment: str, modelo: str, tmp: Path) -> dict:
    from oci.generative_ai_inference.models import ImageContent, ImageUrl, TextContent

    from src.drilldown.pdf import rasterizar_paginas

    log("  3/5 imagen base64 por data-URI (LA PREGUNTA QUE DECIDE) ...")
    pdf = tmp / "smoke.pdf"
    frase = _pdf_sintetico(pdf)
    paginas = rasterizar_paginas(str(pdf), 1)
    if not paginas:
        return {"ok": False, "error": "rasterizar_paginas devolvió vacío"}

    b64 = paginas[0]
    contenido = [
        TextContent(text="Transcribí literalmente el texto que aparece en la imagen."),
        ImageContent(image_url=ImageUrl(url=f"data:image/jpeg;base64,{b64}")),
    ]
    try:
        r, seg = _chat(cliente, compartment, modelo, contenido, 300)
    except Exception as exc:  # noqa: BLE001
        log("      X el modelo NO acepta data-URI: %s", str(exc)[:200])
        return {"ok": False, "acepta_data_uri": False, "error": str(exc)[:300]}

    texto = _texto_de(r)
    # "FIREWALLS" es la palabra discriminante: si aparece, leyó la imagen de verdad.
    leyo = "firewall" in texto.lower()
    log("      %s — %r", "LEYÓ LA IMAGEN" if leyo else "respondió pero NO transcribió",
        texto[:80].replace("\n", " "))
    return {"ok": True, "acepta_data_uri": True, "transcribio": leyo,
            "kb_imagen": round(len(b64) * 3 / 4 / 1024, 1),
            "frase_esperada": frase, "texto": texto[:300], "uso": _uso_de(r),
            "segundos": round(seg, 2)}


# ── 4. RSS de `import oci` ───────────────────────────────────────────────────


def prueba_rss() -> dict:
    """En un subproceso: el padre ya importó oci y contaminaría la medición."""
    log("  4/5 RSS de `import oci` (gate de la VM de 800m) ...")
    codigo = (
        "import resource,sys;"
        "a=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss;"
        "import oci;"
        "b=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss;"
        "print(a,b)"
    )
    try:
        out = subprocess.run([sys.executable, "-c", codigo],
                             capture_output=True, text=True, timeout=120)
        antes, despues = (int(x) for x in out.stdout.split())
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)[:200]}
    # macOS reporta ru_maxrss en bytes; Linux en kilobytes.
    div = 1024 * 1024 if sys.platform == "darwin" else 1024
    mb = round((despues - antes) / div, 1)
    log("      +%s MB", mb)
    return {"ok": True, "delta_mb": mb, "total_mb": round(despues / div, 1)}


# ── 5. Calibración carácter ↔ token ──────────────────────────────────────────


def prueba_calibracion(cliente, compartment: str, modelo: str) -> dict:
    """Mide caracteres enviados contra prompt_tokens reportados."""
    from oci.generative_ai_inference.models import TextContent

    log("  5/5 calibración carácter↔token ...")
    texto = ("El presente proceso de contratacion tiene por objeto el suministro, "
             "instalacion y puesta en marcha de equipamiento de infraestructura "
             "tecnologica para la institucion contratante. ") * 20
    try:
        r, _ = _chat(cliente, compartment, modelo, [TextContent(text=texto)], 10)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)[:200]}
    uso = _uso_de(r)
    pt = uso.get("prompt_tokens") or 0
    ratio = round(len(texto) / pt, 2) if pt else None
    log("      %s caracteres -> %s prompt_tokens (%.2f chars/token)",
        len(texto), pt, ratio or 0)
    return {"ok": True, "caracteres": len(texto), "prompt_tokens": pt,
            "chars_por_token": ratio, "uso": uso}


# ── Orquestación ─────────────────────────────────────────────────────────────


def main() -> int:
    p = argparse.ArgumentParser(description="Prueba de humo de OCI Generative AI.")
    p.add_argument("--perfil", default=os.getenv("OCI_CLI_PROFILE", "MULTICOMPUTOS"))
    p.add_argument("--region", default=REGION_GENAI)
    p.add_argument("--compartment-id", default=COMPARTMENT_AMALIA_DEV)
    p.add_argument("--modelo", default=MODELO_DEFAULT)
    args = p.parse_args()

    import tempfile

    log("OCI Generative AI — prueba de humo")
    log("  perfil=%s región=%s modelo=%s", args.perfil, args.region, args.modelo)
    log("")

    informe: dict = {"perfil": args.perfil, "region": args.region,
                     "modelo": args.modelo, "pruebas": {}}

    try:
        cliente = _cliente(args.perfil, args.region)
    except Exception as exc:  # noqa: BLE001
        informe["error_fatal"] = f"no se pudo construir el cliente: {exc}"
        json.dump(informe, sys.stdout, indent=2, ensure_ascii=False)
        return 2

    informe["pruebas"]["auth"] = prueba_auth(cliente, args.compartment_id, args.modelo)
    if not informe["pruebas"]["auth"]["ok"]:
        log("\n  El chat básico falló: el resto de las pruebas no aportaría nada.")
        json.dump(informe, sys.stdout, indent=2, ensure_ascii=False)
        return 1

    informe["pruebas"]["tope_salida"] = prueba_tope_salida(
        cliente, args.compartment_id, args.modelo)
    with tempfile.TemporaryDirectory() as td:
        informe["pruebas"]["imagen"] = prueba_imagen(
            cliente, args.compartment_id, args.modelo, Path(td))
    informe["pruebas"]["rss_sdk"] = prueba_rss()
    informe["pruebas"]["calibracion"] = prueba_calibracion(
        cliente, args.compartment_id, args.modelo)

    log("")
    img = informe["pruebas"]["imagen"]
    if img.get("transcribio"):
        log("  VEREDICTO: el drill-down es portable — el modelo lee imágenes base64.")
    elif img.get("acepta_data_uri"):
        log("  VEREDICTO: acepta la imagen pero no transcribió. Revisar el prompt.")
    else:
        log("  VEREDICTO: el triaje de escaneados NO es viable con este modelo.")

    json.dump(informe, sys.stdout, indent=2, ensure_ascii=False)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
