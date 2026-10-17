//! Multi-agent group mechanics: task handoff parsing and Chief-of-Staff
//! orchestration helpers.
//!
//! ## Handoff format
//!
//! A bot delegates work to another bot by emitting, anywhere in its reply:
//!
//! ```text
//! [HANDOFF:@bot_name] <task description>
//! ```
//!
//! [`parse_handoffs`] extracts every such directive. Name matching is
//! case-insensitive and also matches on the bot's id (so `@coder` finds a
//! bot named "Coder" or with id "coder").
//!
//! ## Chief-of-Staff
//!
//! Each group has one chief (first member by default). When the user sends a
//! message, the chief triages it: answer directly, or delegate to one or
//! more specialists via handoff directives. [`chief_system_prompt`] builds
//! the chief's augmented system prompt from the member roster.

use regex::Regex;
use std::sync::OnceLock;

/// One parsed delegation directive.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Handoff {
    /// The raw `@name` as written (without the `@`).
    pub target_name: String,
    /// The task description following the directive.
    pub task: String,
}

fn handoff_re() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| {
        Regex::new(r"(?m)\[HANDOFF:@([A-Za-z0-9_\-]+)\]\s*(.+)").expect("handoff regex")
    })
}

/// Extract every `[HANDOFF:@name] task` directive from `text`, in order.
/// The task is the remainder of the line; surrounding whitespace is trimmed.
/// Directives with an empty task are ignored.
pub fn parse_handoffs(text: &str) -> Vec<Handoff> {
    handoff_re()
        .captures_iter(text)
        .filter_map(|cap| {
            let target_name = cap.get(1)?.as_str().trim().to_string();
            let task = cap.get(2)?.as_str().trim().to_string();
            if target_name.is_empty() || task.is_empty() {
                return None;
            }
            Some(Handoff { target_name, task })
        })
        .collect()
}

/// Resolve a handoff target against a roster of `(id, name)` pairs.
/// Matches case-insensitively on name first, then on id. Returns the bot id.
pub fn resolve_handoff_target(target: &str, roster: &[(String, String)]) -> Option<String> {
    let want = target.to_lowercase();
    // Name match first (user-facing), then id match.
    roster
        .iter()
        .find(|(_, name)| name.to_lowercase() == want)
        .map(|(id, _)| id.clone())
        .or_else(|| {
            roster
                .iter()
                .find(|(id, _)| id.to_lowercase() == want)
                .map(|(id, _)| id.clone())
        })
}

/// One line of the member roster shown to the chief / specialists.
pub fn roster_line(id: &str, name: &str, description: &str, is_chief: bool) -> String {
    let role = if description.trim().is_empty() {
        "general assistant".to_string()
    } else {
        description.trim().to_string()
    };
    format!(
        "- @{name} (id: {id}){}{}: {role}",
        if is_chief { " [CHIEF]" } else { "" },
        "",
    )
}

/// Augmented system prompt for the chief of a group.
///
/// The chief triages the user's request: answer directly when it is simple,
/// or delegate to specialists with `[HANDOFF:@name] task` directives (one
/// per line, each on its own line). After specialists reply, the chief
/// synthesizes a single final answer for the user.
pub fn chief_system_prompt(
    chief_name: &str,
    group_name: &str,
    roster: &str,
    base_prompt: &str,
) -> String {
    format!(
        "{base_prompt}\n\n\
         You are the CHIEF OF STAFF of the group \"{group_name}\". {chief_name} triages every user request.\n\
         \n\
         TEAM ROSTER (specialists you can delegate to):\n{roster}\n\
         \n\
         PROTOCOL:\n\
         1. Read the user's request. If you can answer it fully yourself, just answer — no handoff needed.\n\
         2. If a specialist's expertise fits better, delegate with a handoff directive on its own line:\n\
            [HANDOFF:@bot_name] <clear task description>\n\
            You may delegate to several specialists (one directive per line).\n\
         3. Keep any direct reply brief; the specialists' results will be synthesized after.\n\
         4. Never invent a @name that is not on the roster. Never hand off to yourself.\n\
         5. When replying to the user directly, do NOT include the handoff directives in the visible answer — put them at the very end of your message after your reply, each on its own line."
    )
}

