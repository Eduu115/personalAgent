// Service worker minimo: esta SOLO para que la consola se pueda instalar en la
// pantalla de inicio. No cachea NADA.
//
// Cachear respuestas de la API aqui seria peor que no tener consola: verias una
// cola de aprobaciones de hace media hora y aprobarias algo que ya caduco. El
// manejador de fetch existe porque el navegador lo exige para considerarla
// instalable, pero no responde: todo va a la red.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (evento) => evento.waitUntil(self.clients.claim()));
self.addEventListener("fetch", () => {});
