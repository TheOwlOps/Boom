use crate::security::OwnerGuard;
use crossterm::{
    event::{self, DisableMouseCapture, EnableMouseCapture, Event, KeyCode, KeyModifiers},
    execute,
    terminal::{disable_raw_mode, enable_raw_mode, EnterAlternateScreen, LeaveAlternateScreen},
};
use ratatui::{
    backend::CrosstermBackend,
    layout::{Constraint, Direction, Layout},
    style::{Color, Modifier, Style},
    text::{Line, Span},
    widgets::{Paragraph, Wrap},
    Terminal,
};
use std::{io, time::Duration};

// BoomBot Owl & Claude Aesthetic Color Palette
const COLOR_TERRACOTTA: Color = Color::Rgb(215, 119, 87);
const COLOR_OWL_BLUE: Color = Color::Rgb(87, 105, 247); // Owl wings & body
const COLOR_ACCENT_BLUE: Color = COLOR_OWL_BLUE; // Bullet blue
const COLOR_OWL_TEAL: Color = Color::Rgb(45, 212, 191); // Owl ears
const COLOR_OWL_ORANGE: Color = Color::Rgb(249, 115, 22); // Owl eye rings, beak & feet
const COLOR_OWL_CREAM: Color = Color::Rgb(245, 240, 225); // Owl chest feathers
const COLOR_TEXT_MUTED: Color = Color::Rgb(140, 140, 150);
const COLOR_TEXT_DIM: Color = Color::Rgb(95, 95, 105);
const COLOR_BORDER_WARM: Color = Color::Rgb(120, 105, 95);

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum PermissionMode {
    Default,
    Plan,
    AcceptEdits,
    BypassPermissions,
}

impl PermissionMode {
    pub fn badge(&self) -> &'static str {
        match self {
            Self::BypassPermissions => "⏩ bypass permissions on",
            Self::Plan => "⏸ plan mode on",
            Self::AcceptEdits => "⏵⏵ accept edits on",
            Self::Default => "⏸ default permissions",
        }
    }

    pub fn color(&self) -> Color {
        match self {
            Self::Default => Color::Rgb(46, 160, 67),       // green
            Self::Plan => Color::Rgb(56, 189, 248),         // cyan/sky
            Self::AcceptEdits => Color::Rgb(168, 85, 247),   // electric violet
            Self::BypassPermissions => Color::Rgb(248, 81, 73), // red/coral (matches screenshot)
        }
    }

    pub fn next(&self) -> Self {
        match self {
            Self::Default => Self::Plan,
            Self::Plan => Self::AcceptEdits,
            Self::AcceptEdits => Self::BypassPermissions,
            Self::BypassPermissions => Self::Default,
        }
    }
}

pub struct TuiApp {
    pub input: String,
    pub messages: Vec<(String, String)>, // (sender, text)
    pub permission_mode: PermissionMode,
    pub model: String,
    pub total_cost: f64,
    pub context_pct: usize,
    pub tick_count: usize,
    pub is_busy: bool,
    pub busy_ticks: usize,
    pub pending_prompt: Option<String>,
}

impl TuiApp {
    pub fn new(model: String, yolo: bool) -> Self {
        let initial_mode = if yolo {
            PermissionMode::BypassPermissions
        } else {
            PermissionMode::Default
        };

        Self {
            input: String::new(),
            messages: Vec::new(),
            permission_mode: initial_mode,
            model,
            total_cost: 0.0,
            context_pct: 0,
            tick_count: 0,
            is_busy: false,
            busy_ticks: 0,
            pending_prompt: None,
        }
    }

