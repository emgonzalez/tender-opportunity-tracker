"""Verifica `src.feedback.service` (contrato de aislamiento) y `catalogo`."""
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.feedback import catalogo, service, tokens


def _cfg(tmp_path, **kw):
    base = dict(
        enable_feedback=True, feedback_base_url="https://f.example.com",
        feedback_hmac_secret="sec", data_dir=str(tmp_path),
    )
    base.update(kw)
    return SimpleNamespace(**base)


class TestActivo:
    @pytest.mark.parametrize("cambio", [
        {"enable_feedback": False}, {"feedback_base_url": ""}, {"feedback_hmac_secret": ""},
    ])
    def test_apagado_si_falta_algo(self, tmp_path, cambio):
        cfg = _cfg(tmp_path, **cambio)
        assert service.activo(cfg) is False
        assert service.enlaces_para(cfg, "c", "P-1", "a@x.com") is None

    def test_encendido(self, tmp_path):
        assert service.activo(_cfg(tmp_path)) is True


class TestEnlaces:
    def test_enlaces_verificables_y_propios_de_cada_destinatario(self, tmp_path):
        cfg = _cfg(tmp_path)
        ana = service.enlaces_para(cfg, "multicomputos", "P-1", "ana@x.com")
        luis = service.enlaces_para(cfg, "multicomputos", "P-1", "luis@x.com")

        assert set(ana) == {"relevante", "irrelevante"}
        assert ana["relevante"] != luis["relevante"]
        token = ana["irrelevante"].split("/f/")[1].split("/")[0]
        assert tokens.verificar("sec", token).destinatario_id == tokens.destinatario_id("ana@x.com")

    def test_proceso_sin_referencia_no_lleva_botones(self, tmp_path):
        assert service.enlaces_para(_cfg(tmp_path), "c", "", "a@x.com") is None


class TestNoPropagaFallosDeAdb:
    @pytest.fixture(autouse=True)
    def adb_caida(self, monkeypatch):
        from src.memoria import db
        monkeypatch.setattr(db, "connection", MagicMock(side_effect=RuntimeError("ADB caída")))

    def test_registrar_reportadas(self, tmp_path):
        service.registrar_reportadas(_cfg(tmp_path), "c", [SimpleNamespace(numero_referencia="P-1")])

    def test_lecturas_devuelven_none(self, tmp_path):
        assert service.leer_reportada(_cfg(tmp_path), "c", "P-1") is None
        assert service.leer_voto(_cfg(tmp_path), "c", "P-1", "d") is None

    def test_guardar_voto_dice_que_no_pero_deja_respaldo(self, tmp_path):
        ok = service.guardar_voto(
            _cfg(tmp_path), client_id="c", proceso_id="P-1", dest_id="d", email="a@x.com",
            voto="IRRELEVANTE", motivos=["fuera_de_rubro"], comentario="no aplica",
        )
        assert ok is False
        linea = json.loads((tmp_path / "history" / "c.feedback.jsonl").read_text().strip())
        assert linea["proceso_id"] == "P-1" and linea["motivos"] == ["fuera_de_rubro"]

    def test_apagado_no_toca_la_adb(self, tmp_path):
        from src.memoria import db
        service.registrar_reportadas(_cfg(tmp_path, enable_feedback=False), "c", [SimpleNamespace(numero_referencia="P-1")])
        db.connection.assert_not_called()


