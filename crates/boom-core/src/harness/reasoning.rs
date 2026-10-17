//! Reasoning strategies that sit on top of the model seam.
//!
//! Ports of `agent/src/reasoning/`:
//! - `cot.py` — [`ChainOfThought`]: one model call per reasoning step, then a
//!   synthesis call for the final answer.
//! - `verify.py` — [`verify_answer`]: a critic hunts for errors round after
//!   round; a revision fixes the answer when the critic finds one.
//! - `self_consistency.py` — [`vote`]: ask N times (with a "try a different
//!   approach" nudge from round 2), extract each answer, majority wins.
//!
//! The Python versions take a sync `llm_fn(prompt) -> str`; the Rust versions
//! take `&dyn LlmClient` and are async. The prompts keep the Python phrasing
//! (including Vietnamese) so behavior stays comparable.

use super::types::{AgentError, LlmClient, Message};

// ---------------------------------------------------------------------------
// Answer extraction
// ---------------------------------------------------------------------------

/// Extract the answer from one model response.
///
/// Prefers a line starting with `Đáp án:` / `Answer:` (case-insensitive);
/// falls back to the whole stripped response — never invents an answer.
/// (Port of the `_ANSWER_RE` logic in `cot.py` / `self_consistency.py`,
/// without the `regex` crate.)
pub fn extract_answer(response: &str) -> String {
    let text = response.trim();
    for line in text.lines() {
        let t = line.trim();
        let lower = t.to_lowercase();
        for prefix in ["đáp án:", "dap an:", "answer:"] {
            if lower.starts_with(prefix) {
                return t[prefix.len()..].trim().to_string();
            }
        }
    }
    text.to_string()
}

/// True when the critic's verdict means "no error found".
/// (Port of `_NO_ERROR_RE` in `verify.py`; Vietnamese + English.)
fn is_no_error(critique: &str) -> bool {
    let lower = critique.to_lowercase();
    const PHRASES: &[&str] = &[
        "không có lỗi",
        "không thấy lỗi",
        "no issues found",
        "no issue found",
        "no errors found",
        "no error found",
        "no problems found",
        "no problem found",
        "no issues detected",
        "no errors detected",
    ];
    PHRASES.iter().any(|p| lower.contains(p))
}

async fn ask(llm: &dyn LlmClient, prompt: &str) -> Result<String, AgentError> {
    llm.complete(&[Message::user(prompt)])
        .await
        .map_err(|e| AgentError::Llm(e.to_string()))
}

// ---------------------------------------------------------------------------
// Chain-of-Thought
// ---------------------------------------------------------------------------

/// One reasoning step of a [`ChainOfThought::think`] run.
#[derive(Debug, Clone)]
pub struct CotStep {
    pub step: usize,
    pub reasoning: String,
}

/// Result of [`ChainOfThought::think`].
#[derive(Debug, Clone)]
pub struct CotResult {
    pub steps: Vec<CotStep>,
    pub answer: String,
}

/// Step-by-step reasoning: `max_steps` model calls building on each other's
/// history, then one synthesis call for the final answer.
pub struct ChainOfThought;

impl ChainOfThought {
    /// Run CoT: `max_steps` reasoning steps + 1 synthesis.
    pub async fn think(
        llm: &dyn LlmClient,
        problem: &str,
        max_steps: usize,
    ) -> Result<CotResult, AgentError> {
        if max_steps < 1 {
            return Err(AgentError::InvalidPlan(
                "max_steps must be >= 1".to_string(),
            ));
        }
        let mut steps = Vec::with_capacity(max_steps);
        let mut history: Vec<String> = Vec::with_capacity(max_steps);
        for n in 1..=max_steps {
            let reasoning = ask(llm, &Self::step_prompt(problem, &history, n, max_steps))
                .await?
                .trim()
                .to_string();
            history.push(reasoning.clone());
            steps.push(CotStep { step: n, reasoning });
        }
        let raw = ask(llm, &Self::summary_prompt(problem, &history)).await?;
        Ok(CotResult { steps, answer: extract_answer(&raw) })
    }

