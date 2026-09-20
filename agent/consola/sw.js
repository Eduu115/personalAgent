// Service worker minimo: esta SOLO para que la consola se pueda instalar en la
// pantalla de inicio. No cachea NADA, y eso no es una preferencia:
//
// /api/aprobaciones devuelve NONCES de un solo uso, que son la credencial que
// autoriza una escritura. Cachear esa respuesta los escribe en el disco del
// navegador (Cache Storage), donde sobreviven al cierre de la pestana, al
// reinicio de la tablet y a la caducidad de la aprobacion. Un nonce en disco es
// una credencial en disco, en un aparato que vive colgado en la pared.
//
// Y ademas, por lo obvio: una cola cacheada te ensena aprobaciones de hace
// media hora y te hace aprobar algo que ya caduco.
//
// El manejador de fetch existe porque el navegador lo exige para considerar la
// pagina instalable, pero no responde: todo va a la red.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (evento) => evento.waitUntil(self.clients.claim()));
self.addEventListener("fetch", () => {});
