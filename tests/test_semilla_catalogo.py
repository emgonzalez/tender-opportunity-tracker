"""Catálogo y retiro de semillas: perfiles y conexiones exclusivamente falsos."""
import contextlib
from unittest.mock import MagicMock

import pytest

from src.aprendizaje import __main__ as cli
from src.aprendizaje import repo, semilla
from src.classifier import marcas
from src.clientes import repo as clientes_repo


def perfil(*marcas):
    return {"catalog": {"categories": [{"brands": list(marcas)}]}}


CATALOGO = perfil(
    {"id": "fortinet", "name": "Fortinet", "products": ["FortiGate", "Fortinet"]},
    {"id": "msft", "name": "Microsoft", "products": ["Power BI", "Cognos"]},
    {"id": "netsuite", "name": "Oracle NetSuite", "products": ["Cloud@Customer"]},
    {"id": "hpe", "name": "HPE", "products": ["ProLiant"]},
)


def test_deriva_nombre_id_productos_y_filtra_ajena_de_otra_marca():
    filas = semilla.terminos_catalogo(CATALOGO)
    assert ("fortinet", "Fortinet", "fuerte") in filas
    assert ("fortinet", "FortiGate", "fuerte") in filas
    assert ("msft", "msft", "fuerte") in filas
    assert ("netsuite", "Oracle NetSuite", "fuerte") in filas
    assert ("netsuite", "Cloud@Customer", "fuerte") in filas
    assert ("hpe", "HPE", "fuerte") in filas
    # El id canónico de marca es el que establece la pertenencia del alias ajeno.
    assert ("msft", "Power BI", "fuerte") not in filas
    assert not any(t == "Cognos" for _, t, _ in filas)
    assert ("microsoft", "Power BI", "fuerte") in semilla.terminos_catalogo(
        perfil({"id": "microsoft", "name": "Microsoft", "products": ["Power BI"]}))


def test_genericos_acentos_longitud_y_crystal():
    productos = ["Servidores", "Software", "Licencias", "soporte", "redes", "nube", "cloud",
                 "seguridad", "almacenamiento", "respaldo", "equipos", "sistema", "plataforma",
                 "tecnología", "Power", "Crystal", "AB", "x" * 121, "Power BI", "Cloud@Customer"]
    filas = semilla.terminos_catalogo(perfil({"id": "microsoft", "name": "Microsoft",
                                            "products": productos}))
    assert {t for _, t, _ in filas} == {"Microsoft", "Power BI", "Cloud@Customer"}


def test_determinismo_y_duplicados_normalizados():
    marcas_catalogo = CATALOGO["catalog"]["categories"][0]["brands"]
    assert semilla.terminos_catalogo(CATALOGO) == semilla.terminos_catalogo(
        perfil(*reversed(marcas_catalogo), *marcas_catalogo))
    filas = semilla.terminos_catalogo(perfil({"id": "marca", "name": "MÁRCA",
                                            "products": ["marca", " MÁRCA "]}))
    assert len(filas) == 1


def test_oracle_solo_conserva_base_de_datos_como_debil():
    assert semilla.SEMILLA["oracle"]["debil"] == ("base de datos",)
    assert not semilla.OBSOLETOS.intersection(semilla.terminos_semilla())


@pytest.fixture
def adb(monkeypatch):
    estado = {"filas": [], "existentes": set(), "rechazados": [], "catalogo": [], "fijas": []}
    monkeypatch.setattr(clientes_repo, "activo", lambda conn, cid: (1, CATALOGO))
    monkeypatch.setattr(repo, "existe_cliente", lambda *a: True)
    monkeypatch.setattr(repo, "terminos_existentes", lambda *a: estado["existentes"])
    monkeypatch.setattr(repo, "listar_terminos_activos", lambda *a: estado["filas"])

    def insertar(tipo, filas):
        estado[tipo].extend(filas)
        estado["existentes"].update(filas)
        return len(filas)

    monkeypatch.setattr(repo, "sembrar_terminos", lambda c, cid, ts, **kw: insertar("fijas", ts))
    monkeypatch.setattr(semilla, "_sembrar_catalogo", lambda c, cid, ts, actor: insertar("catalogo", ts))
    monkeypatch.setattr(repo, "rechazar_termino",
                        lambda c, tid, actor: estado["rechazados"].append(tid) is None)
    monkeypatch.setattr(semilla, "_rechazar_obsoleto",
                        lambda c, cid, f, actor: repo.rechazar_termino(c, f["termino_id"], actor))
    return estado


def ejecutar(*args):
    return cli.main(["semilla", "--cliente", "demo", *args],
                    conectar=lambda: contextlib.nullcontext(MagicMock()))


def test_cli_catalogo_dry_run_aplicar_idempotente(adb, capsys):
    assert ejecutar() == 0
    assert "+ fortinet" in capsys.readouterr().out
    assert not adb["catalogo"] and not adb["fijas"]
    assert ejecutar("--aplicar") == 0
    assert ("fortinet", "FortiGate", "fuerte") in adb["catalogo"]
    cantidades = len(adb["catalogo"]), len(adb["fijas"])
    assert ejecutar("--aplicar") == 0
    assert cantidades == (len(adb["catalogo"]), len(adb["fijas"]))
    assert "= fortinet" in capsys.readouterr().out


