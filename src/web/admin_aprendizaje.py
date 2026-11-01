"""Administración de aprendizaje: SQL acotado, alcance por cliente y escrituras auditadas."""
from __future__ import annotations

import logging
import unicodedata
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Request

from src.aprendizaje import repo
from src.clientes import repo as clientes_repo
from src.clientes import servicio
from src.memoria import db
from src.web import admin, feedback, rutas
from src.web.plantillas import pagina
from src.web.portal import _sin_datos

router = APIRouter()
BASE = rutas.ADMIN_LEARNING
TAMANO = 25
logger = logging.getLogger(__name__)
SECCIONES = frozenset({'labels', 'terms', 'calibration', 'explore'})


def _url(cliente_id):
    return BASE + '/' + quote(cliente_id, safe='')


def _marcas(conn, cliente_id):
    activo = clientes_repo.activo(conn, cliente_id)
    perfil = activo[1] if activo else {}
    return sorted({m['id'] for c in (perfil.get('catalog') or {}).get('categories', [])
                   for m in c.get('brands', []) if isinstance(m, dict) and isinstance(m.get('id'), str)})


def _texto(valor, limite, obligatorio=True):
    if not isinstance(valor, str) or len(valor.encode('utf-8')) > limite:
        raise ValueError('longitud')
    if any(unicodedata.category(c).startswith('C') for c in valor):
        raise ValueError('control')
    valor = valor.strip()
    if obligatorio and not valor:
        raise ValueError('vacío')
    return valor


def _numero(valor, defecto=1):
    try:
        return max(1, min(int(valor), 10000))
    except (TypeError, ValueError):
        return defecto


def _listar(conn, cliente_id, seccion, filtros, numero):
    binds = {'cliente_id': cliente_id, 'offset': (numero - 1) * TAMANO, 'limite': TAMANO + 1}
    where = ['cliente_id = :cliente_id']
    permitidos = {
        'marca_id': None, 'etiqueta': repo.ETIQUETAS, 'fuente': repo.FUENTES, 'split': repo.SPLITS,
        'activo': {'0', '1'}, 'estado': {'semilla', 'propuesto', 'activo', 'rechazado'},
    }
    campos = {'labels': ('marca_id', 'etiqueta', 'fuente', 'split', 'activo'),
              'terms': ('marca_id', 'estado'), 'calibration': ('marca_id',), 'explore': ('marca_id',)}
    for campo in campos[seccion]:
        valor = filtros.get(campo, '')
        if valor:
            _texto(valor, 64)
            if permitidos[campo] is not None and valor not in permitidos[campo]:
                raise ValueError('filtro')
            where.append(campo + ' = :' + campo)
            binds[campo] = valor
    columnas = {
        'labels': ('ejemplo_id', 'marca_id', 'titulo_texto', 'etiqueta', 'fuente', 'split', 'activo'),
        'terms': ('termino_id', 'marca_id', 'termino', 'tipo', 'estado', 'version'),
        'calibration': ('version', 'marca_id', 'estado', 'metricas', 'umbral_confirmada', 'umbral_posible', 'k', 'parametros', 'pesos_fuentes'),
        'explore': ('proceso_gid', 'titulo', 'marca_id', 'marcas'),
    }[seccion]
    if seccion == 'explore':
        # Solo proyectamos escalares pequeños del JSON; el documento completo viaja como CLOB.
        sql = """SELECT cp.proceso_gid, p.titulo, LOWER(j.marca_id), JSON_SERIALIZE(cp.marcas RETURNING CLOB)
                 FROM cliente_proceso cp JOIN proceso p ON p.proceso_gid = cp.proceso_gid,
                 JSON_TABLE(cp.marcas, '$[*]' COLUMNS (
                   marca_id VARCHAR2(64) PATH '$.marca' NULL ON ERROR,
                   veredicto VARCHAR2(32) PATH '$.veredicto' NULL ON ERROR)) j
                 WHERE cp.cliente_id = :cliente_id AND j.veredicto = 'por_confirmar'"""
        if 'marca_id' in binds:
            sql += ' AND LOWER(j.marca_id) = :marca_id'
        sql += ' ORDER BY cp.actualizado_en DESC, cp.proceso_gid, j.marca_id'
    else:
        tabla, orden = {'labels': ('ejemplo_marca', 'ejemplo_id'), 'terms': ('marca_termino', 'termino_id'),
                        'calibration': ('marca_calibracion', 'version')}[seccion]
        seleccion = ', '.join(f'JSON_SERIALIZE({c} RETURNING CLOB)' if c in {'metricas', 'parametros', 'pesos_fuentes'} else c
                              for c in columnas)
        sql = f"SELECT {seleccion} FROM {tabla} WHERE " + ' AND '.join(where) + f' ORDER BY {orden} DESC'
    filas = repo._todos(conn, sql + ' OFFSET :offset ROWS FETCH NEXT :limite ROWS ONLY', binds)
    salida = [dict(zip(columnas, fila)) for fila in filas]
    for fila in salida:
        for campo in ('metricas', 'marcas', 'parametros', 'pesos_fuentes'):
            if campo in fila:
                fila[campo] = repo._a_json(fila[campo])
    return salida[:TAMANO], len(salida) > TAMANO


