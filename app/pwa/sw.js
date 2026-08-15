/* AI-Investo service worker.
 *
 * Network-first for API calls, cache-first for the shell. Market data that is
 * silently stale is worse than an honest "offline" label, so a cached API
 * response is only served when the network actually fails — and the app stamps
 * the response with its age so you can see what you are looking at.
 */
const SHELL = "investo-shell-v1";
// v2 deliberately abandons v1: it may hold API errors cached as though they were
// data, and there is no way to tell which entries those are after the fact.
// `activate` deletes every cache it does not recognise, so the bump purges them.
const DATA = "investo-data-v2";
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
          // ONLY CACHE SUCCESSES. A 500 is a valid HTTP response, so `fetch`
          // resolves and the old code stored it — then served it from cache on
          // the next failure, presenting a server error as offline data that
          // never expired. The nightly job holding the database lock is enough
          // to produce one, so this was not a rare path.
          if (response.ok) {
            const copy = response.clone();
            caches.open(DATA).then((c) => c.put(request, copy));
          }
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
