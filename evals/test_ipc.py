import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path

root_dir = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(root_dir / "agent" / "src"))

from tools.bridge import IpcBridgeClient

async def run_ipc_test():
    exe_names = ["harness_core.exe", "harness-core.exe"] if os.name == "nt" else ["harness_core", "harness-core"]
    exe_path = None
    for name in exe_names:
        p = root_dir / "target" / "debug" / name
        if p.exists():
            exe_path = p
            break

    assert exe_path is not None and exe_path.exists(), f"Binary not found in {root_dir / 'target' / 'debug'}. Run 'cargo build --all' first."

    port = 51235
    env = os.environ.copy()
    env["HARNESS_IPC_PORT"] = str(port)

    proc = subprocess.Popen(
        [str(exe_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env
    )

    try:
        # Wait for ready signal
        ready = False
        start_time = time.time()
        while time.time() - start_time < 5:
            line = proc.stdout.readline()
            if "HARNESS_IPC_READY" in line:
                ready = True
                print(f"[Core] {line.strip()}")
                break
            time.sleep(0.05)

        assert ready, "Rust IPC server failed to signal readiness"

        client = IpcBridgeClient(port=port)
        await client.connect()

        # 1. Test ping
        pong = await client.ping()
        assert pong == "pong", f"Expected 'pong', got {pong}"
        print("[PASS] IPC Ping -> Pong verified.")

        # 2. Test core.execute via Rust Sandbox
        res = await client.execute(sys.executable, ["-c", "print('hello from rust sandbox')"])
        assert res["exit_code"] == 0, f"Expected 0, got {res['exit_code']}"
        assert "hello from rust sandbox" in res["stdout"]
        print("[PASS] IPC Sandbox Execution verified:", res["stdout"].strip())

        # 3. A failing command must not be reported as success.
        bad = await client.execute(
            sys.executable, ["-c", "import sys; sys.stderr.write('boom'); sys.exit(3)"]
        )
        assert bad["exit_code"] == 3, f"Expected exit code 3, got {bad}"
        assert bad["success"] is False, bad
        assert "boom" in bad["stderr"], bad
        print("[PASS] IPC exit code & stderr propagation verified:", bad["exit_code"])

        await client.close()
        print("ALL IPC INTEGRATION TESTS PASSED.")

    finally:
        proc.terminate()
        proc.wait(timeout=3)

if __name__ == "__main__":
    asyncio.run(run_ipc_test())
