"""Verifica mecánicamente que `CLAUDE.md` siga describiendo el repositorio real.

`CLAUDE.md` es el primer archivo que lee cualquier agente y el único mapa del
repo. Cuando deriva, el daño no es cosmético: un agente navega por una estructura
que no existe. La deriva ya ocurrió —el árbol citaba `oci/delete.sh` (es
`destroy.sh`) y no mencionaba `tests/`, `scripts/`, `.github/` ni `pyproject.toml`,
o sea todo el aparato de CI— y se detectó a mano, meses después.

Esta es la mitad de esa auditoría que **no necesita un LLM**: cuatro chequeos
deterministas que corren en milisegundos dentro del `ci.yml` que ya existe.

## Por qué estos cuatro chequeos y no más

Un prototipo previo que auditaba toda ruta citada en prosa produjo **4 falsos
positivos de 5** (entradas de `data/`: artefactos de runtime, ignorados por git y
ausentes en un worktree limpio). Un test que grita en falso se termina ignorando
o borrando, y entonces no protege nada. La regla de diseño acá es **cobertura
menor y cero falsos positivos**:

- Se auditan solo fuentes **estructuradas** —el árbol ASCII, la tabla de "Key
  Files", los enlaces markdown—, nunca rutas sueltas en prosa. Una ruta en prosa
  puede ser relativa a otra carpeta, un glob o un ejemplo.
- El árbol declara sus propias excepciones **en el texto visible**, con el
  marcador `no versionado` en el comentario, en vez de una allowlist escondida
  acá. Quien lee `CLAUDE.md` ve por qué esa entrada no se verifica.
- Los chequeos de "algo del repo falta en el mapa" se apoyan en `git ls-files`,
  no en el filesystem, para no tropezar con respaldos ni con `.venv`.

Lo que deliberadamente **no** se verifica, y por qué, está en `test_documentacion`
más abajo y en `docs/planes/fase8-evaluacion.md` §3.2: las afirmaciones
semánticas (RB-001…RB-008, "el reporte se envía siempre") no se comprueban con un
`grep` — eso es trabajo de LLM y es episódico, no de cada push.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parent.parent
CLAUDE_MD = RAIZ / "CLAUDE.md"

# Marcador que una entrada del árbol pone en su comentario para declararse
# artefacto de runtime. Va en el texto visible de CLAUDE.md a propósito: es
# información para el humano que lee el mapa, no una excepción escondida en el
# test. Sin esto, `data/` sería un falso positivo permanente.
MARCADOR_NO_VERSIONADO = "no versionado"

# `│   ` o `    `, repetido: cada unidad de 4 caracteres es un nivel de anidación.
_ENTRADA = re.compile(r"^(?P<sangria>(?:[│ ] {3})*)(?:├── |└── )(?P<resto>.*)$")

# Enlaces markdown `[texto](destino)`, incluida la forma imagen `![texto](...)`.
_ENLACE_MD = re.compile(r"!?\[[^\]]*\]\(([^)\s]+)\)")


def _bloque_del_arbol() -> str:
    """El bloque cercado que sigue al encabezado `## Project Structure`."""
    texto = CLAUDE_MD.read_text(encoding="utf-8")
    inicio = texto.index("## Project Structure")
    apertura = texto.index("```", inicio)
    cierre = texto.index("```", apertura + 3)
    return texto[apertura + 3 : cierre]


def _rutas_del_arbol() -> list[str]:
    """Reconstruye las rutas del árbol ASCII a partir de la sangría.

    Devuelve rutas relativas a la raíz del repo. Se omiten:

    - las entradas marcadas `no versionado` (artefactos de runtime);
    - los marcadores de posición (`[other-clients].yaml`, globs con `*`), que no
      afirman que exista ninguna ruta concreta.

    Una línea puede condensar varios hermanos separados por `·`
    (p. ej. `db.py · repo.py · chunker.py`); cada uno cuelga del mismo padre.
    """
    rutas: list[str] = []
    pila: dict[int, str] = {}

    for linea in _bloque_del_arbol().splitlines():
        m = _ENTRADA.match(linea)
        if not m:
            continue

        nivel = len(m.group("sangria")) // 4
        resto = m.group("resto")

        nombre, _, comentario = resto.partition("#")
        nombre = nombre.strip()
        if not nombre:
            continue

        # El padre de este nivel es la última carpeta vista un nivel más arriba.
        padre = pila.get(nivel - 1, "") if nivel else ""

        for pieza in (p.strip() for p in nombre.split("·")):
            if not pieza:
                continue
            ruta = f"{padre}{pieza}" if padre else pieza

            # Una carpeta puede tener hijos: queda registrada como padre del
            # nivel siguiente aunque su ruta no se verifique.
            if pieza.endswith("/"):
                pila[nivel] = ruta

            if MARCADOR_NO_VERSIONADO in comentario.lower():
                continue
            if "[" in pieza or "*" in pieza:
                continue

            rutas.append(ruta.rstrip("/"))

    return rutas


def _archivos_versionados() -> list[str]:
    """`git ls-files`, o skip si no hay git (el filesystem no sirve: trae basura).

    Un `Path.iterdir()` sobre la raíz devolvería `data_*_backup/`, `.venv/` y
    cualquier archivo suelto del operador — justo la clase de falso positivo que
    este archivo existe para evitar.
    """
    try:
        salida = subprocess.run(
            ["git", "ls-files"],
            cwd=RAIZ,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError) as exc:  # pragma: no cover
        pytest.skip(f"sin git para enumerar lo versionado: {exc}")

    return [linea for linea in salida.splitlines() if linea]


# ── 1. Toda ruta del árbol existe ────────────────────────────────────────────


def test_el_arbol_no_cita_rutas_inexistentes():
    """Cada ruta del árbol de `CLAUDE.md` tiene que existir en el repo.

    Es el chequeo que habría atrapado `oci/delete.sh` el día que se renombró a
    `destroy.sh`, en vez de meses después.
    """
    rutas = _rutas_del_arbol()
    assert rutas, "el árbol de CLAUDE.md no se pudo parsear: 0 rutas extraídas"

    faltantes = sorted(r for r in rutas if not (RAIZ / r).exists())
    assert not faltantes, (
        "CLAUDE.md → 'Project Structure' cita rutas que no existen:\n  "
        + "\n  ".join(faltantes)
        + "\n\nCorregí el árbol, o —si es un artefacto de runtime— marcá la "
        f"entrada con '{MARCADOR_NO_VERSIONADO}' en su comentario."
    )


# ── 2. Todo módulo de `src/` figura en el árbol ──────────────────────────────


def test_el_arbol_no_omite_modulos_de_src():
    """La dirección inversa: código nuevo que el mapa no menciona.

    Es el modo de falla real observado — las Fases 4 a 8 agregaron módulos
    (`notifier/ops_alert.py`, `memoria/keepalive.py`) invisibles para quien lee
    el mapa. Se excluyen los `__init__.py`: son plomería del paquete, no
    componentes que alguien tenga que encontrar.
    """
    del_arbol = set(_rutas_del_arbol())
    en_disco = {
        str(p.relative_to(RAIZ))
        for p in (RAIZ / "src").rglob("*.py")
        if p.name != "__init__.py" and "__pycache__" not in p.parts
    }

    omitidos = sorted(en_disco - del_arbol)
    assert not omitidos, (
        "hay módulos en src/ que el árbol de CLAUDE.md no menciona:\n  "
        + "\n  ".join(omitidos)
        + "\n\nAgregalos al árbol (se pueden condensar con ' · ' en una línea)."
    )


# ── 3. Toda carpeta versionada de primer nivel figura en el árbol ────────────


def test_el_arbol_no_omite_carpetas_de_primer_nivel():
    """Que no aparezca un área entera del repo sin entrar al mapa.

    `.github/`, `tests/` y `scripts/` existieron durante fases enteras sin
    figurar en `CLAUDE.md`. A nivel de carpeta el chequeo es barato y no tiene
    falsos positivos: sale de `git ls-files`, así que solo mira lo versionado.

    A nivel de *archivo* suelto de la raíz **no** se verifica nada: obligaría a
    documentar scripts de un solo uso (`delete_amalia_multicomputos.sh`,
    `network_diagnostics.sh`, `send_test_email.py`) y a mantener una allowlist
    para distinguirlos de lo que sí importa. Cobertura menor, cero ruido.
    """
    del_arbol = set(_rutas_del_arbol())
    carpetas = {ruta.split("/", 1)[0] for ruta in _archivos_versionados() if "/" in ruta}

    omitidas = sorted(c for c in carpetas if c not in del_arbol)
    assert not omitidas, (
        "hay carpetas versionadas de primer nivel ausentes del árbol de CLAUDE.md:\n  "
        + "\n  ".join(omitidas)
    )


# ── 4. La tabla "Key Files" no cita archivos inexistentes ────────────────────


def test_la_tabla_de_archivos_clave_no_cita_inexistentes():
    """La otra superficie estructurada de `CLAUDE.md` que afirma rutas.

    Se auditan solo las filas de esa tabla —primera columna, entre backticks—
    y no las rutas sueltas en prosa: en prosa una ruta puede ser un glob
    (`clients/*.yaml`), un ejemplo, o relativa a otra carpeta (`scheduler/cron.py`
    en las reglas RB). Ahí es donde el prototipo de auditoría se llenó de falsos
    positivos, así que esa superficie queda deliberadamente fuera.
    """
    texto = CLAUDE_MD.read_text(encoding="utf-8")
    inicio = texto.index("## Key Files (Quick Reference)")
    tabla = texto[inicio : texto.index("\n---", inicio)]

    citadas = re.findall(r"^\|\s*`([^`]+)`\s*\|", tabla, flags=re.MULTILINE)
    assert citadas, "no se pudo parsear la tabla 'Key Files' de CLAUDE.md"

    faltantes = sorted({r for r in citadas if not (RAIZ / r).exists()})
    assert not faltantes, (
        "CLAUDE.md → 'Key Files' cita archivos que no existen:\n  " + "\n  ".join(faltantes)
    )


# ── 5. Los enlaces relativos de la documentación resuelven ───────────────────


def _documentos_markdown() -> list[Path]:
    return [CLAUDE_MD, RAIZ / "README.md", *sorted((RAIZ / "docs").rglob("*.md"))]


@pytest.mark.parametrize(
    "documento", _documentos_markdown(), ids=lambda p: str(p.relative_to(RAIZ))
)
def test_los_enlaces_relativos_resuelven(documento: Path):
    """Todo `[texto](ruta/relativa)` apunta a algo que existe.

    Se ignoran los destinos con esquema (`http:`, `mailto:`) y los anclas puras
    (`#seccion`). Se tolera el sufijo de línea que usa `docs/` para citar código
    (`oci/lib/common.sh:71-93`): la referencia es al archivo, no a la línea, y
    verificar el número de línea sería un falso positivo garantizado en cuanto
    alguien edite el archivo citado.
    """
    base = documento.parent
    rotos: list[str] = []

    for destino in _ENLACE_MD.findall(documento.read_text(encoding="utf-8")):
        if destino.startswith("#") or re.match(r"^[a-z][a-z0-9+.-]*:", destino):
            continue

        ruta = destino.split("#", 1)[0]
        ruta = re.sub(r":\d+(?:-\d+)?$", "", ruta)  # sufijo `:71-93`
        if not ruta:
            continue

        if not (base / ruta).exists():
            rotos.append(destino)

    assert not rotos, (
        f"{documento.relative_to(RAIZ)} tiene enlaces relativos rotos:\n  " + "\n  ".join(rotos)
    )
