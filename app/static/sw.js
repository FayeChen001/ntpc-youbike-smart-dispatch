// Service worker：離線外殼快取 + Web Push 接收端。
//
// 誠實說明：安裝這個 worker 不等於手機收得到背景推播。
// 背景推播還需要「推播伺服器 + VAPID 金鑰 + 使用者訂閱」三者齊備。
// 本專案未設定推播伺服器（見 /api/c/push/status），所以下面的 push 事件實際上不會被觸發。
// 保留處理器是為了介接推播伺服器時不必改前端，不是為了讓介面宣稱已經有推播。
const SHELL = "yb-shell-v2";
const FILES = ["/static/common.css", "/static/common.js", "/static/icons/icon-192.png"];

self.addEventListener("install", e => {
  e.waitUntil(caches.open(SHELL).then(c => Promise.allSettled(FILES.map(f => c.add(f)))).then(() => self.skipWaiting()));
});
self.addEventListener("activate", e => {
  e.waitUntil(caches.keys().then(ks => Promise.all(ks.filter(k => k !== SHELL).map(k => caches.delete(k)))).then(() => self.clients.claim()));
});
self.addEventListener("fetch", e => {
  const u = new URL(e.request.url);
  if (u.pathname.startsWith("/api/")) return;                 // 即時資料一律走網路，不快取
  e.respondWith(fetch(e.request).catch(() => caches.match(e.request).then(r => r || caches.match("/citizen"))));
});

// 由推播伺服器送來的訊息。目前沒有推播伺服器，因此這段在本次 demo 不會執行。
self.addEventListener("push", e => {
  let d = { title: "YouBike 生活圈", body: "有新的通知", url: "/citizen" };
  try { if (e.data) d = Object.assign(d, e.data.json()); } catch (err) { if (e.data) d.body = e.data.text(); }
  e.waitUntil(self.registration.showNotification(d.title, {
    body: d.body, icon: "/static/icons/icon-192.png", badge: "/static/icons/icon-192.png",
    tag: d.tag || "yb", renotify: false, data: { url: d.url || "/citizen" }
  }));
});

// 頁面還活著時，由頁面主動要求顯示系統通知。關掉頁面後不會有。
self.addEventListener("message", e => {
  const d = e.data || {};
  if (d.type !== "show-notification") return;
  self.registration.showNotification(d.title || "YouBike 生活圈", {
    body: d.body || "", icon: "/static/icons/icon-192.png", badge: "/static/icons/icon-192.png",
    tag: d.tag || "yb-inpage", data: { url: d.url || "/citizen" }
  });
});

// 依角色路由：政府通知開 /gov、營運通知開 /ops、民眾通知才開 /citizen。
// 先找同一個角色已開著的分頁聚焦，找不到才新開。帶 event_id 讓目標頁深連結到那一筆。
self.addEventListener("notificationclick", e => {
  e.notification.close();
  const d = e.notification.data || {};
  const base = d.url || "/citizen";
  const target = base + (d.event_id ? (base.includes("?") ? "&" : "?") + "focus=" + encodeURIComponent(d.event_id) : "");
  e.waitUntil(clients.matchAll({ type: "window", includeUncontrolled: true }).then(list => {
    for (const c of list) {
      try {
        if (new URL(c.url).pathname === new URL(target, self.location.origin).pathname && "focus" in c) {
          if ("navigate" in c) c.navigate(target);
          return c.focus();
        }
      } catch (err) {}
    }
    if (clients.openWindow) return clients.openWindow(target);
  }));
});
