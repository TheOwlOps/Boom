/**
 * BotPanel — right-side info panel shown when chatting with a bot.
 *
 * Shows the bot's large avatar, name, description, status, allowed
 * tools/skills as chips, channel bindings, and a shortcut to the
 * advanced editor in Settings. Collapsible via the onClose prop.
 */
import { X, Settings2, Wrench, Radio } from "lucide-react";
import type { Bot } from "../lib/types";
import { BotAvatar } from "./BotAvatar";
import "./BotPanel.css";

export interface BotPanelProps {
  bot: Bot;
  onClose(): void;
  /** Open the advanced bot editor (Settings → Bots). */
  onEditAdvanced(): void;
}

function Chip({ label }: { label: string }) {
  return <span className="botpanel-chip">{label}</span>;
}

export default function BotPanel({ bot, onClose, onEditAdvanced }: BotPanelProps) {
  const online = bot.is_active;
  const tools = bot.allowed_tools ?? [];
  const channels = bot.channel_bindings ?? [];

  return (
    <aside className="botpanel" aria-label={`Thông tin ${bot.name}`}>
      <div className="botpanel-header">
        <span className="botpanel-title">Chi tiết bot</span>
        <button
          type="button"
          className="icon-btn icon-btn-sm"
          onClick={onClose}
          title="Đóng panel"
          aria-label="Đóng panel chi tiết bot"
        >
          <X size={16} />
        </button>
      </div>

      <div className="botpanel-hero">
        <BotAvatar
          botId={bot.id}
          name={bot.name}
          emoji={bot.avatar_emoji}
          size={76}
          status={online ? "online" : "offline"}
        />
        <h2 className="botpanel-name">{bot.name}</h2>
        <span className={`botpanel-status${online ? " online" : ""}`}>
          <span className="botpanel-status-dot" aria-hidden="true" />
          {online ? "Đang hoạt động" : "Ngoại tuyến"}
        </span>
        {bot.description.trim() && (
          <p className="botpanel-desc">{bot.description}</p>
        )}
      </div>

      <div className="botpanel-section">
        <h3 className="botpanel-section-title">
          <Wrench size={14} aria-hidden="true" />
          Tools được phép dùng
        </h3>
        {tools.length === 0 ? (
          <p className="botpanel-note">Được dùng tất cả tools</p>
        ) : (
          <div className="botpanel-chips" role="list" aria-label="Tools được phép">
            {tools.map((t) => (
              <Chip key={t} label={t} />
            ))}
          </div>
        )}
      </div>

      {channels.length > 0 && (
        <div className="botpanel-section">
          <h3 className="botpanel-section-title">
            <Radio size={14} aria-hidden="true" />
            Kênh đã kết nối
          </h3>
          <div className="botpanel-chips" role="list" aria-label="Kênh đã kết nối">
            {channels.map((c) => (
              <Chip key={c} label={c} />
            ))}
          </div>
        </div>
      )}

      {bot.system_prompt.trim() && (
        <div className="botpanel-section">
          <h3 className="botpanel-section-title">System prompt</h3>
          <p className="botpanel-prompt">{bot.system_prompt}</p>
        </div>
      )}

      <div className="botpanel-footer">
        <button
          type="button"
          className="botpanel-edit-btn"
          onClick={onEditAdvanced}
        >
          <Settings2 size={15} aria-hidden="true" />
          Chỉnh sửa nâng cao
        </button>
      </div>
    </aside>
  );
}
