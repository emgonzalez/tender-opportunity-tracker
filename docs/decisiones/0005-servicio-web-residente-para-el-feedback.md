# ADR 0005 · Un servicio web residente en la VM para el feedback de relevancia

- **Fecha:** 2026-09-26
- **Estado:** aceptado (Fase 1, el código, implementada; el despliegue —Fase 2— pendiente)
- **Ámbito:** `src/web/`, `src/feedback/`, `src/notifier/email_sender.py`,
  `templates/report.html`, `db/schema.sql`
- **Relacionado:** [`../arquitectura/vision-general.md`](../arquitectura/vision-general.md)
  (VM como orquestador sin daemons), [`0003-keepalive-adb-fuera-de-la-vm.md`](0003-keepalive-adb-fuera-de-la-vm.md)
  (por qué nada externo puede escribir en la ADB)

---

## Contexto

Cada oportunidad del correo lleva dos botones, **Relevante** e **Irrelevante**, para que
quien lo recibe le diga a AMALIA si acertó. "Irrelevante" abre un formulario con motivos
predefinidos y un comentario libre. Los botones son enlaces: alguien tiene que
**recibir el clic** desde internet, y hasta hoy no existe nada que escuche.

El sistema se diseñó sin daemons: la VM corre un `docker run --rm` por etapa, el único
ingress es el 22 desde un /32, y la ADB solo admite a la VM
([ADR 0003](0003-keepalive-adb-fuera-de-la-vm.md)).

## Decisión

El endpoint vive **en la VM**, como un servicio residente aparte (`python -m src.web`),
detrás de un proxy TLS. Es la excepción deliberada a "sin daemons", y está acotada:

- **Escribe directo a la ADB** por el mismo camino que ya tiene acceso. Un endpoint
  fuera de OCI (p. ej. un servicio serverless de terceros) no podría —la ACL no lo
  admite— y obligaría a una base paralela y a sincronizarla en cada corrida.
- **No comparte proceso con AMALIA:** contenedor propio con `mem_limit` propio. La
  corrida diaria sigue siendo efímera y no se entera de que existe.
- **Es la semilla del portal administrativo** (dashboards por rol): la capa de datos
  (`src/feedback/`) no depende de HTTP y `src/web/` es una fábrica de app con routers.
  El portal se sumará como otro router; hoy no se implementa `/admin` ni autenticación.

## Decisiones de diseño que conviene no revertir sin leer esto

1. **Un correo por destinatario, con enlaces firmados (HMAC) propios.** Sin esto el voto
   sería anónimo. El token identifica (proceso, cliente, destinatario) y **no es una
   credencial fuerte**: un correo reenviado atribuye el voto a su destinatario original.
   Se acepta y queda documentado.
2. **Un GET nunca escribe.** Gmail y Outlook (SafeLinks) precargan los enlaces de los
   correos; un voto registrado por un escáner sería un voto fantasma. Por eso también
   "Relevante" pasa por una página con un botón que hace POST. Cuesta un clic extra.
3. **404 único** ante cualquier fallo de resolución (firma mala, cliente desconocido,
   feedback apagado): quien sondea no distingue causas.
4. **`licitaciones_reportadas` es la foto del proceso al enviarlo**, por (proceso,
   cliente). No cuelga de `licitaciones_metadata`, que es por proceso y solo se llena
   con `ENABLE_MEMORIA`. Los votos **no tienen FK** a esa foto: si la foto falló, el
   criterio de quien hizo clic no se pierde por eso.
5. **MODO VALIDACIÓN intacto:** la lista efectiva se resuelve una vez y se itera sobre
   ella. Ningún destinatario real recibe nada mientras haya `override_recipients`.
6. **Con `ENABLE_FEEDBACK` apagado nada cambia:** un solo mensaje, sin botones, sin
   tocar la ADB. Un fallo de la ADB nunca impide que el reporte llegue.

## Lo que esto cuesta (y por qué es la Fase 2)

Nada de esto está desplegado. Exponer el servicio exige, y es territorio de revisión
humana (`oci/`):

- ingress **80/443** dentro del **mismo** `security-list update` (la API reemplaza la
  lista entera; si se agrega aparte, el siguiente `ssh-allow` o deploy la borra);
- una **IP pública reservada** (hoy es efímera y cambia si se recrea la VM) y un
  **dominio o subdominio** para el `FEEDBACK_BASE_URL` que va en los correos;
- memoria: el host ya "vive al filo" (~300 MB de base + 800 MB de contenedor sobre
  1 GB). El servicio necesita su propio límite y una `oom_score_adj` que no lo ponga
  primero en la fila de `earlyoom`;
- el `cost-watchdog` que apaga la VM también apaga este endpoint.

Los correos con botones **no deben enviarse** (`ENABLE_FEEDBACK=true`) hasta que ese
endpoint responda: un botón que lleva a una página caída es peor que no tener botón.