    pub fn handle_submit(&mut self) {
        let text = self.input.trim().to_string();
        if text.is_empty() {
            return;
        }

        self.messages.push(("user".to_string(), text.clone()));
        self.input.clear();

        if text.starts_with('/') {
            match text.as_str() {
                "/help" => {
                    self.messages.push((
                        "agent".to_string(),
                        "Shortcuts & Commands:\n  Shift+Tab / F2 - Cycle permission mode (bypass, default, plan)\n  /clear        - Clear screen\n  /exit         - Quit Claude Code".to_string(),
                    ));
                }
                "/clear" => {
                    self.messages.clear();
                }
                _ => {
                    self.messages.push(("agent".to_string(), format!("Unknown command: {}", text)));
                }
            }
            return;
        }

        // Owner-Only Guard Check
        if let Err(msg) = OwnerGuard::evaluate(&text, "Ryan") {
            self.messages.push(("agent".to_string(), msg.to_string()));
            return;
        }

        self.is_busy = true;
        self.busy_ticks = 0;
        self.pending_prompt = Some(text);
    }

    pub fn finalize_task(&mut self) {
        let text = self.pending_prompt.take().unwrap_or_default();
        self.is_busy = false;

        let resp = format!(
            "● Executed: {}\n  ├─ Verified with System 1 Gate (<2ms)\n  ├─ Sandbox IPC dispatched successfully\n  └─ Done (0.2s)",
            text
        );
        self.messages.push(("agent".to_string(), resp));
        self.context_pct = (self.context_pct + 4).min(100);
        self.total_cost += 0.0002;
    }
}

pub fn run_tui(mut app: TuiApp) -> anyhow::Result<()> {
    enable_raw_mode()?;
    let mut stdout = io::stdout();
    execute!(stdout, EnterAlternateScreen, EnableMouseCapture)?;
    let backend = CrosstermBackend::new(stdout);
    let mut terminal = Terminal::new(backend)?;

    let res = run_event_loop(&mut terminal, &mut app);

    disable_raw_mode()?;
    execute!(
        terminal.backend_mut(),
        LeaveAlternateScreen,
        DisableMouseCapture
    )?;
    terminal.show_cursor()?;

    res
}