/// Augmented system prompt for a specialist inside a group: it sees the
/// roster so it can hand work off to a teammate when the task belongs to
/// someone else's expertise.
pub fn specialist_group_prompt(
    bot_name: &str,
    group_name: &str,
    roster: &str,
    base_prompt: &str,
) -> String {
    format!(
        "{base_prompt}\n\n\
         You are chatting in the group \"{group_name}\" as {bot_name}.\n\
         \n\
         TEAMMATES (you may hand work off to them when it fits their expertise better):\n{roster}\n\
         \n\
         To hand off, put on its own line at the very end of your message:\n\
         [HANDOFF:@bot_name] <clear task description>\n\
         Never hand off to yourself. If you hand off, still give a one-line summary of what you did first.\n\
         \n\
         SHARED FILES: all bots in this group share the directory /workspace/ (use the shared_write / shared_read tools).\n\
         When you save a file for another bot, write the file first, then end your message with:\n\
         📁 Saved to /workspace/<filename>"
    )
}

/// Strip handoff directive lines from a reply before showing it to the user,
/// so the routing syntax never leaks into the visible chat.
pub fn strip_handoff_lines(text: &str) -> String {
    let re = handoff_re();
    let stripped: Vec<&str> = text
        .lines()
        .filter(|line| !re.is_match(line.trim()))
        .collect();
    let joined = stripped.join("\n");
    // Collapse 3+ blank lines left behind by stripped directives.
    let mut out = String::with_capacity(joined.len());
    let mut blanks = 0;
    for line in joined.lines() {
        if line.trim().is_empty() {
            blanks += 1;
            if blanks <= 2 {
                out.push('\n');
            }
        } else {
            blanks = 0;
            out.push_str(line);
            out.push('\n');
        }
    }
    out.trim_end().to_string()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_single_handoff() {
        let h = parse_handoffs("Let me check.\n[HANDOFF:@coder] write the parser\nDone.");
        assert_eq!(
            h,
            vec![Handoff {
                target_name: "coder".to_string(),
                task: "write the parser".to_string(),
            }]
        );
    }

    #[test]
    fn parses_multiple_handoffs_in_order() {
        let h = parse_handoffs(
            "[HANDOFF:@qa] test it\nsome text\n[HANDOFF:@writer] document it",
        );
        assert_eq!(h.len(), 2);
        assert_eq!(h[0].target_name, "qa");
        assert_eq!(h[1].target_name, "writer");
    }

    #[test]
    fn ignores_empty_task_and_bad_names() {
        assert!(parse_handoffs("[HANDOFF:@coder]   ").is_empty());
        assert!(parse_handoffs("[HANDOFF:] task").is_empty());
        assert!(parse_handoffs("no directives here").is_empty());
        // Name with hyphen/underscore is fine.
        let h = parse_handoffs("[HANDOFF:@my-bot_2] do it");
        assert_eq!(h[0].target_name, "my-bot_2");
    }

    #[test]
    fn resolves_target_by_name_then_id() {
        let roster = vec![
            ("b1".to_string(), "Coder".to_string()),
            ("b2".to_string(), "writer".to_string()),
        ];
        assert_eq!(
            resolve_handoff_target("coder", &roster),
            Some("b1".to_string())
        );
        assert_eq!(
            resolve_handoff_target("CODER", &roster),
            Some("b1".to_string())
        );
        assert_eq!(
            resolve_handoff_target("b2", &roster),
            Some("b2".to_string())
        );
        assert_eq!(resolve_handoff_target("ghost", &roster), None);
    }

    #[test]
    fn strips_handoff_lines_but_keeps_reply() {
        let out = strip_handoff_lines("Here is my answer.\n[HANDOFF:@coder] do the thing\nThanks!");
        assert_eq!(out, "Here is my answer.\nThanks!");
    }

    #[test]
    fn strip_keeps_text_without_directives() {
        let text = "Just a normal reply.\nSecond line.";
        assert_eq!(strip_handoff_lines(text), text);
    }

    #[test]
    fn chief_prompt_contains_roster_and_protocol() {
        let p = chief_system_prompt("Chief", "Team", "- @coder: codes", "Be helpful.");
        assert!(p.contains("CHIEF OF STAFF"));
        assert!(p.contains("@coder"));
        assert!(p.contains("[HANDOFF:@bot_name]"));
    }
}