    fn step_prompt(problem: &str, history: &[String], step: usize, max_steps: usize) -> String {
        let mut p = format!("Bài toán: {problem}\n\n");
        if !history.is_empty() {
            p.push_str("Các bước đã suy nghĩ:\n");
            for (i, h) in history.iter().enumerate() {
                p.push_str(&format!("- Bước {}: {h}\n", i + 1));
            }
            p.push('\n');
        }
        p.push_str(&format!(
            "Bước {step} (trong tối đa {max_steps} bước): \
             Viết suy luận tiếp theo, chỉ tập trung vào bước này."
        ));
        p
    }

    fn summary_prompt(problem: &str, steps: &[String]) -> String {
        let mut p = format!("Bài toán: {problem}\n\nCác bước suy luận:\n");
        for (i, s) in steps.iter().enumerate() {
            p.push_str(&format!("{}. {s}\n", i + 1));
        }
        p.push_str(
            "\nDựa vào các bước trên, cho đáp án cuối cùng, \
             bắt đầu dòng đáp án bằng \"Đáp án:\".",
        );
        p
    }
}

// ---------------------------------------------------------------------------
// Verify: critic → revise loop
// ---------------------------------------------------------------------------

/// One round of a [`verify_answer`] run.
#[derive(Debug, Clone)]
pub struct VerifyRound {
    pub round: usize,
    pub kind: VerifyKind,
    /// Initial/revise: the answer. Critic: the critique text.
    pub text: String,
    /// Critic rounds only: did the critic find an error?
    pub found_error: bool,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum VerifyKind {
    Initial,
    Critic,
    Revise,
}

/// Result of [`verify_answer`].
#[derive(Debug, Clone)]
pub struct VerifyResult {
    pub final_answer: String,
    pub rounds: Vec<VerifyRound>,
    pub revised: bool,
}

/// Multi-round verify: the model answers, then plays critic
/// ("find the error in this answer"). A clean verdict stops the loop early;
/// a found error triggers a revision round, up to `max_rounds` critic rounds.
///
/// Mirrors `verify_answer` in `verify.py`, including its round numbering
/// (critic and revise rounds each bump the counter).
pub async fn verify_answer(
    llm: &dyn LlmClient,
    problem: &str,
    max_rounds: usize,
) -> Result<VerifyResult, AgentError> {
    if max_rounds < 1 {
        return Err(AgentError::InvalidPlan(
            "max_rounds must be >= 1".to_string(),
        ));
    }
    let mut current = ask(
        llm,
        &format!("Bài toán: {problem}\nCho đáp án cuối, bắt đầu bằng \"Đáp án:\"."),
    )
    .await?
    .trim()
    .to_string();

    let mut rounds = vec![VerifyRound {
        round: 1,
        kind: VerifyKind::Initial,
        text: current.clone(),
        found_error: false,
    }];
    let mut revised = false;
    let mut round_no = 1usize;

    for _ in 0..max_rounds.saturating_sub(1) {
        round_no += 1;
        let critique = ask(llm, &critic_prompt(problem, &current)).await?.trim().to_string();
        let found_error = !is_no_error(&critique);
        rounds.push(VerifyRound {
            round: round_no,
            kind: VerifyKind::Critic,
            text: critique.clone(),
            found_error,
        });
        if !found_error {
            // Clean verdict: stop early, no wasted revise call.
            break;
        }
        round_no += 1;
        current = ask(llm, &revise_prompt(problem, &current, &critique))
            .await?
            .trim()
            .to_string();
        revised = true;
        rounds.push(VerifyRound {
            round: round_no,
            kind: VerifyKind::Revise,
            text: current.clone(),
            found_error: false,
        });
    }

    Ok(VerifyResult { final_answer: current, rounds, revised })
}

fn critic_prompt(problem: &str, answer: &str) -> String {
    format!(
        "Bài toán: {problem}\n\n\
         Đáp án được đề xuất: {answer}\n\n\
         Tìm lỗi trong đáp án sau: kiểm tra kỹ lập luận và kết quả. \
         Nếu không thấy lỗi, chỉ trả lời đúng cụm \"KHÔNG CÓ LỖI\". \
         Nếu có lỗi, mô tả rõ lỗi ở đâu và vì sao."
    )
}

fn revise_prompt(problem: &str, answer: &str, critique: &str) -> String {
    format!(
        "Bài toán: {problem}\n\n\
         Đáp án hiện tại: {answer}\n\n\
         Critic phát hiện lỗi: {critique}\n\n\
         Sửa lại đáp án cho đúng dựa trên lỗi đã chỉ ra. \
         Trả lời trực tiếp đáp án đúng, bắt đầu bằng \"Đáp án:\"."
    )
}

// ---------------------------------------------------------------------------
// Self-consistency vote
// ---------------------------------------------------------------------------

/// Result of [`vote`].
#[derive(Debug, Clone)]
pub struct VoteResult {
    pub answers: Vec<String>,
    /// `(answer, count)` in first-appearance order.
    pub votes: Vec<(String, usize)>,
    pub winner: String,
}

/// Ask the model `n` times (nudging it to try a different approach from
/// round 2 on, simulating high temperature), extract each round's answer,
/// and return the majority vote. Ties go to the earliest-appearing answer —
/// stable, no randomness. (Port of `vote` in `self_consistency.py`.)
pub async fn vote(
    llm: &dyn LlmClient,
    problem: &str,
    n: usize,
) -> Result<VoteResult, AgentError> {
    if n < 1 {
        return Err(AgentError::InvalidPlan("n must be >= 1".to_string()));
    }
    let base = format!("Bài toán: {problem}\nCho đáp án cuối, bắt đầu bằng \"Đáp án:\".");
    let mut answers = Vec::with_capacity(n);
    for i in 0..n {
        let prompt = if i == 0 {
            base.clone()
        } else {
            format!("Hãy thử cách khác:\n{base}")
        };
        answers.push(extract_answer(&ask(llm, &prompt).await?));
    }

    // Ordered counts: first appearance wins ties (like Counter.most_common).
    let mut votes: Vec<(String, usize)> = Vec::new();
    for a in &answers {
        match votes.iter_mut().find(|(ans, _)| ans == a) {
            Some((_, c)) => *c += 1,
            None => votes.push((a.clone(), 1)),
        }
    }
    // Strictly-greater scan: the first answer to reach the max keeps the
    // crown on ties (Counter.most_common keeps first-appearance order).
    let mut winner = String::new();
    let mut best = 0usize;
    for (a, c) in &votes {
        if *c > best {
            best = *c;
            winner = a.clone();
        }
    }

    Ok(VoteResult { answers, votes, winner })
}

// ---------------------------------------------------------------------------
// Tests (mock LlmClient — no real model needed)
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use async_trait::async_trait;
    use crate::harness::types::Message;
    use std::collections::VecDeque;
    use std::sync::{Arc, Mutex};

