"""Filtro conservador puro: lista cerrada de familias ajenas a TI.

Solo el título aporta evidencia; la institución no define el objeto comprado.
Huawei no es un indicio por sí solo: también fabrica climatización de precisión.
Ante cualquier indicio fuerte de TI se conserva el proceso para revisión.
"""
import re
import unicodedata
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class Ruido:
    ruido: bool
    nivel: Literal['claro', 'posible', 'ninguno']
    categoria: str
    termino: str


# Familias claras: cada frase es evidencia positiva, nunca una subcadena.
CLARAS = {
    'climatizacion': ('aire acondicionado', 'aires acondicionados', 'refrigeracion',
                     'enfriamiento', 'minisplit', 'mini split', 'mini splits', 'minisplits'),
    'medico_laboratorio': ('equipo medico', 'equipos medicos', 'equipo de laboratorio',
                          'equipos de laboratorio', 'patologia', 'rayos x', 'dosimetria'),
    'electrico': ('planta electrica', 'plantas electricas', 'inversor', 'inversores',
                  'sistema electrico', 'sistemas electricos', 'infraestructura electrica',
                  'materiales electricos', 'alumbrado publico', 'iluminacion led'),
    'solar': ('panel solar', 'paneles solares', 'fotovoltaico', 'fotovoltaica',
              'fotovoltaicos', 'fotovoltaicas'),
    'incendios': ('deteccion de incendios', 'supresion de incendios',
                  'deteccion y supresion de incendios', 'alarma contra incendios',
                  'alarmas contra incendios'),
    'industrial': ('soldadura', 'maquinaria industrial', 'equipo industrial',
                   'equipos industriales', 'soplador', 'sopladores', 'blower', 'blowers',
                   'turbina', 'turbinas', 'maquinaria y equipo'),
    'gestion': ('antisoborno', 'iso 9001', 'sistema de gestion de calidad',
                'sistemas de gestion de calidad', 'auditoria de calidad',
                'auditorias de calidad', 'consultoria de calidad'),
    'medicion': ('estacion meteorologica', 'estaciones meteorologicas', 'gnss', 'rtk',
                 'calibracion de equipos de medicion'),
}
POSIBLES = {
    'audiovisual': ('televisor', 'televisores', 'radio intercomunicador',
                    'radios intercomunicadores', 'intercomunicadores'),
    'generico': ('mobiliario', 'activo fijo', 'activos fijos'),
}
# Incluye plurales y variantes habituales de escritura; no depende de un catálogo remoto.
INDICIOS_TI = (
    'servidor', 'servidores', 'software', 'licencia', 'licencias', 'licenciamiento',
    'licenciamientos', 'computadora', 'computadoras', 'computador', 'computadores',
    'laptop', 'laptops', 'equipo informatico', 'equipos informaticos', 'computo',
    'switch', 'switches', 'router', 'routers', 'firewall', 'firewalls', 'red de datos',
    'redes de datos', 'lan', 'wan', 'wifi', 'wi fi', 'wlan', 'base de datos',
    'bases de datos', 'almacenamiento', 'storage', 'backup', 'backups', 'respaldo de datos',
    'respaldos de datos', 'ciberseguridad', 'antivirus', 'nube', 'cloud', 'telefonia ip',
    'central telefonica', 'centrales telefonicas', 'planta telefonica', 'plantas telefonicas',
    'camara ip', 'camaras ip', 'cctv', 'cableado estructurado', 'ups', 'sai',
    'u p s', 'no break', 'nobreak', 'sistema de alimentacion ininterrumpida', 'sistemas de alimentacion ininterrumpida',
    'fuente de alimentacion ininterrumpida', 'soporte tecnico de ti', 'soporte tecnico informatico',
    'erp', 'saas', 'oracle', 'netsuite', 'mysql', 'java', 'weblogic', 'solaris', 'sun',
    'peoplesoft', 'hpe', 'hewlett packard', 'nutanix', 'purestorage', 'pure storage',
    'supermicro', 'citrix', 'ncomputing', 'aruba', 'fortinet', 'imperva', 'cyrebro',
    'okta', 'onespan', 'one span', 'teramind', 'dynatrace', 'veritas', 'exagrid',
)


def _normalizar(texto: str) -> str:
    texto = unicodedata.normalize('NFKD', texto.lower())
    texto = ''.join(c for c in texto if not unicodedata.combining(c))
    return ' '.join(re.sub(r'[^\w\s]', ' ', texto).split())


def _contiene(texto: str, termino: str) -> bool:
    return bool(re.search(r'(?<!\w)' + re.escape(termino) + r'(?!\w)', texto))


def evaluar_ruido(titulo: str, institucion: str | None = None) -> Ruido:
    """Conserva toda compra mixta o dudosa; devuelve la frase que motivó el filtro."""
    texto = _normalizar(titulo)
    if any(_contiene(texto, t) for t in INDICIOS_TI):
        return Ruido(False, 'ninguno', '', '')
    for familias, nivel in ((CLARAS, 'claro'), (POSIBLES, 'posible')):
        for categoria, terminos in familias.items():
            for termino in terminos:
                if _contiene(texto, termino):
                    return Ruido(nivel == 'claro', nivel, categoria, termino)
    return Ruido(False, 'ninguno', '', '')
