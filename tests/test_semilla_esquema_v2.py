"""Semilla del esquema v2: SICOP, cliente_portal y taxonomia_modalidad (sin ADB real)."""
from contextlib import contextmanager
from unittest.mock import MagicMock

from scripts import semilla_esquema_v2 as semilla
from src.portales.modalidades import MODALIDADES


def _cfg(tmp_path):
    cfg = MagicMock()
    cfg.clients_dir = str(tmp_path)  # sin YAMLs
    return cfg


def _conn_falsa(monkeypatch):
    cursor = MagicMock()
    cursor.fetchone.return_value = (0,)
    conn = MagicMock()
    conn.cursor.return_value = cursor

    @contextmanager
    def _connection(cfg):
        yield conn

    monkeypatch.setattr(semilla.db, "connection", _connection)
    return cursor


def _sqls(cursor):
    return [c[0][0] for c in cursor.execute.call_args_list]


def test_taxonomia_se_genera_desde_el_codigo():
    filas = semilla._modalidades_filas("sicop_cr")
    sicop = [m for m in MODALIDADES if m.portal_id == "sicop_cr"]
    assert len(filas) == len(sicop) > 0
    assert {f["modalidad_raw"] for f in filas} == {m.nombre for m in sicop}
    assert {f["modalidad_norm"] for f in filas} <= {m.norm for m in sicop}


def test_con_costa_rica_siembra_portal_cliente_portal_y_taxonomia(monkeypatch, tmp_path):
    cursor = _conn_falsa(monkeypatch)
    conteos = semilla.semilla(_cfg(tmp_path), con_costa_rica=True)
    sqls = _sqls(cursor)
    assert all("MERGE" in s for s in sqls)  # idempotente: nada de INSERT suelto
    assert sum("MERGE INTO taxonomia_modalidad" in s for s in sqls) == conteos["modalidades"] > 0
    cp = next(c for c in cursor.execute.call_args_list if "MERGE INTO cliente_portal" in c[0][0])
    assert "VALUES (:cliente_id, :portal_id, 1, NULL, :lookback_dias)" in cp[0][0]
    assert cp[1] == {"cliente_id": "multicomputos_cr", "portal_id": "sicop_cr", "lookback_dias": 1}
    cli = next(c for c in cursor.execute.call_args_list if "MERGE INTO cliente " in c[0][0])
    assert cli[1]["activo"] == 0  # multicomputos_cr sigue inactivo
    portal_sicop = [c[0][1] for c in cursor.execute.call_args_list
                    if "MERGE INTO portal" in c[0][0] and c[0][1]["portal_id"] == "sicop_cr"]
    assert portal_sicop and portal_sicop[0]["moneda_default"] == "CRC"
    assert ":base_url, 1" in next(s for s in sqls if "MERGE INTO portal" in s)  # activo=1


def test_sin_costa_rica_no_toca_sicop(monkeypatch, tmp_path):
    cursor = _conn_falsa(monkeypatch)
    semilla.semilla(_cfg(tmp_path))
    sqls = _sqls(cursor)
    assert not any("cliente_portal" in s or "taxonomia_modalidad" in s for s in sqls)


def test_dry_run_no_abre_conexion(monkeypatch, tmp_path):
    def _boom(cfg):
        raise AssertionError("dry-run no debe conectar")

    monkeypatch.setattr(semilla.db, "connection", _boom)
    semilla.semilla(_cfg(tmp_path), dry_run=True, con_costa_rica=True)


def _correr_main(monkeypatch, argv):
    import sys
    from types import SimpleNamespace

    import src.config as config_mod

    llamadas = []
    monkeypatch.setattr(sys, "argv", ["semilla_esquema_v2", *argv])
    monkeypatch.setattr(config_mod, "get_config", lambda: SimpleNamespace())
    monkeypatch.setattr(semilla, "semilla", lambda cfg, dry_run=False, con_costa_rica=False:
                        llamadas.append(dry_run))
    semilla.main()
    return llamadas


def test_cli_dry_run_por_defecto(monkeypatch):
    assert _correr_main(monkeypatch, []) == [True]


def test_cli_dry_run_explicito_es_alias(monkeypatch):
    assert _correr_main(monkeypatch, ["--dry-run"]) == [True]


def test_cli_aplicar_escribe(monkeypatch):
    assert _correr_main(monkeypatch, ["--aplicar"]) == [False]


def test_importar_localizador_sicop_no_carga_playwright():
    """El localizador comparte constantes con `src.scraper.sicop` sin arrastrar el navegador."""
    import subprocess
    import sys

    codigo = (
        "import sys, src.drilldown.localizador_sicop as m, src.scraper.sicop as s;"
        "assert m.API_BASE is s.API_BASE and m.ORIGEN_SPA is s.ORIGEN_SPA;"
        "malos=[k for k in sys.modules if k.split('.')[0]=='playwright' or k=='src.scraper.navegador_anonimo'];"
        "assert not malos, malos"
    )
    r = subprocess.run([sys.executable, "-c", codigo], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
