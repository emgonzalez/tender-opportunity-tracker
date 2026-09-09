# ADR 0003 · El vigía externo de la ADB mira el plano de control, no la base

- **Fecha:** 2026-09-09
- **Estado:** aceptado
- **Ámbito:** `.github/workflows/adb-keepalive.yml`, `src/memoria/keepalive.py`,
  `src/memoria/service.py`
- **Relacionado:** el keepalive interno (B1.3) en
  [`../planes/fase2-track-b.md`](../planes/fase2-track-b.md)

---

## Contexto

La ADB Always Free de AMALIA_DEV tiene dos relojes distintos, y conviene no
confundirlos porque se apagan con cosas distintas:

| Reloj | Qué lo dispara | Qué lo reinicia |
|---|---|---|
| **7 días** → la base se **detiene** | ninguna conexión ni consumo de CPU | una conexión SQL\*Net o HTTPS que **ejecute SQL** |
| **90 días acumulados detenida** → la base se **reclama y se borra** | permanecer detenida | **arrancarla** |

Hoy lo único que toca el primer reloj es el `SELECT 1 FROM dual` que la corrida
diaria ejecuta desde la VM. Eso deja un punto único de fallo con una propiedad
desagradable: **el mecanismo que debía avisar del problema vive dentro de lo que
falla**. Si `amalia.timer` se cae —como ya pasó el 2026-05-14, cuando cloud-init
lo dejó inactivo— nadie ejecuta el ping, nadie manda el correo, y a los siete
días la base se apaga en silencio.

Hacía falta una capa que no dependiera de la VM. El plan pedía "un workflow
diario que abra una conexión a la ADB".

## El obstáculo: la ACL no deja entrar a un runner

La ADB está configurada con TLS one-way y una ACL de red que admite exactamente
dos orígenes (`oci/lib/05-adb.sh`, sección 2): el OCID de la VCN del tenant y la
IP pública de la VM. Esa ACL aplica a **todo** acceso a la base —SQL\*Net y
también HTTPS/ORDS/Database Actions—, así que no hay puerta lateral.

Un runner de GitHub Actions sale por rangos de Azure que GitHub publica en
`api.github.com/meta`: **~5.400 CIDRs IPv4** el día de esta medición, y rotan
semana a semana. **Un workflow en GitHub no puede abrir una conexión SQL a esta
base.** El paso del plan, tal como estaba escrito, no es implementable.

### Alternativas evaluadas

**A · Ampliar la ACL a los rangos de GitHub.** Descartada. Aun ignorando si el
campo admite miles de entradas, significa exponer el listener de la base a media
infraestructura de Azure —cualquiera puede pedir un runner ahí— con la
contraseña de `AMALIA` como única defensa, y volver a sincronizar la lista cada
semana o el vigía empieza a fallar solo. Se paga una superficie de ataque
enorme y un mantenimiento recurrente para ganar un ping.

**B · Runner self-hosted en la VM.** Descartada de plano: la VM es exactamente
lo que este mecanismo no debe presuponer encendido.

**C · Ejecutarlo dentro de OCI** (Functions, Resource Scheduler, un job con
instance principal). Descartada hoy: el tenant no tiene permiso para crear
*dynamic groups* —denegado por IAM, ver `oci/PERMISOS-IAM.md`— así que no hay
identidad de recurso disponible, y cualquier otro cómputo dentro de OCI vuelve a
ser una máquina que puede estar apagada.

**D · Llamar al plano de control de OCI desde el runner.** La API de gestión
(`oci db autonomous-database get` / `start`) es un endpoint público autenticado
con firma de API key: **no pasa por la ACL de la base**. Un runner sí puede
hablar con ella.

## Decisión

Se adopta **D**, con una limitación que conviene decir con todas las letras:

> **Una llamada al plano de control NO cuenta como actividad de la base.** El
> reloj de los 7 días solo lo reinicia una conexión que ejecute SQL. Este
> workflow **no evita el auto-stop**; prevenir el auto-stop sigue siendo tarea
> del keepalive que corre en la VM.

Lo que sí consigue, que es el riesgo que de verdad importaba:

1. **Cierra el riesgo de perder la base.** Si la encuentra detenida la arranca, y
   arrancarla reinicia el contador de los 90 días. La ADB con sus datos deja de
   ser algo que puede desaparecer por abandono.
2. **Convierte un fallo silencioso en uno ruidoso.** Encontrarla detenida es
   prueba de que la VM lleva siete días sin correr. El workflow falla a
   propósito en ese caso: la falla dispara el correo de GitHub al dueño del
   repo, por un canal que no comparte destino con la VM ni con el SMTP de la app.
3. **Acota la ventana de caída** a menos de 24 horas.

El criterio de aceptación del plan —"deja la ADB fuera de riesgo de auto-stop"—
**no se cumple tal como está escrito, y no puede cumplirse desde GitHub**. Lo que
se entrega es que la ADB queda fuera de riesgo de *desaparecer*, y que el
auto-stop deja de poder pasar inadvertido.

## Consecuencia complementaria: umbral en el keepalive interno

Si el vigía externo tarda hasta 7 días en enterarse, el keepalive de la VM tiene
que avisar mucho antes. Por eso `src/memoria/keepalive.py` guarda el conteo de
fallos consecutivos en `data/adb_keepalive.json` —el único directorio montado
como volumen— y marca `debe_alertar` **al segundo fallo consecutivo**, no al
primero.

El umbral es 2 por dos razones: un fallo aislado casi siempre es un reinicio de
mantenimiento de Oracle o un parpadeo de red, y alertar por eso entrena al
operador a ignorar la alerta; y dos días son ~48 h, que deja cinco de margen
antes del auto-stop. Un éxito resetea la racha: fallar un día de cada tres no
pone la base en riesgo. Las dos capas quedan escalonadas — la interna avisa al
segundo día, la externa rescata al séptimo.

## Costo

Cero en dólares: un job diario de ~2 minutos, dentro de los minutos gratuitos de
Actions. El costo real son seis secretos que alguien tiene que crear y una API
key que alguien tiene que rotar; ambos están documentados en la cabecera del
workflow.
