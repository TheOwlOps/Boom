/**
 * connectorLabels — friendly Vietnamese labels for connectors.
 *
 * The backend uses technical ids (gmail, meta_ads, ...) and tool names
 * (gmail_send, canva_create). This file maps them to human-readable
 * Vietnamese labels so users instantly understand what each service does.
 */

/** One connector's display info. */
export interface ConnectorLabel {
  /** Display name, e.g. "Gmail". */
  label: string;
  /** Short Vietnamese description, e.g. "Đọc, tìm kiếm và gửi email". */
  description: string;
}

/** Connector id → display label. Covers all 17 backend connectors. */
export const CONNECTOR_LABELS: Record<string, ConnectorLabel> = {
  gmail: { label: "Gmail", description: "Đọc, tìm kiếm và gửi email" },
  gcalendar: { label: "Lịch Google", description: "Xem và tạo sự kiện lịch" },
  gdrive: { label: "Google Drive", description: "Tìm, tải lên và tải xuống file" },
  github: { label: "GitHub", description: "Xem repo, issue và tìm kiếm code" },
  spotify: { label: "Spotify", description: "Tìm nhạc và điều khiển phát nhạc" },
  social: { label: "Mạng xã hội", description: "Đăng bài Facebook, Instagram, Threads" },
  places: { label: "Địa điểm", description: "Tìm quán ăn, cửa hàng, địa chỉ" },
  flightaware: { label: "FlightAware", description: "Tra cứu trạng thái chuyến bay" },
  duffel: { label: "Duffel", description: "Tìm kiếm vé máy bay giá rẻ" },
  ticketmaster: { label: "Ticketmaster", description: "Tìm sự kiện, concert, vé" },
  health: { label: "Sức khỏe", description: "Xem bước chân, giấc ngủ, tập luyện" },
  canva: { label: "Canva", description: "Tạo và xuất thiết kế" },
  finances: { label: "Tài chính", description: "Xem tài khoản ngân hàng, giao dịch" },
  meta_ads: { label: "Quảng cáo Meta", description: "Quản lý chiến dịch Facebook/Instagram" },
  shopify: { label: "Shopify", description: "Quản lý cửa hàng, đơn hàng, khách hàng" },
  stripe: { label: "Stripe", description: "Xem khách hàng, hóa đơn, thanh toán" },
  tailscale: { label: "Tailscale", description: "Quản lý thiết bị trong mạng riêng" },
};

/** Fallback for unknown connector ids — prettify the id. */
export function connectorLabel(id: string): ConnectorLabel {
  const known = CONNECTOR_LABELS[id.toLowerCase()];
  if (known) return known;
  const label = id
    .split(/[_-]/)
    .map((w) => (w.length > 0 ? w[0].toUpperCase() + w.slice(1) : w))
    .join(" ");
  return { label, description: "Dịch vụ kết nối" };
}