class _ConexionFalsa:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class TestEspejoV2:
    """Doble escritura del esquema v2 (plan del portal §3.3 Fase 2)."""

    def test_apagado_no_llama_a_las_funciones_v2(self, tmp_path, monkeypatch):
        monkeypatch.setattr("src.memoria.db.connection", lambda cfg: _ConexionFalsa())
        monkeypatch.setattr("src.feedback.repo.upsert_reportada", MagicMock())
        reportada_v2 = MagicMock()
        monkeypatch.setattr("src.feedback.repo.upsert_reportada_v2", reportada_v2)

        cfg = _cfg(tmp_path)  # sin enable_esquema_v2
        service.registrar_reportadas(cfg, "c", [SimpleNamespace(numero_referencia="P-1")])

        reportada_v2.assert_not_called()

    def test_activado_llama_upsert_reportada_v2(self, tmp_path, monkeypatch):
        monkeypatch.setattr("src.memoria.db.connection", lambda cfg: _ConexionFalsa())
        monkeypatch.setattr("src.feedback.repo.upsert_reportada", MagicMock())
        reportada_v2 = MagicMock()
        monkeypatch.setattr("src.feedback.repo.upsert_reportada_v2", reportada_v2)

        cfg = _cfg(tmp_path, enable_esquema_v2=True)
        opp = SimpleNamespace(numero_referencia="P-1")
        service.registrar_reportadas(cfg, "c", [opp])

        reportada_v2.assert_called_once()
        args = reportada_v2.call_args.args
        assert args[1] == "c"
        assert args[2] is opp

    def test_reportada_v2_fallando_no_afecta_v1(self, tmp_path, monkeypatch):
        monkeypatch.setattr("src.memoria.db.connection", lambda cfg: _ConexionFalsa())
        reportada_v1 = MagicMock()
        monkeypatch.setattr("src.feedback.repo.upsert_reportada", reportada_v1)
        monkeypatch.setattr(
            "src.feedback.repo.upsert_reportada_v2",
            MagicMock(side_effect=RuntimeError("boom")),
        )

        cfg = _cfg(tmp_path, enable_esquema_v2=True)
        # No debe lanzar.
        service.registrar_reportadas(cfg, "c", [SimpleNamespace(numero_referencia="P-1")])

        reportada_v1.assert_called_once()

    def test_guardar_voto_activado_llama_upsert_voto_v2(self, tmp_path, monkeypatch):
        monkeypatch.setattr("src.memoria.db.connection", lambda cfg: _ConexionFalsa())
        monkeypatch.setattr("src.feedback.repo.upsert_voto", MagicMock())
        voto_v2 = MagicMock()
        monkeypatch.setattr("src.feedback.repo.upsert_voto_v2", voto_v2)

        cfg = _cfg(tmp_path, enable_esquema_v2=True)
        ok = service.guardar_voto(
            cfg, client_id="c", proceso_id="P-1", dest_id="d", email="a@x.com",
            voto="IRRELEVANTE", motivos=["fuera_de_rubro"], comentario="no aplica",
        )

        assert ok is True
        voto_v2.assert_called_once()
        kwargs = voto_v2.call_args.kwargs
        assert kwargs["proceso_id"] == "P-1"
        assert kwargs["cliente_id"] == "c"
        assert kwargs["destinatario_id"] == "d"

    def test_guardar_voto_v2_fallando_no_afecta_v1_ni_el_respaldo_jsonl(self, tmp_path, monkeypatch):
        monkeypatch.setattr("src.memoria.db.connection", lambda cfg: _ConexionFalsa())
        voto_v1 = MagicMock()
        monkeypatch.setattr("src.feedback.repo.upsert_voto", voto_v1)
        monkeypatch.setattr(
            "src.feedback.repo.upsert_voto_v2",
            MagicMock(side_effect=RuntimeError("boom")),
        )

        cfg = _cfg(tmp_path, enable_esquema_v2=True)
        ok = service.guardar_voto(
            cfg, client_id="c", proceso_id="P-1", dest_id="d", email="a@x.com",
            voto="IRRELEVANTE", motivos=["fuera_de_rubro"], comentario="no aplica",
        )

        assert ok is True, "el fallo del espejo v2 no debe cambiar lo que informa guardar_voto"
        voto_v1.assert_called_once()
        linea = json.loads((tmp_path / "history" / "c.feedback.jsonl").read_text().strip())
        assert linea["proceso_id"] == "P-1"


class TestCatalogo:
    def test_por_defecto(self):
        motivos = catalogo.motivos_de(SimpleNamespace(feedback_motivos=[]))
        assert {"fuera_de_rubro", "otro"} <= {m["id"] for m in motivos}

    def test_el_cliente_reemplaza_el_catalogo_entero(self):
        propios = [{"id": "x", "label": "Equis"}, {"id": "roto"}]
        assert catalogo.motivos_de(SimpleNamespace(feedback_motivos=propios)) == [{"id": "x", "label": "Equis"}]

    def test_validar_descarta_desconocidos_y_repetidos(self):
        cat = catalogo.motivos_de(SimpleNamespace(feedback_motivos=[]))
        assert catalogo.validar_motivos(["otro", "inventado", "otro", "fuera_de_rubro"], cat) == ["otro", "fuera_de_rubro"]

    def test_comentario_se_recorta(self):
        assert len(catalogo.limpiar_comentario("a" * 5000)) == catalogo.COMENTARIO_MAX
        assert catalogo.limpiar_comentario(None) == ""


class TestPerfilDelCliente:
    def test_lee_feedback_motivos_del_yaml(self, tmp_path):
        from src.classifier.client_profile import load_client_profile
        (tmp_path / "x.yaml").write_text(
            "client: {id: x, name: X}\ncatalog: {categories: []}\n"
            "feedback:\n  motivos:\n    - {id: uno, label: Uno}\n", encoding="utf-8")
        perfil = load_client_profile("x", str(tmp_path))
        assert catalogo.motivos_de(perfil) == [{"id": "uno", "label": "Uno"}]
