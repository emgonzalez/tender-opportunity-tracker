"""Tests del respaldo verificado (oci/lib/backup/amalia-backup.sh).

El respaldo es shell puro, pero eso no lo exime de tener pruebas: es el único
mecanismo que evita que perder la VM signifique perder el historial, y un
respaldo que nadie ejercita es una creencia, no un respaldo.

Cómo se prueba sin OCI: el script habla con Object Storage únicamente por HTTP
contra la URL de un PAR (PUT / GET / listado). Acá se levanta un servidor HTTP
mínimo que imita esa semántica —incluida la validación de `Content-MD5`, que es
lo que Object Storage hace del lado servidor— y se corre el script de verdad
contra él. Lo que se ejercita es el ciclo completo: snapshot → subida →
verificación (bajar y restaurar) → restauración a un directorio.

Los casos negativos son los que importan de verdad: si el objeto se corrompe
después de subirse, la verificación TIENE que fallar. Un respaldo que dice "ok"
sobre un tarball roto es peor que no tener respaldo.
"""
import base64
import hashlib
import json
import os
import shutil
import subprocess
import tarfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "oci" / "lib" / "backup" / "amalia-backup.sh"
PAR_PATH = "/p/tok3n/n/ns/b/amalia-backups-test/o/"


# ── Object Storage de mentira, con la semántica de un PAR ────────────────────