@router.get(BASE)
def indice(request: Request):
    alcance, respuesta = admin.acceso_admin(request)
    if respuesta is not None:
        return respuesta
    try:
        with db.connection(request.app.state.cfg) as conn:
            clientes = servicio.listar(conn, alcance)
    except Exception:
        return _sin_datos(request)
    return pagina(request, 'portal_admin_aprendizaje.html', **admin.contexto_admin(request, alcance),
                  clientes=clientes, base=BASE, c=None)


@router.get(BASE + '/{cliente_id}')
@router.get(BASE + '/{cliente_id}/{seccion}')
def vista(request: Request, cliente_id: str, seccion: str = 'labels'):
    alcance, respuesta = admin.acceso_admin(request)
    if respuesta is not None:
        return respuesta
    try:
        with db.connection(request.app.state.cfg) as conn:
            cliente = servicio.visible(conn, alcance, cliente_id)
            if cliente is None or seccion not in SECCIONES:
                return feedback.no_encontrado(request)
            filtros = dict(request.query_params)
            numero = _numero(filtros.get('page'))
            filas, siguiente = _listar(conn, cliente_id, seccion, filtros, numero)
            marcas = _marcas(conn, cliente_id)
    except ValueError:
        return feedback.no_encontrado(request)
    except Exception:
        logger.exception('aprendizaje: lectura fallida')
        return _sin_datos(request)
    url = _url(cliente_id)
    def enlace(n):
        return url + '/' + seccion + '?' + urlencode({**filtros, 'page': n})
    return pagina(request, 'portal_admin_aprendizaje.html', **admin.contexto_admin(request, alcance),
                  c=cliente, url=url, base=BASE, seccion=seccion, filas=filas, marcas=marcas, filtros=filtros,
                  pagina_num=numero, hay_siguiente=siguiente, anterior_url=enlace(numero - 1) if numero > 1 else None,
                  siguiente_url=enlace(numero + 1))


