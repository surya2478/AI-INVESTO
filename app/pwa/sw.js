/* AI-Investo service worker.
 *
 * Network-first for API calls, cache-first for the shell. Market data that is
 * silently stale is worse than an honest "offline" label, so a cached API
 * response is only served when the network actually fails — and the app stamps
 * the response with its age so you can see what you are looking at.
 */
const SHELL = "investo-shell-v1";
const DATA = "investo-data-v1";
const SHELL_FILES = ["/", "/index.html", "/icon.svg", "/manifest.webmanifest"];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(SHELL).then((c) => c.addAll(SHELL_FILES)).then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(
        keys.filter((k) => k !== SHELL && k !== DATA).map((k) => caches.delete(k))
      ))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const { request } = event;
  if (request.method !== "GET") return;

  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;

  if (url.pathname.startsWith("/api/")) {
    event.respondWith(
      fetch(request)
        .then((response) => {
          const copy = response.clone();
          caches.open(DATA).then((c) => c.put(request, copy));
          return response;
        })
        .catch(() => caches.match(request).then(
          (hit) => hit || new Response(
            JSON.stringify({ error: "offline and nothing cached" }),
            { status: 503, headers: { "Content-Type": "application/json" } }
          )
        ))
    );
    return;
  }

  event.respondWith(
    caches.match(request).then((hit) => hit || fetch(request))
  );
});