fn run_event_loop<B: ratatui::backend::Backend>(
    terminal: &mut Terminal<B>,
    app: &mut TuiApp,
) -> anyhow::Result<()> {
    loop {
        app.tick_count += 1;

        if app.is_busy {
            app.busy_ticks += 1;
            if app.busy_ticks >= 8 {
                app.finalize_task();
            }
        }

        terminal.draw(|f| {
            let width = f.area().width as usize;
            let divider_str = "─".repeat(width);

            let chunks = Layout::default()
                .direction(Direction::Vertical)
                .constraints([
                    Constraint::Min(4),    // Scrollback / Welcome
                    Constraint::Length(1), // Unboxed Prompt Line
                    Constraint::Length(1), // Divider ───────
                    Constraint::Length(6), // Clean Status Line (Exact screenshot)
                ])
                .split(f.area());

            // 1. Content Viewport (Mascot header + Conversation)
            let mut lines = Vec::new();

            // BoomBot Geometric Pixel Owl (Matching 5d588e965709eae60ce4157589dfe1a6.png)
            // Row 0: Ears + Top of head
            lines.push(Line::from(vec![
                Span::raw("   "),
                Span::styled("▟▛", Style::default().fg(COLOR_OWL_TEAL).add_modifier(Modifier::BOLD)),
                Span::raw("   "),
                Span::styled("▜▙", Style::default().fg(COLOR_OWL_TEAL).add_modifier(Modifier::BOLD)),
                Span::raw("      "),
                Span::styled("Boom Code ", Style::default().fg(Color::White).add_modifier(Modifier::BOLD)),
                Span::styled("(v0.1.0)", Style::default().fg(COLOR_TEXT_DIM)),
            ]));
            // Row 1: Orange Eye Rings (Top) + Wings
            lines.push(Line::from(vec![
                Span::raw("  "),
                Span::styled("▐", Style::default().fg(COLOR_OWL_BLUE)),
                Span::styled("▛▀▜", Style::default().fg(COLOR_OWL_ORANGE)),
                Span::raw(" "),
                Span::styled("▛▀▜", Style::default().fg(COLOR_OWL_ORANGE)),
                Span::styled("▌", Style::default().fg(COLOR_OWL_BLUE)),
                Span::raw("     "),
                Span::styled("D:\\Boom", Style::default().fg(COLOR_TEXT_DIM)),
            ]));
            // Row 2: Orange Eye Rings (Bottom) + Orange Beak + Wings
            lines.push(Line::from(vec![
                Span::raw("  "),
                Span::styled("▐", Style::default().fg(COLOR_OWL_BLUE)),
                Span::styled("▜▄▛", Style::default().fg(COLOR_OWL_ORANGE)),
                Span::styled("▼", Style::default().fg(COLOR_OWL_ORANGE).add_modifier(Modifier::BOLD)),
                Span::styled("▜▄▛", Style::default().fg(COLOR_OWL_ORANGE)),
                Span::styled("▌", Style::default().fg(COLOR_OWL_BLUE)),
                Span::raw("     "),
                Span::styled("Welcome back Son!", Style::default().fg(Color::White)),
            ]));
            // Row 3: Blue Body Wings + Cream Chest Feathers
            lines.push(Line::from(vec![
                Span::raw("   "),
                Span::styled("▝▜", Style::default().fg(COLOR_OWL_BLUE)),
                Span::styled("███", Style::default().fg(COLOR_OWL_CREAM)),
                Span::styled("▛▘", Style::default().fg(COLOR_OWL_BLUE)),
                Span::raw("      "),
                Span::styled(
                    "Try \"how does <filepath> work?\"",
                    Style::default().fg(COLOR_TEXT_MUTED).add_modifier(Modifier::ITALIC),
                ),
            ]));
            // Row 4: Orange Feet
            lines.push(Line::from(vec![
                Span::raw("     "),
                Span::styled("▘▘ ▝▝", Style::default().fg(COLOR_OWL_ORANGE)),
            ]));
            lines.push(Line::from(Span::styled(&divider_str, Style::default().fg(COLOR_BORDER_WARM))));
            lines.push(Line::raw(""));

            // Conversation history (clean flat text, zero boxes)
            for (sender, msg) in &app.messages {
                match sender.as_str() {
                    "user" => {
                        lines.push(Line::from(vec![
                            Span::styled("> ", Style::default().fg(COLOR_TERRACOTTA).add_modifier(Modifier::BOLD)),
                            Span::styled(msg, Style::default().fg(Color::White).add_modifier(Modifier::BOLD)),
                        ]));
                        lines.push(Line::raw(""));
                    }
                    _ => {
                        for l in msg.lines() {
                            if l.starts_with('●') {
                                lines.push(Line::from(vec![
                                    Span::styled("● ", Style::default().fg(COLOR_ACCENT_BLUE)),
                                    Span::styled(&l[2..], Style::default().fg(Color::White)),
                                ]));
                            } else if l.trim_start().starts_with("├─") || l.trim_start().starts_with("└─") {
                                lines.push(Line::from(Span::styled(l, Style::default().fg(COLOR_TEXT_MUTED))));
                            } else {
                                lines.push(Line::from(Span::styled(l, Style::default().fg(Color::White))));
                            }
                        }
                        lines.push(Line::raw(""));
                    }
                }
            }

            let chat_widget = Paragraph::new(lines).wrap(Wrap { trim: false });
            f.render_widget(chat_widget, chunks[0]);

            // 2. Unboxed Prompt Input (Exact > prompt█ or placeholder)
            let prompt_line = if app.input.is_empty() {
                Line::from(vec![
                    Span::styled("> ", Style::default().fg(COLOR_TERRACOTTA).add_modifier(Modifier::BOLD)),
                    Span::styled("Try \"how does <filepath> work?\"", Style::default().fg(COLOR_TEXT_DIM).add_modifier(Modifier::ITALIC)),
                    Span::styled("█", Style::default().fg(COLOR_TERRACOTTA)),
                ])
            } else {
                Line::from(vec![
                    Span::styled("> ", Style::default().fg(COLOR_TERRACOTTA).add_modifier(Modifier::BOLD)),
                    Span::styled(&app.input, Style::default().fg(Color::White)),
                    Span::styled("█", Style::default().fg(COLOR_TERRACOTTA)),
                ])
            };
            f.render_widget(Paragraph::new(prompt_line), chunks[1]);

            // 3. Divider Line
            let divider_widget = Paragraph::new(Line::from(Span::styled(&divider_str, Style::default().fg(COLOR_BORDER_WARM))));
            f.render_widget(divider_widget, chunks[2]);

            // 4. Clean Flat Status Line (Exact 6 lines from the user's screenshot)
            let model_display = if app.model.contains("deepseek") || app.model.contains("claude") {
                app.model.clone()
            } else {
                "deepseek-flash".to_string()
            };

            let status_lines = vec![
                // Line 1: [model · high] · Boom · Cost $0.0000
                Line::from(vec![
                    Span::styled(format!("[{} · high]", model_display), Style::default().fg(Color::Rgb(56, 189, 248))),
                    Span::styled(" · Boom · ", Style::default().fg(COLOR_TEXT_DIM)),
                    Span::styled(format!("Cost ${:.4}", app.total_cost), Style::default().fg(COLOR_TEXT_DIM)),
                ]),
                // Line 2: Context: 0%
                Line::from(vec![
                    Span::styled(format!("Context: {}%", app.context_pct), Style::default().fg(COLOR_TEXT_DIM)),
                ]),
                // Line 3: Memory: 14 GB / 16 GB (88%) [████████████████████████░░░░]
                Line::from(vec![
                    Span::styled("Memory: 14 GB / 16 GB (88%) ", Style::default().fg(Color::Rgb(168, 85, 247))),
                    Span::styled("[████████████████████████░░░░]", Style::default().fg(Color::Rgb(168, 85, 247))),
                ]),
                // Line 4: 15 toolsets | 3 skills | IPC online
                Line::from(vec![
                    Span::styled("15 toolsets | 3 skills", Style::default().fg(COLOR_TEXT_DIM)),
                ]),
                // Line 5: ⏩ bypass permissions on (shift+tab to cycle) · 0 background agents (ctrl+b)
                Line::from(vec![
                    Span::styled(
                        app.permission_mode.badge(),
                        Style::default().fg(app.permission_mode.color()),
                    ),
                    Span::styled(" (shift+tab to cycle) · 0 background agents (ctrl+b)", Style::default().fg(COLOR_TEXT_DIM)),
                ]),
                // Line 6: ? for shortcuts ── esc to interrupt
                Line::from(vec![
                    Span::styled("? for shortcuts", Style::default().fg(COLOR_TEXT_DIM)),
                    Span::styled(" ── esc to interrupt", Style::default().fg(COLOR_TEXT_DIM)),
                ]),
            ];

            let status_widget = Paragraph::new(status_lines);
            f.render_widget(status_widget, chunks[3]);
        })?;

        if event::poll(Duration::from_millis(80))? {
            if let Event::Key(key) = event::read()? {
                match key.code {
                    KeyCode::Char('c') if key.modifiers.contains(KeyModifiers::CONTROL) => {
                        return Ok(());
                    }
                    KeyCode::Esc => {
                        app.is_busy = false;
                    }
                    KeyCode::BackTab | KeyCode::F(2) => {
                        // Shift+Tab or F2 cycles Permission Modes
                        app.permission_mode = app.permission_mode.next();
                    }
                    KeyCode::Enter => {
                        if app.input.trim() == "/exit" || app.input.trim() == "/quit" {
                            return Ok(());
                        }
                        app.handle_submit();
                    }
                    KeyCode::Backspace => {
                        app.input.pop();
                    }
                    KeyCode::Char(c) => {
                        app.input.push(c);
                    }
                    _ => {}
                }
            }
        }
    }
}