def _guardar(conn, alcance, cliente_id, seccion, identificador, operacion, datos):
    if servicio.visible(conn, alcance, cliente_id) is None:
        return admin.NO_ENCONTRADO
    if seccion not in {'labels', 'terms', 'calibration'}:
        return admin.NO_ENCONTRADO
    fila = None
    if identificador is not None:
        tabla, clave = {'labels': ('ejemplo_marca', 'ejemplo_id'), 'terms': ('marca_termino', 'termino_id'),
                        'calibration': ('marca_calibracion', 'version')}[seccion]
        if not identificador.isdecimal() or len(identificador) > 18:
            return admin.NO_ENCONTRADO
        fila = repo._uno(conn, f'SELECT {clave} FROM {tabla} WHERE cliente_id = :cliente_id '
                         f'AND {clave} = :id FOR UPDATE', {'cliente_id': cliente_id, 'id': int(identificador)})
        if fila is None:
            return admin.NO_ENCONTRADO
    try:
        if fila is None:
            marca = _texto(datos.get('marca_id'), 64)
            if marca not in _marcas(conn, cliente_id):
                return admin.INVALIDO
            if seccion == 'labels':
                gid = _texto(datos.get('proceso_gid'), 96)
                titulo = repo._uno(conn, 'SELECT p.titulo FROM proceso p JOIN cliente_proceso cp '
                                   'ON cp.proceso_gid = p.proceso_gid WHERE cp.cliente_id = :cliente_id '
                                   'AND cp.proceso_gid = :gid', {'cliente_id': cliente_id, 'gid': gid})
                if titulo is None:
                    return admin.NO_ENCONTRADO
                etiqueta, split = datos.get('etiqueta'), datos.get('split', 'entrenamiento')
                if etiqueta not in repo.ETIQUETAS or split not in repo.SPLITS:
                    return admin.INVALIDO
                evidencia = _texto(datos.get('evidencia', ''), 1000, False)
                repo.agregar_ejemplo(conn, cliente_id, marca, etiqueta, 'humana', titulo[0], proceso_gid=gid,
                                     split=split, evidencia=evidencia, creado_por=servicio.creado_por(conn, alcance))
            elif seccion == 'terms':
                termino = _texto(datos.get('termino'), 120)
                if termino.casefold() == 'crystal' or datos.get('tipo') not in repo.TIPOS_TERMINO:
                    return admin.INVALIDO
                repo.proponer_termino(conn, cliente_id, marca, termino, datos['tipo'], origen='humana',
                                      creado_por=alcance.usuario_id)
            else:
                return admin.INVALIDO
        elif seccion == 'labels' and operacion == 'deactivate':
            repo.desactivar_ejemplo(conn, int(identificador))
        elif seccion == 'terms' and operacion in {'activate', 'reject'}:
            funcion = repo.activar_termino if operacion == 'activate' else repo.rechazar_termino
            if not funcion(conn, int(identificador), actor=alcance.usuario_id):
                return admin.CONFLICTO
        elif seccion == 'calibration' and operacion in {'promote', 'archive'}:
            if datos.get('confirmar') != 'si':
                return admin.INVALIDO
            funcion = repo.promover if operacion == 'promote' else repo.archivar
            funcion(conn, int(identificador), actor=alcance.usuario_id)
        else:
            return admin.NO_ENCONTRADO
    except ValueError:
        return admin.INVALIDO
    return admin.OK


@router.post(BASE + '/{cliente_id}/{seccion}')
@router.post(BASE + '/{cliente_id}/{seccion}/{identificador}/{operacion}')
async def guardar(request: Request, cliente_id: str, seccion: str, identificador: str | None = None,
                  operacion: str = 'create'):
    datos = dict(await request.form())
    acciones = {('labels', 'create'): 'EJEMPLO_ETIQUETADO', ('labels', 'deactivate'): 'EJEMPLO_DESACTIVADO',
                ('terms', 'create'): 'TERMINO_PROPUESTO', ('terms', 'activate'): 'TERMINO_ACTIVADO',
                ('terms', 'reject'): 'TERMINO_RECHAZADO', ('calibration', 'promote'): 'CALIBRACION_PROMOVIDA',
                ('calibration', 'archive'): 'CALIBRACION_ARCHIVADA'}
    accion = acciones.get((seccion, operacion), 'EJEMPLO_ETIQUETADO')
    def escribir(conn, alcance, ip_hash):
        if (seccion, operacion) not in acciones or (identificador is None) != (operacion == 'create'):
            return admin.NO_ENCONTRADO
        return _guardar(conn, alcance, cliente_id, seccion, identificador, operacion, datos)
    destino = _url(cliente_id) + '/' + (seccion if seccion in SECCIONES else 'labels')
    return admin.escritura_admin(request, datos.get('csrf', ''), 'aprendizaje', accion,
                                f'cliente={cliente_id[:64]};id={(identificador or "nuevo")[:18]}',
                                escribir, destino=destino)