    /// Scripted model: pops canned replies; records prompts for assertions.
    struct ScriptLlm {
        replies: Mutex<VecDeque<String>>,
        prompts: Mutex<Vec<String>>,
    }

    impl ScriptLlm {
        fn of(replies: &[&str]) -> Arc<Self> {
            Arc::new(Self {
                replies: Mutex::new(replies.iter().map(|s| s.to_string()).collect()),
                prompts: Mutex::new(Vec::new()),
            })
        }
        fn prompts(&self) -> Vec<String> {
            self.prompts.lock().unwrap().clone()
        }
    }

    #[async_trait]
    impl LlmClient for ScriptLlm {
        async fn complete(&self, messages: &[Message]) -> Result<String, AgentError> {
            let prompt = messages.last().map(|m| m.content.clone()).unwrap_or_default();
            self.prompts.lock().unwrap().push(prompt);
            self.replies
                .lock()
                .unwrap()
                .pop_front()
                .ok_or_else(|| AgentError::Llm("script exhausted".into()))
        }
    }

    // -- extract_answer --------------------------------------------------

    #[test]
    fn extract_answer_prefers_dap_an_line() {
        assert_eq!(extract_answer("suy luận...\nĐáp án: A > C"), "A > C");
        assert_eq!(extract_answer("reasoning\nAnswer: 24"), "24");
    }

    #[test]
    fn extract_answer_falls_back_to_whole_text() {
        assert_eq!(extract_answer("  just 42  "), "just 42");
    }

    #[test]
    fn is_no_error_detects_both_languages() {
        assert!(is_no_error("KHÔNG CÓ LỖI"));
        assert!(is_no_error("Tôi không thấy lỗi nào."));
        assert!(is_no_error("No errors found."));
        assert!(!is_no_error("Lỗi ở bước 2: sai dấu."));
        assert!(!is_no_error("There is a problem with step 1."));
    }

    // -- ChainOfThought ----------------------------------------------------

