// 極簡 service worker：只為了可安裝與離線時顯示外殼。資料一律走網路，不快取預測結果。
const SHELL = "yb-shell-v1";
const FILES = ["/static/common.css", "/static/common.js", "/static/icons/icon-192.png"];
self.addEventListener("install", e => { e.waitUntil(caches.open(SHELL).then(c => Promise.allSettled(FILES.map(f => c.add(f)))).then(() => self.skipWaiting())); });
self.addEventListener("activate", e => { e.waitUntil(caches.keys().then(ks => Promise.all(ks.filter(k => k !== SHELL).map(k => caches.delete(k)))).then(() => self.clients.claim())); });
self.addEventListener("fetch", e => {
  const u = new URL(e.request.url);
  if (u.pathname.startsWith("/api/")) return;               // 即時資料不快取
  e.respondWith(fetch(e.request).catch(() => caches.match(e.request).then(r => r || caches.match("/citizen"))));
});
