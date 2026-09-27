"""Tests de src.mercado.api_publica — sin red real, `requests.get` mockeado.

El endpoint (`/api/v1/publico/obtener/procesos/publicos`) se verificó en vivo
(2026-09-27): pública, sin sesión, pagina con `pagination=true&page=N&
per_page=N` y el total real viaja en la cabecera `total_rows` (no en el
body) — estos tests fijan ese contrato con respuestas fabricadas.
"""
from unittest.mock import MagicMock, patch

from src.mercado import api_publica


def _respuesta(filas: list[dict], total_rows: int):
    r = MagicMock()
    r.raise_for_status = MagicMock()
    r.json = MagicMock(return_value={"data": filas, "message": "ok"})
    r.headers = {"total_rows": str(total_rows)}
    return r


class TestPagina:
    def test_lee_total_de_la_cabecera_no_del_body(self):
        with patch.object(api_publica.requests, "get", return_value=_respuesta([{"id": 1}], 131775)) as m:
            filas, total = api_publica._pagina(1)
        assert total == 131775
        assert filas == [{"id": 1}]
        # el body no trae "total_rows": si esta función lo leyera de ahí, fallaría
        assert "total_rows" not in m.return_value.json()

    def test_pasa_los_filtros_confirmados_como_query_params(self):
        with patch.object(api_publica.requests, "get", return_value=_respuesta([], 0)) as m:
            api_publica._pagina(1, id_institucion=25, modalidad=12, nombre="medicamentos")
        params = m.call_args.kwargs["params"]
        assert params["id_institucion"] == 25
        assert params["modalidad"] == 12
        assert params["nombre"] == "medicamentos"

    def test_filtros_none_no_se_envian(self):
        with patch.object(api_publica.requests, "get", return_value=_respuesta([], 0)) as m:
            api_publica._pagina(1, id_institucion=None)
        assert "id_institucion" not in m.call_args.kwargs["params"]


class TestPaginarTodo:
    def test_recorre_hasta_agotar_el_total(self):
        paginas = [_respuesta([{"id": 1}, {"id": 2}], 4), _respuesta([{"id": 3}, {"id": 4}], 4)]
        with patch.object(api_publica.requests, "get", side_effect=paginas):
            with patch.object(api_publica.time, "sleep"):  # no esperar de verdad en el test
                resultado = list(api_publica.paginar_todo(per_page=2))
        assert resultado == [[{"id": 1}, {"id": 2}], [{"id": 3}, {"id": 4}]]

    def test_pagina_vacia_corta_el_barrido(self):
        with patch.object(api_publica.requests, "get", return_value=_respuesta([], 0)):
            resultado = list(api_publica.paginar_todo())
        assert resultado == []

    def test_reenvia_filtros_a_cada_pagina(self):
        with patch.object(api_publica.requests, "get", return_value=_respuesta([], 0)) as m:
            list(api_publica.paginar_todo(id_institucion=25))
        assert m.call_args.kwargs["params"]["id_institucion"] == 25
