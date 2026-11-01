"""Vector C6 puro, independiente del nombre comercial de la marca.

Contrato v1: presencias ausentes valen 0; probabilidades y booleanos desconocidos
valen 0.5. El voto firmado y el logprob ausentes valen 0. Ninguna presencia
negativa constituye por sí misma autorización para descartar una oportunidad.
Las fuentes del pliego se reciben explícitamente en ``texto`` o ``fuentes``;
una bandera evidencia_valida nunca sustituye la comprobación de la cita.
"""
from __future__ import annotations

import math
from collections.abc import Mapping

from src.classifier.marcas import contiene, normalizar, validar_evidencia

VERSION_FEATURES = "v1"
NOMBRES_FEATURES = tuple("v1_" + nombre for nombre in (
    "termino_fuerte", "termino_debil", "termino_ajena", "score_knn", "score_llm",
    "mencion_pliego", "marca_exigida", "equivalente_admitido", "afinidad",
    "proveedor_unico_hecho", "contratacion_directa", "voto_verificador", "logprob_triaje",
))


def _get(obj, clave, defecto=None):
    return obj.get(clave, defecto) if isinstance(obj, Mapping) else getattr(obj, clave, defecto)


def _numero(valor, defecto=0.5):
    try:
        numero = float(valor)
    except (TypeError, ValueError):
        return defecto
    return numero if math.isfinite(numero) else defecto


def _prob(valor):
    return min(1.0, max(0.0, _numero(valor)))


def _booleano(valor):
    return float(valor) if isinstance(valor, bool) else 0.5


def extraer_features(proc, marca_id, *, dic, knn, llm, pliego, mercado,
                     verificador, forma) -> dict[str, float]:
    """Extrae v1 sin IO ni estado global; acepta mappings y dataclasses.

    ``dic`` contiene filas marca_id/tipo/termino. ``mercado`` recibe valores de
    senal_mercado ya acotados a institución y marca (afinidad y
    proveedor_unico_hecho). ``forma`` es texto o mapping con forma_contratacion
    y logprob_triaje (logprob natural, recortado a [-20, 0]). ``knn`` es una
    probabilidad o un mapping score_knn. El voto del verificador es [-1, 1]
    y solo participa con cita verificada. Las puntuaciones están en [0, 1].
    """
    marca = normalizar(marca_id)
    fuentes = [_get(proc, k, "") for k in
               ("titulo", "descripcion", "institucion", "tipo_actividad")]
    fuentes = [f for f in fuentes if isinstance(f, str)]
    texto = " ".join(fuentes)
    terminos = [t for t in (dic or []) if isinstance(t, Mapping)
                and t.get("activo", True) in (True, 1)
                and t.get("estado", "activo") in ("activo", "semilla")
                and normalizar(t.get("termino", "")) not in ("", "crystal")]
    propios = [t for t in terminos if normalizar(t.get("marca_id", "")) == marca]
    fuertes = [t for t in propios if t.get("tipo") == "fuerte"]

    def presencia(tipo):
        filas = (t for t in terminos if t.get("tipo") == tipo and
                 ((normalizar(t.get("marca_id", "")) != marca) if tipo == "ajena"
                  else normalizar(t.get("marca_id", "")) == marca))
        return float(any(contiene(texto, t["termino"]) for t in filas))

    fuentes_pliego = _get(pliego, "fuentes", []) or []
    if isinstance(fuentes_pliego, (str, Mapping)):
        fuentes_pliego = [fuentes_pliego] if isinstance(fuentes_pliego, str) else list(fuentes_pliego.values())
    fuentes_pliego = [f for f in fuentes_pliego if isinstance(f, str)]
    texto_pliego = _get(pliego, "texto", "")
    if isinstance(texto_pliego, str):
        fuentes_pliego = [*fuentes_pliego, texto_pliego]

    def cita_valida(fila, fuentes_cita):
        cita = _get(fila, "cita", _get(fila, "evidencia", ""))
        return validar_evidencia(cita, fuentes_cita, fuertes)

    llm_valido = (_get(llm, "evidencia_valida") is True
                  and cita_valida(llm, fuentes + fuentes_pliego))
    menciones = _get(pliego, "marcas_involucradas", []) or []
    mencion = any(normalizar(_get(m, "marca", "")) == marca
                  and cita_valida(m, fuentes_pliego) for m in menciones)
    exigida = _get(pliego, "marca_exigida")
    exige = (normalizar(_get(exigida, "marca", "")) == marca
             and cita_valida(exigida, fuentes_pliego))
    voto_valido = (_get(verificador, "evidencia_valida") is True
                   and cita_valida(verificador, fuentes + fuentes_pliego))
    forma_texto = forma if isinstance(forma, str) else _get(forma, "forma_contratacion", None)
    if forma_texto is None:
        forma_texto = _get(proc, "forma_contratacion", "")
    logprob = _get(forma, "logprob_triaje")
    valores = (
        presencia("fuerte"), presencia("debil"), presencia("ajena"),
        _prob(_get(knn, "score_knn", knn)),
        _prob(_get(llm, "score", 0)) if llm_valido else 0.0,
        float(mencion), float(exige), _booleano(_get(pliego, "equivalente_admitido")),
        _prob(_get(mercado, "afinidad")),
        _prob(_get(mercado, "proveedor_unico_hecho")),
        float(contiene(str(forma_texto or ""), "contratacion directa")),
        min(1.0, max(-1.0, _numero(_get(verificador, "voto"), 0))) if voto_valido else 0.0,
        min(0.0, max(-20.0, _numero(logprob, 0))),
    )
    return dict(zip(NOMBRES_FEATURES, valores))
