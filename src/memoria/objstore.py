"""src.memoria.objstore — OCI Object Storage como data-lake temporal (Fase 2, Track B).

Rol en la arquitectura: **punto de handoff durable entre las dos etapas efímeras**
(`--stage scrape` sube el PDF, `--stage analyze` lo recupera). Protege el boot
volume y permite que la etapa de análisis se reanude aunque la VM se reinicie
entre etapas.

Degradación: si el SDK de OCI no está disponible o falla, todas las funciones
devuelven None/False y el pipeline sigue usando **disco local** (el modo de
Track A). Nunca lanzan.

Autenticación: **Instance Principal** (sin llaves estáticas en la VM).
Retención: la regla de lifecycle del bucket (creada en `oci/lib/05-adb.sh`)
borra los objetos > 7 días — higiene de costo y de PII.
"""
import logging
import os

logger = logging.getLogger(__name__)

# Tamaño de parte para subidas multipart: acota la RAM al subir PDFs grandes.
_CHUNK = 8 * 1024 * 1024


def _cliente(cfg):
    """Construye el ObjectStorageClient con Instance Principal. None si no se puede."""
    try:
        import oci  # import perezoso: la app funciona sin el SDK
        signer = oci.auth.signers.InstancePrincipalsSecurityTokenSigner()
        return oci.object_storage.ObjectStorageClient({}, signer=signer)
    except Exception as exc:  # noqa: BLE001
        logger.info("Object Storage no disponible (%s); se usará disco local.", exc)
        return None


def _namespace(cliente) -> str | None:
    try:
        return cliente.get_namespace().data
    except Exception as exc:  # noqa: BLE001
        logger.warning("No se pudo obtener el namespace de Object Storage: %s", exc)
        return None


def subir_pliego(cfg, ruta_local: str, nombre_objeto: str) -> str | None:
    """Sube un PDF al bucket. Devuelve el URI del objeto, o None si no se pudo.

    Usa upload manager (multipart) para no cargar el archivo completo en RAM.
    """
    bucket = getattr(cfg, "objstore_bucket", "") or ""
    if not bucket or not os.path.exists(ruta_local):
        return None
    cliente = _cliente(cfg)
    if cliente is None:
        return None
    ns = _namespace(cliente)
    if ns is None:
        return None
    try:
        import oci
        upload_manager = oci.object_storage.UploadManager(
            cliente, allow_parallel_uploads=False)
        upload_manager.upload_file(
            ns, bucket, nombre_objeto, ruta_local, part_size=_CHUNK)
        uri = f"oci://{bucket}@{ns}/{nombre_objeto}"
        logger.info("Pliego subido a Object Storage: %s", uri)
        return uri
    except Exception as exc:  # noqa: BLE001
        logger.warning("Fallo al subir %s a Object Storage: %s", nombre_objeto, exc)
        return None


def bajar_pliego(cfg, nombre_objeto: str, ruta_destino: str) -> str | None:
    """Descarga un objeto a disco en streaming. Devuelve la ruta, o None."""
    bucket = getattr(cfg, "objstore_bucket", "") or ""
    if not bucket:
        return None
    cliente = _cliente(cfg)
    if cliente is None:
        return None
    ns = _namespace(cliente)
    if ns is None:
        return None
    try:
        resp = cliente.get_object(ns, bucket, nombre_objeto)
        os.makedirs(os.path.dirname(ruta_destino) or ".", exist_ok=True)
        # Escritura chunk a chunk: nunca el objeto completo en RAM.
        with open(ruta_destino, "wb") as f:
            for chunk in resp.data.raw.stream(_CHUNK, decode_content=False):
                f.write(chunk)
        logger.info("Pliego recuperado de Object Storage: %s", nombre_objeto)
        return ruta_destino
    except Exception as exc:  # noqa: BLE001
        logger.warning("Fallo al bajar %s de Object Storage: %s", nombre_objeto, exc)
        return None


def borrar_pliego(cfg, nombre_objeto: str) -> bool:
    """Borra un objeto (además del lifecycle de 7 días). True si se borró."""
    bucket = getattr(cfg, "objstore_bucket", "") or ""
    if not bucket:
        return False
    cliente = _cliente(cfg)
    if cliente is None:
        return False
    ns = _namespace(cliente)
    if ns is None:
        return False
    try:
        cliente.delete_object(ns, bucket, nombre_objeto)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("Fallo al borrar %s de Object Storage: %s", nombre_objeto, exc)
        return False
