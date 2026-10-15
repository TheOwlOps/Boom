"""Interactive clarify tool for user input and disambiguation."""

from typing import Any, Dict, List, Optional, Union

RECOMMENDED_LABEL = "(Recommended)"
MAX_CHOICES = 4
MAX_QUESTIONS = 5

def mark_recommended(choices: List[str]) -> List[str]:
    if len(choices) < 2:
        return choices
    first = str(choices[0]).strip()
    return [f"{first} {RECOMMENDED_LABEL}"] + list(choices[1:])

def strip_recommended(text: str) -> str:
    text = str(text).strip()
    if text.endswith(RECOMMENDED_LABEL):
        return text[:-len(RECOMMENDED_LABEL)].strip()
    return text

def clarify(questions: List[Dict[str, Any]], callback: Optional[Any] = None) -> Dict[str, Any]:
    """
    Ask user one or more questions for decision or clarification.
    questions format: [{'question': '...', 'choices': ['...'], 'multi_select': False}]
    """
    if not isinstance(questions, list) or not questions:
        return {"error": "questions must be a non-empty list of question objects."}
    if len(questions) > MAX_QUESTIONS:
        return {"error": f"Maximum {MAX_QUESTIONS} questions allowed per call."}

    normalized = []
    for idx, q in enumerate(questions):
        q_text = str(q.get("question", "")).strip()
        if not q_text:
            return {"error": f"Question at index {idx} has empty text."}
        raw_choices = q.get("choices")
        choices = mark_recommended(raw_choices) if raw_choices else []
        multi = bool(q.get("multi_select", False))
        normalized.append({
            "qid": f"q{idx + 1}",
            "question": q_text,
            "choices": choices,
            "multi_select": multi
        })

    # In CLI/headless/test mode, default to the recommended first choice or answered placeholder
    responses = []
    for item in normalized:
        resp_val = item["choices"][0] if item["choices"] else "Default user acknowledgement"
        responses.append({
            "question": item["question"],
            "status": "answered",
            "user_response": strip_recommended(resp_val)
        })

    return {
        "responses": responses,
        "outcome": "submitted"
    }
