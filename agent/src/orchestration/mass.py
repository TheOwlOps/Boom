from enum import Enum
from typing import Any, Dict, List, Optional
from pydantic import BaseModel

class AgentRole(str, Enum):
    PLANNER = "planner"
    CODER = "coder"
    REVIEWER = "reviewer"
    CRITIC = "critic"
    LEARNER = "learner"

class SpecialistAgent(BaseModel):
    role: AgentRole
    system_instruction: str
    # The contract is what this subagent owes back: the checks it must run, the shape its
    # answer must take. It is appended to the subagent's system prompt, so a step in a
    # workflow can be judged against something the step itself was told to satisfy.
    contract: str = ""
    allowed_tools: List[str]

# Every name here must exist in AgentEnvironment.get_tools(): an allow-list that names a
# nonexistent tool silently strips the subagent's toolset, and the role then does nothing.
# execute_code is absent on purpose — its kernel holds the parent's whole tool dict, so it
# would hand back the tools a subagent is denied.
SPECIALISTS: Dict[AgentRole, SpecialistAgent] = {
    AgentRole.PLANNER: SpecialistAgent(
        role=AgentRole.PLANNER,
        system_instruction=(
            "Decompose the user's goal into independent subtasks. Report the subtasks and "
            "how they depend on each other. Do not modify files."
        ),
        contract=(
            "Answer with a numbered list of subtasks. For each: what it produces, and which "
            "other subtask (if any) it must wait for."
        ),
        allowed_tools=["read_file", "list_skills", "view_skill", "session_search", "ast_inspect"],
    ),
    AgentRole.CODER: SpecialistAgent(
        role=AgentRole.CODER,
        system_instruction=(
            "Write and fix code until it works. Run it to check. Prefer patch_file over "
            "rewriting a file whole."
        ),
        contract=(
            "Report the exact command you ran and its observed output. Any claim about "
            "behaviour that you did not run is not a result — say plainly that it is unverified."
        ),
        allowed_tools=[
            "read_file", "write_file", "patch_file", "ast_inspect",
            "sandbox_exec", "process_start", "process_poll", "process_kill",
        ],
    ),
    AgentRole.REVIEWER: SpecialistAgent(
        role=AgentRole.REVIEWER,
        system_instruction=(
            "Review the work for correctness, security and quality. Run the tests. Report "
            "what is wrong with a file and line, or say plainly that you found nothing."
        ),
        contract=(
            "List each defect as `path:line — what is wrong — why it matters`. If you found "
            "nothing, say `no defects` rather than inventing a finding."
        ),
        allowed_tools=["read_file", "ast_inspect", "session_search", "sandbox_exec"],
    ),
    AgentRole.CRITIC: SpecialistAgent(
        role=AgentRole.CRITIC,
        system_instruction=(
            "You are an adversarial verifier. Your job is to falsify the work in front of "
            "you, not to praise it. Assume it is wrong and go looking for the proof: read the "
            "files it names, run the commands it claims to have run, and check the claims it "
            "makes about behaviour against what actually happens. A claim you could not check "
            "is a defect in the report, not a pass. If the work survives your attempts, say so."
        ),
        contract=(
            "Answer with JSON only, no prose around it:\n"
            '{"passed": true|false, "score": 0.0-1.0, "defects": '
            '[{"claim": "what is wrong", "evidence": "the command, output or file:line that '
            'proves it"}]}\n'
            "`passed` is false if there is any defect you can evidence. `score` is your own "
            "confidence that the work is correct and complete. Each defect needs evidence you "
            "actually obtained; a defect you cannot evidence does not go in the list."
        ),
        allowed_tools=["read_file", "ast_inspect", "session_search", "sandbox_exec"],
    ),
    AgentRole.LEARNER: SpecialistAgent(
        role=AgentRole.LEARNER,
        system_instruction=(
            "Extract the durable lesson from what happened and persist it with the memory "
            "tool. Persist only what would change future behaviour."
        ),
        contract=(
            "One or two sentences, each a rule that would change a future decision. If the run "
            "taught nothing reusable, say `nothing durable` — do not pad the memory."
        ),
        allowed_tools=["memory", "read_file", "list_skills", "view_skill", "install_skill"],
    ),
}


class MASSSwarm:
    """Multi-Agent Swarm routing and agent team management."""
    def __init__(self):
        self.specialists: Dict[AgentRole, SpecialistAgent] = dict(SPECIALISTS)

    def get_agent(self, role: AgentRole) -> SpecialistAgent:
        return self.specialists[role]

    def resolve(self, role: Any) -> Optional[SpecialistAgent]:
        """Specialist for a role name supplied by the model, or None for an unknown role.

        Models send roles as free text ("coder", "Coder"), so a lookup that raises on an
        unknown name would fail the whole delegation over a typo.
        """
        if isinstance(role, AgentRole):
            return self.specialists.get(role)
        if role is None or role == "":
            return None
        try:
            return self.specialists.get(AgentRole(str(role).strip().lower()))
        except ValueError:
            return None

    def role_names(self) -> List[str]:
        return [role.value for role in self.specialists]
