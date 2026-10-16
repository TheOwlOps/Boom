---
name: doc-researcher
description: "Build a research plan for 1 topic: questions to answer, sources to check, a markdown outline template for the agent to fill in."
version: 1.0.0
author: Boom
license: MIT
platforms: [linux, macos, windows]
metadata:
  boom:
    tags: [research, documentation, planning, outline]
    related_skills: [research/deep-research]
---

# Doc-Researcher — Research Plan & Outline

## Name
`doc-researcher`

## Description
Takes a research topic → generates a structured **research plan**: decomposes the topic into questions to answer, suggests sources to check for each question (official docs, papers, blogs, source code, community), and compiles everything into a **markdown outline** with a template for the agent (or a human) to fill results into.

This skill does not browse the web itself — it creates the framework that makes research systematic and avoids missing perspectives.

## When to use
- Before researching a new technology/library ("should I use X or Y?")
- When writing a synthesis document, comparison report, or RFC
- When splitting research work across multiple subagents per question

## Input
- `topic` (str): research topic, e.g. `"Comparing Redis vs Valkey for session cache"`
- `goal` (str, optional): end goal, e.g. `"pick one solution to deploy"`
- `depth` ("quick" | "standard" | "deep", default `"standard"`): research depth
- `questions` (list[str], optional): the user's own questions, merged into the plan

## Output
Dict:
- `topic`, `goal`, `depth`
- `questions`: list of `{id, question, why, suggested_sources[]}`
- `outline_markdown`: full outline with `> TODO:` templates to fill in results
- `source_checklist`: checklist of sources by type (official docs, paper, blog, code, community)

## Example

```python
from doc_researcher.research import build_research_plan, render_outline

plan = build_research_plan(
    topic="Comparing Redis vs Valkey for session cache",
    goal="pick one solution to deploy in Q4",
    depth="standard",
)
print(render_outline(plan))   # markdown outline to start the research
```
