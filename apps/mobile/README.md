# Boom Mobile (PWA)

Giao diện mobile gọn nhẹ cho trợ lý Boom: chat, xem trạng thái, xem logs.
**Không dependency, không build step** — HTML + CSS + vanilla JS thuần.

## Chạy thử (2 phút)

```bash
cd apps/mobile
python3 -m http.server 8080
```

Mở trên điện thoại (cùng Wi-Fi với máy chạy lệnh trên):

```
http://<IP-máy-tính>:8080
```

VD máy tính có IP `192.168.1.10` → mở `http://192.168.1.10:8080` trên điện thoại.

**Add to Home Screen:** mở bằng Chrome (Android) → menu ⋮ → *Add to Home screen*;
Safari (iOS) → nút Share → *Add to Home Screen*. App sẽ mở ở chế độ standalone,
có icon Boom.

## Trỏ app tới gateway Boom

Sửa **1 biến duy nhất** ở đầu thẻ `<script>` trong `index.html`:

```js
const API_BASE = "";                       // cùng origin (mặc định)
const API_BASE = "http://192.168.1.10:18080"; // trỏ thẳng tới máy chạy gateway
```

## ⚠️ 2 việc phải làm trên gateway để dùng từ điện thoại

Mặc định `gateway/api.py` chỉ phục vụ localhost và chỉ cho CORS 2 origin dev:

1. **Bind ra LAN** (mặc định chỉ `127.0.0.1` nên điện thoại không tới được).
   Sửa trong `gateway/api.py` hàm `main()` hoặc chạy gateway sau reverse proxy.
2. **Thêm origin vào CORS**: bổ sung `"http://<IP-máy-tính>:8080"` vào
   `ALLOWED_ORIGINS` trong `gateway/api.py`, rồi restart gateway.

Không làm 2 bước trên, trình duyệt sẽ chặn request (lỗi CORS / không kết nối)
— đây là giới hạn của gateway, không phải lỗi của PWA.

## Các tab

| Tab | Gọi API | Ghi chú |
|-----|---------|---------|
| 💬 Chat | `POST /api/chat` `{prompt, strategy, session_id}` | 4 chiến lược: auto/react/dag/mass; session lưu ở localStorage |
| 📊 Trạng thái | `GET /api/health`, `GET /api/channels/status` | Hiện đúng field API v1 trả về (`status`, `version`, `model`) + trạng thái 8 kênh |
| 🖥️ Logs | `GET /api/logs` (5s/lần) | **API v1 chưa có endpoint này** → tab báo rõ 404, không hiện log giả |

Chấm tròn ở header ping `GET /api/health` mỗi 15s: xanh = online, đỏ = mất kết nối.

## Cấu trúc

```
apps/mobile/
├── index.html      # toàn bộ UI (1 file)
├── manifest.json   # PWA manifest: name "Boom Mobile", theme dark, standalone
├── sw.js           # service worker: cache app shell, offline fallback
├── icons/          # icon 192/512 + maskable, gen từ docs/logo.png
└── README.md       # file này
```

## Trung thực dữ liệu

App tuân thủ nguyên tắc của Boom: **không bịa dữ liệu**. API v1 không có
uptime / số tools / số skills / logs — app hiển thị đúng những gì API trả về
và ghi chú rõ chỗ còn thiếu, thay vì vẽ số giả cho đẹp.
