from typing import Any, List

from pydantic import BaseModel

from loop.react import ReActLoop, ReActStep
from environment import AgentEnvironment

class ConversationTurn(BaseModel):
    session_id: str
    user_prompt: str
    steps: List[ReActStep] = []
    response_text: str = ""
    success: bool = True

class ConversationRunner:
    def __init__(self, env: AgentEnvironment, llm: Any = None, max_turns: int = 10):
        self.env = env
        self.llm = llm
        self.max_turns = max_turns
        self.react_loop = ReActLoop(
            env,
            llm,
            max_turns=max_turns,
            accountant=getattr(env, "token_accountant", None),
        )

    async def run_turn(self, session_id: str, prompt: str) -> ConversationTurn:
        self.react_loop.session_id = session_id
        result = await self.react_loop.step(prompt)
        return ConversationTurn(
            session_id=session_id,
            user_prompt=prompt,
            steps=[ReActStep(**s) for s in result.get("trace", [])],
            response_text=result.get("action", ""),
            success=result.get("success", True),
        )
