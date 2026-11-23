"""`src.history` en el núcleo multi-portal: estado por portal (§5.1) y serialización (§3)."""
import json
import os
import types
from datetime import datetime, timezone

from src import history
from src.portales.moneda import moneda_de
from src.scraper.models import ProcesoClasificado


def _cfg(tmp_path):
    return types.SimpleNamespace(data_dir=str(tmp_path))


class TestEstadoPortal:
    def test_sin_archivo_todo_en_cero(self, tmp_path):
        cfg = _cfg(tmp_path)
        assert history.estado_portales(cfg) == {}
        assert history.fallos_portal(cfg, "sicop_cr") == 0

    def test_cuenta_fallos_y_se_reinicia_con_un_exito(self, tmp_path, caplog):
        cfg = _cfg(tmp_path)
        assert history.registrar_resultado_portal(cfg, "sicop_cr", ok=False, error="PortalNoDisponible: 5xx") == 1
        assert history.registrar_resultado_portal(cfg, "sicop_cr", ok=False, error="x") == 2
        assert history.registrar_resultado_portal(cfg, "comprasal_sv", ok=True, procesos=7) == 0
        estado = history.estado_portales(cfg)
        assert estado["sicop_cr"]["fallos_consecutivos"] == 2
        assert estado["sicop_cr"]["ultimo_error"] == "x"
        assert estado["sicop_cr"]["ultimo_ok"] is None
        assert estado["comprasal_sv"]["procesos"] == 7 and estado["comprasal_sv"]["ultimo_ok"]
        with caplog.at_level("INFO"):
            assert history.registrar_resultado_portal(cfg, "sicop_cr", ok=True, procesos=3) == 0
        assert "recuperado tras 2" in caplog.text
        assert history.fallos_portal(cfg, "sicop_cr") == 0
        assert history.fallos_portal(cfg, "comprasal_sv") == 0
        # Escritura atómica: no quedan temporales.
        assert sorted(os.listdir(tmp_path)) == [history.PORTAL_ESTADO]

    def test_error_largo_se_recorta(self, tmp_path):
        cfg = _cfg(tmp_path)
        history.registrar_resultado_portal(cfg, "sicop_cr", ok=False, error="e" * 5000)
        assert len(history.estado_portales(cfg)["sicop_cr"]["ultimo_error"]) == 500

    def test_nunca_lanza(self, tmp_path):
        bloqueo = tmp_path / "archivo"
        bloqueo.write_text("no soy un directorio")
        cfg = types.SimpleNamespace(data_dir=str(bloqueo / "data"))
        assert history.registrar_resultado_portal(cfg, "sicop_cr", ok=False) == 1
        assert history.fallos_portal(cfg, "sicop_cr") == 0

    def test_archivo_corrupto_se_tolera(self, tmp_path):
        (tmp_path / history.PORTAL_ESTADO).write_text("{roto")
        cfg = _cfg(tmp_path)
        assert history.estado_portales(cfg) == {}
        assert history.registrar_resultado_portal(cfg, "sicop_cr", ok=False) == 1


class TestSerializacion:
    def _sicop(self):
        return ProcesoClasificado(
            institucion="CCSS", descripcion="Servidores", url_directa="https://www.sicop.go.cr/app/",
            fecha_publicacion=datetime(2026, 11, 20, 9, 0, tzinfo=timezone.utc),
            numero_referencia="2026LD-000364-0015499999", portal_id="sicop_cr", moneda="CRC",
            monto=12_500_000.0, montos_por_moneda={}, monto_usd=24_000.0, id_externo="20261100123",
            modalidad_codigo="LD", detalle="Servidor rack", datos_portal={"cartel_seq": "00"},
            es_relevante=True)

    def test_ida_y_vuelta_con_los_campos_nuevos(self):
        p = self._sicop()
        d = history._serialize(p)
        assert "proceso_gid" not in d
        assert d["portal_id"] == "sicop_cr" and d["moneda"] == "CRC"
        r = history._deserialize(json.loads(json.dumps(d)))
        for campo in ("portal_id", "moneda", "montos_por_moneda", "monto_usd", "id_externo",
                      "modalidad_codigo", "detalle", "datos_portal", "monto"):
            assert getattr(r, campo) == getattr(p, campo), campo
        assert r.proceso_gid == "sicop_cr:2026LD-000364-0015499999"

    def test_linea_vieja_sin_portal_es_comprasal_en_usd(self):
        viejo = {"numero_referencia": "96-2026-P0011", "institucion": "MH", "descripcion": "Licencias",
                 "fecha_publicacion": "2026-03-30T16:34:44-06:00", "monto": 45000.0}
        r = history._deserialize(viejo)
        assert r.portal_id == "comprasal_sv"
        assert r.moneda == "" and moneda_de(r) == "USD"
        assert r.montos_por_moneda == {} and r.datos_portal == {}
        assert r.proceso_gid == "comprasal_sv:96-2026-P0011"

    def test_misma_referencia_en_dos_portales_no_colisiona(self):
        a = history._deserialize({"numero_referencia": "X-1", "institucion": "i", "descripcion": "d",
                                  "fecha_publicacion": "2026-03-30T16:34:44-06:00"})
        b = history._deserialize({**history._serialize(a), "portal_id": "sicop_cr"})
        assert a.proceso_gid != b.proceso_gid
