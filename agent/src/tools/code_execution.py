import io
import sys
import time
from typing import Any, Callable, Dict

# Tool-gluing code should finish instantly; this is the guard against a model-written loop.
EXEC_DEADLINE_SECONDS = 30.0

class CodeExecutionKernel:
    """Persistent Python kernel executing tools programmatically."""

    def __init__(self, tools: Dict[str, Dict[str, Any]]):
        self.tools = tools
        self.locals_ctx: Dict[str, Any] = {}

    @staticmethod
    def _deadline_tracer(start: float, limit: float):
        def _trace(frame, event, arg):
            if time.monotonic() - start > limit:
                raise TimeoutError(f"execution exceeded {EXEC_DEADLINE_SECONDS:g}s")
            return _trace
        return _trace

    def execute(self, code: str) -> Dict[str, Any]:
        stdout_buf = io.StringIO()
        old_stdout = sys.stdout

        tool_helpers = {name: meta["func"] for name, meta in self.tools.items()}
        exec_scope = {**tool_helpers, **self.locals_ctx}

        # shortcut: exec still runs in-process with full trust. The deadline interrupts
        # bytecode only, so a blocking C call (time.sleep, a hung socket) still freezes the
        # agent. Upgrade by routing this through IpcBridgeClient.execute() once tool handles
        # no longer need to be in scope.
        try:
            sys.stdout = stdout_buf
            sys.settrace(self._deadline_tracer(time.monotonic(), EXEC_DEADLINE_SECONDS))
            exec(code, exec_scope)
            for k, v in exec_scope.items():
                if k not in tool_helpers and not k.startswith("__"):
                    self.locals_ctx[k] = v

            output = stdout_buf.getvalue()
            return {"stdout": output, "exit_code": 0, "success": True}
        except BaseException as e:
            # BaseException, not Exception: model code calling exit()/sys.exit() raises
            # SystemExit, which would otherwise take the whole agent down with it.
            return {
                "error": f"{type(e).__name__}: {e}",
                "stdout": stdout_buf.getvalue(),
                "exit_code": 1,
                "success": False,
            }
        finally:
            sys.settrace(None)
            sys.stdout = old_stdout
