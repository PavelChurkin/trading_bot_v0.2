/* Service worker «Орбиты»: показывает Web Push и открывает нужную страницу
 * по клику. Кэширует только оболочку (offline-заглушка), API — всегда сеть. */
const CACHE = "orbita-shell-v1";
const SHELL = ["/", "/static/style.css", "/static/app.js", "/static/charts.js", "/static/icons/icon-192.png"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (e) => {
  e.waitUntil(caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
    .then(() => self.clients.claim()));
});

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.origin !== location.origin || url.pathname.startsWith("/api/")) return;
  // сеть в приоритете, кэш — только если сервер недоступен
  e.respondWith(fetch(e.request).then((r) => {
    if (r.ok && SHELL.includes(url.pathname)) {
      const copy = r.clone();
      caches.open(CACHE).then((c) => c.put(e.request, copy));
    }
    return r;
  }).catch(() => caches.match(e.request).then((m) => m || caches.match("/"))));
});

self.addEventListener("push", (e) => {
  let d = {};
  try { d = e.data ? e.data.json() : {}; } catch (err) { d = { body: e.data && e.data.text() }; }
  const data = d.data || {};
  e.waitUntil(self.registration.showNotification(d.title || "Орбита", {
    body: d.body || "",
    icon: "/static/icons/icon-192.png",
    badge: "/static/icons/badge-72.png",
    tag: data.tag || undefined,
    renotify: !!data.tag,
    data: { url: data.url || "/", ...data },
  }));
});

self.addEventListener("notificationclick", (e) => {
  e.notification.close();
  const url = (e.notification.data && e.notification.data.url) || "/";
  e.waitUntil(self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((list) => {
    for (const c of list) {
      if (new URL(c.url).origin === location.origin) { c.focus(); return c.navigate ? c.navigate(url) : null; }
    }
    return self.clients.openWindow(url);
  }));
});