class _FakePARHandler(BaseHTTPRequestHandler):
    """PUT guarda, GET devuelve, GET del prefijo raíz lista. Como un PAR real."""

    objetos: dict[str, bytes] = {}

    def _objeto(self) -> str:
        ruta = urlparse(self.path).path
        if not ruta.startswith(PAR_PATH):
            return ""
        return ruta[len(PAR_PATH):]

    def do_PUT(self):  # noqa: N802 — nombre impuesto por BaseHTTPRequestHandler
        nombre = self._objeto()
        cuerpo = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        esperado = self.headers.get("Content-MD5")
        if esperado:
            real = base64.b64encode(hashlib.md5(cuerpo).digest()).decode()
            if real != esperado:
                self.send_response(400)
                self.end_headers()
                return
        type(self).objetos[nombre] = cuerpo
        self.send_response(200)
        self.end_headers()

    def do_GET(self):  # noqa: N802
        nombre = self._objeto()
        if not nombre:  # listado del bucket
            prefijo = parse_qs(urlparse(self.path).query).get("prefix", [""])[0]
            cuerpo = json.dumps({
                "objects": [
                    {"name": n, "size": len(b)}
                    for n, b in sorted(type(self).objetos.items())
                    if n.startswith(prefijo)
                ]
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
        elif nombre in type(self).objetos:
            cuerpo = type(self).objetos[nombre]
            self.send_response(200)
            self.send_header("Content-Type", "application/gzip")
        else:
            self.send_response(404)
            self.end_headers()
            return
        self.send_header("Content-Length", str(len(cuerpo)))
        self.end_headers()
        self.wfile.write(cuerpo)

    def log_message(self, *_args):
        pass  # sin ruido en la salida de pytest


@pytest.fixture
def bucket():
    """Servidor HTTP efímero; devuelve (url_del_par, dict_de_objetos)."""
    _FakePARHandler.objetos = {}
    srv = HTTPServer(("127.0.0.1", 0), _FakePARHandler)
    hilo = threading.Thread(target=srv.serve_forever, daemon=True)
    hilo.start()
    try:
        yield f"http://127.0.0.1:{srv.server_port}{PAR_PATH}", _FakePARHandler.objetos
    finally:
        srv.shutdown()
        srv.server_close()


# ── Andamiaje ────────────────────────────────────────────────────────────────

def _data_dir(tmp_path: Path) -> Path:
    """Un data/ como el de la VM: lo que hay que salvar y lo que no."""
    d = tmp_path / "data"
    (d / "history").mkdir(parents=True)
    (d / "last_run.json").write_text(
        json.dumps({"timestamp": "2026-09-09T07:03:00-06:00", "success": True,
                    "procesos_revisados": 42, "version": "abc123"}),
        encoding="utf-8",
    )
    (d / "gasto_diario.json").write_text(
        json.dumps({"fecha": "2026-09-09", "usd": 0.12}), encoding="utf-8")
    (d / "history" / "multicomputos.jsonl").write_text(
        "\n".join(json.dumps({"run_timestamp": f"2026-09-0{i}T07:00:00", "procesos": []})
                  for i in range(1, 8)) + "\n",
        encoding="utf-8",
    )
    # Nada de esto debe terminar en el bucket.
    (d / "storage_state.json").write_text('{"cookies": ["SECRETO DE SESION"]}', encoding="utf-8")
    (d / "screenshots").mkdir()
    (d / "screenshots" / "login.png").write_bytes(b"\x89PNG" + b"0" * 4096)
    return d


def _correr(cmd, *, par_url, tmp_path, data_dir, extra=None, check=True):
    entorno = {
        **os.environ,
        "AMALIA_DATA_DIR": str(data_dir),
        "AMALIA_BACKUP_PAR_FILE": str(tmp_path / "backup.par"),
        "AMALIA_BACKUP_STATE_DIR": str(tmp_path / "state"),
        "AMALIA_BACKUP_LOG": str(tmp_path / "backup.log"),
        "AMALIA_APP_DIR": str(tmp_path / "app"),  # sin compose → notify() no hace nada
        "AMALIA_BACKUP_WAIT_MAX_S": "0",
        **(extra or {}),
    }
    del par_url  # el PAR ya está en el archivo; el parámetro documenta la dependencia
    return subprocess.run(
        ["bash", str(SCRIPT), *cmd],
        env=entorno, capture_output=True, text=True, check=check, timeout=120,
    )


@pytest.fixture
def escenario(tmp_path, bucket):
    par_url, objetos = bucket
    data_dir = _data_dir(tmp_path)
    (tmp_path / "backup.par").write_text(
        f"url={par_url}\nexpires=2099-01-01T00:00:00Z\n", encoding="utf-8"
    )
    return par_url, objetos, data_dir


# ── El ciclo completo ────────────────────────────────────────────────────────

@pytest.mark.skipif(shutil.which("curl") is None, reason="el respaldo necesita curl")
def test_ciclo_respaldo_verificacion_restauracion(tmp_path, escenario):
    par_url, objetos, data_dir = escenario

    r = _correr(["run"], par_url=par_url, tmp_path=tmp_path, data_dir=data_dir)
    assert "OK —" in r.stdout, r.stdout + r.stderr

    # Se subió exactamente un objeto, bajo un prefijo con política de retención.
    assert len(objetos) == 1
    (nombre,) = objetos
    assert nombre.startswith(("daily/", "monthly/")) and nombre.endswith(".tar.gz")

    # El sello local prueba que la verificación ocurrió, no solo la subida.
    sello = json.loads((tmp_path / "state" / "last-backup.json").read_text())
    assert sello["verificado"] is True and sello["objeto"] == nombre

    # `restore` reconstruye el data/ a partir del objeto del bucket.
    destino = tmp_path / "restaurado"
    _correr(["restore", nombre, str(destino)], par_url=par_url,
            tmp_path=tmp_path, data_dir=data_dir)
    assert json.loads((destino / "last_run.json").read_text())["version"] == "abc123"
    assert (destino / "history" / "multicomputos.jsonl").read_text() == \
           (data_dir / "history" / "multicomputos.jsonl").read_text()


@pytest.mark.skipif(shutil.which("curl") is None, reason="el respaldo necesita curl")
def test_la_credencial_de_sesion_no_viaja_al_bucket(tmp_path, escenario):
    """storage_state.json es una credencial y queda fuera por decisión explícita."""
    par_url, objetos, data_dir = escenario
    _correr(["run"], par_url=par_url, tmp_path=tmp_path, data_dir=data_dir)

    (nombre,) = objetos
    crudo = objetos[nombre]
    (tmp_path / "bajado.tar.gz").write_bytes(crudo)
    with tarfile.open(tmp_path / "bajado.tar.gz") as tar:
        miembros = {m.name.lstrip("./") for m in tar.getmembers() if m.isfile()}

    assert not any("storage_state" in m for m in miembros)
    assert b"SECRETO DE SESION" not in crudo
    # Tampoco los derivados: son lo único que pesa y se regeneran solos.
    assert not any("screenshots" in m for m in miembros)
    assert {"data/last_run.json", "data/history/multicomputos.jsonl"} <= miembros


@pytest.mark.skipif(shutil.which("curl") is None, reason="el respaldo necesita curl")
def test_avisa_de_datos_nuevos_que_la_allowlist_no_cubre(tmp_path, escenario):
    """La allowlist protege contra filtraciones, pero puede dejar datos afuera.

    El precio de un 'todo menos X' invertido es este: si mañana alguien agrega un
    archivo durable a data/, no se respalda. Que grite es lo que hace que el
    precio sea pagable.
    """
    par_url, _objetos, data_dir = escenario
    (data_dir / "precedentes_nuevos.json").write_text("{}", encoding="utf-8")

    r = _correr(["run"], par_url=par_url, tmp_path=tmp_path, data_dir=data_dir)
    assert "precedentes_nuevos.json" in r.stdout
    assert "allowlist" in r.stdout


# ── Los casos negativos: la verificación tiene que fallar cuando corresponde ──

@pytest.mark.skipif(shutil.which("curl") is None, reason="el respaldo necesita curl")
def test_un_respaldo_corrupto_no_pasa_la_verificacion(tmp_path, escenario):
    """Si el objeto del bucket está roto, `verify` sale con error."""
    par_url, objetos, data_dir = escenario
    _correr(["run"], par_url=par_url, tmp_path=tmp_path, data_dir=data_dir)
    (nombre,) = objetos

    # Un tarball válido pero con el contenido cambiado: el gzip abre, los
    # checksums del MANIFEST no dan. Es el modo de falla que un `tar -tzf` no ve.
    origen = tmp_path / "orig.tar.gz"
    origen.write_bytes(objetos[nombre])
    picado = tmp_path / "picado"
    picado.mkdir()
    subprocess.run(["tar", "-xzf", str(origen), "-C", str(picado)], check=True)
    (picado / "data" / "last_run.json").write_text('{"adulterado": true}', encoding="utf-8")
    nuevo = tmp_path / "picado.tar.gz"
    subprocess.run(["tar", "-czf", str(nuevo), "-C", str(picado), "."], check=True)
    objetos[nombre] = nuevo.read_bytes()

    r = _correr(["verify", nombre], par_url=par_url, tmp_path=tmp_path,
                data_dir=data_dir, check=False)
    assert r.returncode != 0
    assert "NO es restaurable" in r.stdout


@pytest.mark.skipif(shutil.which("curl") is None, reason="el respaldo necesita curl")
def test_un_par_vencido_falla_en_vez_de_saltear_el_respaldo(tmp_path, escenario):
    """El modo de falla a evitar es dejar de respaldar en silencio."""
    par_url, objetos, data_dir = escenario
    (tmp_path / "backup.par").write_text(
        f"url={par_url}\nexpires=2020-01-01T00:00:00Z\n", encoding="utf-8"
    )
    r = _correr(["run"], par_url=par_url, tmp_path=tmp_path, data_dir=data_dir, check=False)
    assert r.returncode != 0
    assert "venció" in r.stdout
    assert objetos == {}


def test_sin_par_configurado_falla_ruidosamente(tmp_path, escenario):
    par_url, _objetos, data_dir = escenario
    (tmp_path / "backup.par").unlink()
    r = _correr(["run"], par_url=par_url, tmp_path=tmp_path, data_dir=data_dir, check=False)
    assert r.returncode != 0
    assert "backup_setup" in r.stdout


@pytest.mark.skipif(shutil.which("curl") is None, reason="el respaldo necesita curl")
def test_status_falla_mientras_no_haya_un_respaldo_verificado(tmp_path, escenario):
    par_url, _objetos, data_dir = escenario
    r = _correr(["status"], par_url=par_url, tmp_path=tmp_path, data_dir=data_dir, check=False)
    assert r.returncode != 0

    _correr(["run"], par_url=par_url, tmp_path=tmp_path, data_dir=data_dir)
    r = _correr(["status"], par_url=par_url, tmp_path=tmp_path, data_dir=data_dir)
    assert json.loads(r.stdout)["verificado"] is True
