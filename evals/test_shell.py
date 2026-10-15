"""Tests for shell.py — must all PASS with `python3 test_shell.py`."""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import shell


def t1_run_echo():
    r = shell.run("echo hello")
    assert r["ok"] is True, r
    assert r["returncode"] == 0, r
    assert r["stdout"].strip() == "hello", r
    assert r["timed_out"] is False, r
    print("PASS test 1: run('echo hello')")


def t2_timeout():
    r = shell.run("sleep 5", timeout=1)
    assert r["timed_out"] is True, r
    assert r["ok"] is False, r
    print("PASS test 2: run('sleep 5', timeout=1) -> timed_out")


def t3_nonzero():
    r = shell.run("ls /nonexistent")
    assert r["returncode"] != 0, r
    assert r["ok"] is False, r
    print("PASS test 3: run('ls /nonexistent') -> nonzero, ok=False")


def t4_blocklist():
    r = shell.run("rm -rf /")
    assert r["ok"] is False, r
    assert "blocked" in r["stderr"].lower(), r
    print("PASS test 4: run('rm -rf /') blocked")


def t5_bg_lifecycle():
    r = shell.run_bg("sleep 30")
    assert r["ok"] is True and r["job_id"].startswith("job_"), r
    jid = r["job_id"]
    s = shell.job_status(jid)
    assert s["ok"] is True and s["running"] is True, s
    k = shell.job_kill(jid)
    assert k["ok"] is True, k
    time.sleep(0.3)
    s2 = shell.job_status(jid)
    assert s2["running"] is False, s2
    print("PASS test 5: run_bg -> status running -> kill -> not running")


def t6_unknown_job():
    s = shell.job_status("nonexistent")
    assert s["ok"] is False and s["error"] == "not found", s
    k = shell.job_kill("nonexistent")
    assert k["ok"] is False and k["error"] == "not found", k
    print("PASS test 6: job_status('nonexistent') -> not found")


def t7_injection():
    marker = "/tmp/shell_test_injection_marker"
    with open(marker, "w") as f:
        f.write("alive")
    try:
        r = shell.run(f"echo hi; rm -rf {marker}")
        # ';' must be a literal argument to echo, never interpreted
        assert "hi;" in r["stdout"], r
        assert os.path.exists(marker), "injection: second command executed!"
        assert r["returncode"] == 0, r
    finally:
        if os.path.exists(marker):
            os.remove(marker)
    print("PASS test 7: 'echo hi; rm -rf ...' -> ';' is a literal arg, marker untouched")


def t8_cwd_confinement():
    r = shell.run("pwd", cwd="/")
    assert r["ok"] is False, r
    assert "outside allowed_root" in r["stderr"], r
    print("PASS test 8: cwd outside allowed_root rejected")


def t9_blocklist_variants():
    for cmd in ["mkfs /dev/sda", "dd if=/dev/zero of=/tmp/x", "shutdown now",
                ":(){:|:&};:", "reboot", "rm -fr --no-preserve-root /"]:
        r = shell.run(cmd)
        assert r["ok"] is False and "blocked" in r["stderr"].lower(), (cmd, r)
    print("PASS test 9: blocklist variants (mkfs, dd, shutdown, fork bomb, reboot, rm --no-preserve-root)")


def t10_list_form():
    r = shell.run(["echo", "from-list"])
    assert r["ok"] is True and r["stdout"].strip() == "from-list", r
    print("PASS test 10: list-form argv accepted")


def t11_bg_output():
    r = shell.run_bg(["sh", "-c", "echo out1; echo err1 1>&2"])
    jid = r["job_id"]
    for _ in range(50):
        s = shell.job_status(jid)
        if not s["running"]:
            break
        time.sleep(0.1)
    assert s["stdout"].strip() == "out1", s
    assert s["stderr"].strip() == "err1", s
    assert s["returncode"] == 0, s
    print("PASS test 11: background job buffers stdout+stderr")


if __name__ == "__main__":
    for fn in [t1_run_echo, t2_timeout, t3_nonzero, t4_blocklist,
               t5_bg_lifecycle, t6_unknown_job, t7_injection,
               t8_cwd_confinement, t9_blocklist_variants,
               t10_list_form, t11_bg_output]:
        fn()
    print("\nALL 11 TESTS PASSED")