    #[tokio::test]
    async fn cot_runs_steps_then_synthesizes() {
        let llm = ScriptLlm::of(&[
            "vì A>B nên A lớn hơn B",
            "vì B>C nên B lớn hơn C",
            "kết luận:\nĐáp án: A > C",
        ]);
        let r = ChainOfThought::think(&*llm, "so sánh A và C", 2).await.unwrap();
        assert_eq!(r.steps.len(), 2);
        assert_eq!(r.steps[0].step, 1);
        assert!(r.steps[1].reasoning.contains("B>C"));
        assert_eq!(r.answer, "A > C");
        // 2 step prompts + 1 summary prompt
        assert_eq!(llm.prompts().len(), 3);
        assert!(llm.prompts()[2].contains("Đáp án:"));
    }

    #[tokio::test]
    async fn cot_rejects_zero_steps() {
        let llm = ScriptLlm::of(&[]);
        assert!(ChainOfThought::think(&*llm, "x", 0).await.is_err());
    }

    // -- verify_answer ------------------------------------------------------

    #[tokio::test]
    async fn verify_stops_early_on_clean_critique() {
        let llm = ScriptLlm::of(&["Đáp án: 24", "KHÔNG CÓ LỖI"]);
        let r = verify_answer(&*llm, "7 + 5 x 2", 3).await.unwrap();
        assert!(!r.revised);
        assert_eq!(r.final_answer, "Đáp án: 24");
        assert_eq!(r.rounds.len(), 2);
        assert_eq!(r.rounds[1].kind, VerifyKind::Critic);
        assert!(!r.rounds[1].found_error);
    }

    #[tokio::test]
    async fn verify_revises_when_critic_finds_error() {
        let llm = ScriptLlm::of(&[
            "Đáp án: 24",
            "Lỗi: nhân chia trước cộng trừ, 5x2=10, 7+10=17",
            "Đáp án: 17",
            "KHÔNG CÓ LỖI",
        ]);
        let r = verify_answer(&*llm, "7 + 5 x 2", 3).await.unwrap();
        assert!(r.revised);
        assert_eq!(r.final_answer, "Đáp án: 17");
        assert_eq!(r.rounds.len(), 4);
        assert!(r.rounds[1].found_error);
        assert_eq!(r.rounds[2].kind, VerifyKind::Revise);
    }

    #[tokio::test]
    async fn verify_respects_max_rounds() {
        // Critic always complains; with max_rounds=2 only one revise happens.
        let llm = ScriptLlm::of(&["Đáp án: 1", "sai rồi", "Đáp án: 2", "sai nữa", "Đáp án: 3"]);
        let r = verify_answer(&*llm, "x", 2).await.unwrap();
        assert!(r.revised);
        assert_eq!(r.rounds.len(), 3); // initial + critic + revise
        assert_eq!(r.final_answer, "Đáp án: 2");
    }

    // -- vote ----------------------------------------------------------------

    #[tokio::test]
    async fn vote_picks_majority_answer() {
        let llm = ScriptLlm::of(&[
            "Đáp án: 24",
            "Đáp án: 17",
            "Đáp án: 24",
            "Đáp án: 24",
            "Đáp án: 17",
        ]);
        let r = vote(&*llm, "7 + 5 x 2", 5).await.unwrap();
        assert_eq!(r.winner, "24");
        assert_eq!(r.answers.len(), 5);
        let counts: std::collections::HashMap<&str, usize> =
            r.votes.iter().map(|(a, c)| (a.as_str(), *c)).collect();
        assert_eq!(counts["24"], 3);
        assert_eq!(counts["17"], 2);
        // round 2+ carries the variation nudge
        assert!(llm.prompts()[1].starts_with("Hãy thử cách khác:"));
        assert!(!llm.prompts()[0].starts_with("Hãy thử cách khác:"));
    }

    #[tokio::test]
    async fn vote_breaks_ties_by_first_appearance() {
        let llm = ScriptLlm::of(&["Đáp án: B", "Đáp án: A"]);
        let r = vote(&*llm, "x", 2).await.unwrap();
        assert_eq!(r.winner, "B");
    }

    #[tokio::test]
    async fn vote_rejects_zero_rounds() {
        let llm = ScriptLlm::of(&[]);
        assert!(vote(&*llm, "x", 0).await.is_err());
    }
}
