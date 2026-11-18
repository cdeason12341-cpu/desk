// App shell cache so Chrome offers Install. Data (/d/...) is never cached here.
// Push (DECISION 62): the desk sends {title, body, tag, url, renotify, actions, aid, bot}; nothing secret is in a payload.
const V = "desk-shell-v27";
const SHELL = ["./", "index.html", "app.css", "app.js", "chart.js", "manifest.json",
  "icons/icon-192.png", "icons/icon-512.png", "icons/apple-touch-icon.png", "icons/badge-96.png"];
self.addEventListener("install", e => { e.waitUntil(caches.open(V).then(c => c.addAll(SHELL.map(u => new Request(u, {cache: "reload"})))).then(() => self.skipWaiting())); });
self.addEventListener("activate", e => { e.waitUntil(caches.keys().then(ks => Promise.all(ks.filter(k => k !== V).map(k => caches.delete(k)))).then(() => self.clients.claim())); });
self.addEventListener("fetch", e => {
  const u = new URL(e.request.url);
  if (e.request.method !== "GET" || u.origin !== location.origin || u.pathname.includes("/d/")) return;
  // network first, fall back to the cached shell when offline
  e.respondWith(fetch(e.request, {cache: "no-cache"}).then(r => { const cp = r.clone(); if (r.ok) caches.open(V).then(c => c.put(e.request, cp)); return r; })
    .catch(() => caches.match(e.request, {ignoreSearch: true}).then(r => r || caches.match("index.html"))));
});

self.addEventListener("push", e => {
  let d;
  try { d = e.data ? e.data.json() : {}; } catch (_) { d = { title: "Desk", body: e.data ? e.data.text() : "" }; }
  const opts = {
    body: String(d.body || ""), icon: "icons/icon-192.png", badge: "icons/badge-96.png", data: d,
    timestamp: d.ts || Date.now(), requireInteraction: !!d.requireInteraction,
  };
  if (d.tag) { opts.tag = String(d.tag); opts.renotify = d.renotify !== false; }   // same tag = replaced in place
  if (Array.isArray(d.actions) && d.actions.length) opts.actions = d.actions.slice(0, 2).map(a => ({ action: String(a.action), title: String(a.title) }));
  e.waitUntil(self.registration.showNotification(String(d.title || "Desk"), opts));
});

function target(d, action) {
  const bot = /^[A-Za-z0-9_]{5,32}$/.test(d.bot || "") ? d.bot : "Denarpobot";
  if ((action === "approve" || action === "skip") && /^\d+$/.test(String(d.aid))) // owner's Telegram confirms it (owner-chat lock)
    return `https://t.me/${bot}?start=${action === "approve" ? "gmap" : "gmsk"}_${d.aid}`;
  const u = String(d.url || "#/home");
  if (/^https:\/\/fomo\.family\//.test(u)) return u;                               // links.py coin_url (r=savipww) from the desk
  return new URL(u.startsWith("#") ? "./" + u : "./#/home", self.registration.scope).href;
}

self.addEventListener("notificationclick", e => {
  const d = e.notification.data || {};
  e.notification.close();
  const url = target(d, e.action);
  e.waitUntil((async () => {
    if (url.startsWith(self.registration.scope)) {
      const cs = await clients.matchAll({ type: "window", includeUncontrolled: true });
      for (const c of cs) {
        if (!c.url.startsWith(self.registration.scope)) continue;
        try { await c.focus(); } catch (_) {}
        c.postMessage({ nav: new URL(url).hash });
        return;
      }
    }
    await clients.openWindow(url);
  })());
});
