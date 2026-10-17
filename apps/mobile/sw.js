/* Boom Mobile — service worker cơ bản.
 * - Cache-first cho file tĩnh (app shell): mở được ngay cả khi offline.
 * - Network-first cho /api/*: luôn lấy dữ liệu mới; offline thì trả lỗi JSON
 *   để UI báo rõ, không bịa dữ liệu.
 */
"use strict";

const CACHE = "boom-mobile-v1";
const SHELL = [
  "./",
  "./index.html",
  "./manifest.json",
  "./icons/icon-192.png",
  "./icons/icon-512.png",
  "./icons/icon-maskable-512.png",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE).then((cache) => cache.addAll(SHELL)).then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);

  // Chỉ xử lý request cùng origin với PWA.
  if (url.origin !== self.location.origin) return;

  // API: network-first — dữ liệu phải mới; offline thì báo lỗi JSON rõ ràng.
  if (url.pathname.startsWith("/api/")) {
    event.respondWith(
      fetch(event.request).catch(
        () => new Response(JSON.stringify({error: "offline: không kết nối được API"}), {
          status: 503,
          headers: {"Content-Type": "application/json"},
        })
      )
    );
    return;
  }

  // File tĩnh: cache-first, đồng thời cập nhật cache nền.
  event.respondWith(
    caches.match(event.request).then((cached) => {
      const net = fetch(event.request)
        .then((res) => {
          if (res.ok) {
            const copy = res.clone();
            caches.open(CACHE).then((cache) => cache.put(event.request, copy));
          }
          return res;
        })
        .catch(() => cached);
      return cached || net;
    })
  );
});