def test_retirar_solo_semilla_fija_obsoleta_sin_edicion(adb, capsys):
    base = dict(marca_id="oracle", termino="nube", tipo="debil", origen="semilla",
                estado="semilla", version=1)
    adb["filas"] = [dict(base, termino_id=i, **cambios) for i, cambios in enumerate([
        {}, {"estado": "activo"}, {"estado": "propuesto"}, {"origen": "catalogo"},
        {"origen": "manual"}, {"version": 2}, {"termino": "base de datos"},
        {"marca_id": "fortinet"}, {"termino": "licenciamiento"}, {"termino": "soporte técnico"},
    ])]
    assert ejecutar("--retirar-obsoletos") == 0
    out = capsys.readouterr().out
    assert "id=0; rechazar" in out and "id=8; rechazar" in out and "id=9; rechazar" in out
    assert out.count("; rechazar") == 3
    assert not adb["rechazados"]
    assert ejecutar("--aplicar") == 0
    assert not adb["rechazados"]
    assert ejecutar("--aplicar", "--retirar-obsoletos") == 0
    assert adb["rechazados"] == [0, 8, 9]


def test_catalogo_inserta_con_origen_y_auditoria(monkeypatch):
    conn = MagicMock()
    cursor = conn.cursor.return_value.__enter__.return_value
    cursor.rowcount = 1
    auditar = MagicMock()
    monkeypatch.setattr(repo, "_auditar", auditar)
    assert semilla._sembrar_catalogo(conn, "demo", [("fortinet", "Fortinet", "fuerte")], "cli:test") == 1
    sql, lote = cursor.executemany.call_args.args
    assert "'semilla', 'catalogo'" in sql
    assert lote[0]["termino"] == "Fortinet"
    auditar.assert_called_once()
    conn.commit.assert_not_called()


@pytest.mark.parametrize("alias", ["Fortinet", "FortiGate"])
def test_cita_valida_para_marca_correcta(alias):
    filas = semilla.terminos_catalogo(CATALOGO)
    fuertes = [t for m, t, _ in filas if m == "fortinet"]
    cita = f"Renovación de {alias}"
    assert marcas.validar_evidencia(cita, [cita], fuertes)
    assert not marcas.validar_evidencia(cita, [cita], [t for m, t, _ in filas if m == "hpe"])


def test_licenciamiento_solo_no_genera_veredicto_oracle():
    from src.notifier.veredicto_marca import veredicto_marca
    filas = [dict(marca_id=m, termino=t, tipo=tipo, estado="semilla")
             for m, t, tipo in semilla.terminos_semilla()]
    assert not marcas.validar_evidencia("licenciamiento", ["licenciamiento"], filas)
    assert veredicto_marca({"descripcion": "licenciamiento"}, None, "oracle", terminos=filas).nivel != "por_confirmar"


def test_catalogo_respeta_ajenas_existentes_de_otra_marca(adb):
    adb["existentes"].add(("otra", "FortiGate", "ajena"))
    assert ejecutar("--aplicar") == 0
    assert ("fortinet", "FortiGate", "fuerte") not in adb["catalogo"]


@pytest.mark.parametrize("actual, esperado", [
    (("semilla", "semilla", 1, "oracle", "nube", "debil"), True),
    (("activo", "semilla", 2, "oracle", "nube", "debil"), False),
    (("semilla", "manual", 1, "oracle", "nube", "debil"), False),
    (("semilla", "semilla", 2, "oracle", "nube", "debil"), False),
    (None, False),
])
def test_retiro_revalida_edicion_concurrente(monkeypatch, actual, esperado):
    conn = MagicMock()
    cursor = conn.cursor.return_value.__enter__.return_value
    cursor.fetchone.return_value = actual
    rechazar = MagicMock(return_value=True)
    monkeypatch.setattr(repo, "rechazar_termino", rechazar)
    assert semilla._rechazar_obsoleto(conn, "demo", {"termino_id": 7}, "cli:test") == esperado
    assert rechazar.call_count == int(esperado)
    assert "FOR UPDATE" in cursor.execute.call_args.args[0]


@pytest.mark.parametrize("nombre", [
    "HPE", "Fortinet", "Aruba", "Supermicro", "APC", "Cisco", "Microsoft", "VMware",
    "Citrix", "OneSpan", "Imperva", "Dynatrace", "Veritas", "ExaGrid", "PureStorage",
    "Cyrebro", "Hitachi", "Adobe", "Autodesk",
])
def test_todas_las_marcas_representadas_validan_su_cita(nombre):
    mid = nombre.lower()
    filas = semilla.terminos_catalogo(perfil({"id": mid, "name": nombre, "products": []}))
    assert filas == [(mid, nombre, "fuerte")]
    cita = f"Renovación de {nombre}"
    assert marcas.validar_evidencia(cita, [cita], [t for _, t, _ in filas])
