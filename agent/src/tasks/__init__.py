"""Durable task engine for Boom.

The `tasks` module provides a durable task management engine:
- `models`: base dataclasses (Task, TaskStep, TaskStatus, StepStatus) — JSON-serializable, stdlib only.
- `manager` (later worker): task CRUD + persistence.
- `engine` (later worker): the loop running tasks/steps per the depends_on DAG.

No eager imports here so each module stays independently testable.
"""
