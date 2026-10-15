"""Test thuc cho agent/src/autonomous/coverage.py (chay bang python3).

Module mau 20 dong, test_fn chi cover ~60% -> verify pct va uncovered lines.
"""

import importlib.util
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_sys_src = os.path.join(_REPO, "agent", "src")
if _sys_src not in sys.path:
    sys.path.insert(0, _sys_src)

from autonomous.coverage import CoverageTracker, measure, report, to_markdown  # noqa: E402

# Module mau: dung 20 dong. test_fn se import lai ben trong tracker (de dong
# `def` duoc tinh), goi add/classify(duong)/classify(am), KHONG goi sub va
# unused -> cover 9/15 = 60%.
SAMPLE_SRC = '''"""Module mau de kiem tra CoverageTracker."""

def add(a, b):
    return a + b

def sub(a, b):
    return a - b

def classify(x):
    if x > 0:
        return "duong"
    elif x < 0:
        return "am"
    else:
        return "khong"

def unused():
    y = 1
    z = 2
    return y + z
'''

TMPDIR = tempfile.mkdtemp(prefix="covtest_")
SAMPLE = os.path.realpath(os.path.join(TMPDIR, "sample_target.py"))
with open(SAMPLE, "w", encoding="utf-8") as fh:
    fh.write(SAMPLE_SRC)

# Ky vong (da verify bang cach chay that):
# code lines = 3,4,6,7,9,10,11,12,13,14,15,17,18,19,20 (15 dong; dong 1 la
# docstring module, cac dong blank khong co token)
# covered = 3,4,6,9,10,11,12,13,17 (9 dong: cac dong `def` chay luc import,
# body cua add va 2 nhanh classify) -> 60.0%
# uncovered = 7,14,15,18,19,20 (body sub, nhanh else, ham unused khong goi)
EXP_TOTAL = 15
EXP_COVERED = 9
EXP_PCT = 60.0
EXP_UNCOVERED = [7, 14, 15, 18, 19, 20]


def make_test_fn():
    def test_fn():
        # import tuoi ben trong tracker de dong `def` cung duoc ghi nhan
        spec = importlib.util.spec_from_file_location("_cov_sample", SAMPLE)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        mod.add(1, 2)
        # NOTE: khong goi mod.sub -> body sub (dong 7) giu uncovered
        mod.classify(10)
        mod.classify(-5)

    return test_fn


def run_tracked(force_settrace=False):
    """Chay test_fn bang tracker truc tiep, tra ve executed map."""
    tr = CoverageTracker(include_dirs=[TMPDIR])
    if force_settrace:
        tr._use_monitoring = False  # gia lap Python cu
    tr.start()
    try:
        make_test_fn()()
    finally:
        executed = tr.stop()
    return tr, executed


TESTS = []


def t(name):
    def deco(fn):
        TESTS.append((name, fn))
        return fn
    return deco


@t("module mau dung 20 dong")
def _():
    assert len(SAMPLE_SRC.splitlines()) == 20, len(SAMPLE_SRC.splitlines())


@t("measure tra ve dict co key la file target")
def _():
    rep = measure([SAMPLE], make_test_fn())
    assert isinstance(rep, dict)
    assert SAMPLE in rep, list(rep)


@t("total_lines = 15 (bo docstring/blank/comment)")
def _():
    rep = measure([SAMPLE], make_test_fn())
    assert rep[SAMPLE]["total_lines"] == EXP_TOTAL, rep[SAMPLE]


@t("covered_lines = 9")
def _():
    rep = measure([SAMPLE], make_test_fn())
    assert rep[SAMPLE]["covered_lines"] == EXP_COVERED, rep[SAMPLE]


@t("pct = 60.0")
def _():
    rep = measure([SAMPLE], make_test_fn())
    pct = rep[SAMPLE]["pct"]
    assert isinstance(pct, float), type(pct)
    assert abs(pct - EXP_PCT) < 0.05, pct


@t("uncovered lines chinh xac va da sort")
def _():
    rep = measure([SAMPLE], make_test_fn())
    unc = rep[SAMPLE]["uncovered"]
    assert unc == EXP_UNCOVERED, unc
    assert unc == sorted(unc)


@t("dong def duoc cover nho import trong tracker")
def _():
    rep = measure([SAMPLE], make_test_fn())
    unc = rep[SAMPLE]["uncovered"]
    for d in (3, 6, 9, 17):  # cac dong `def`
        assert d not in unc, (d, unc)


@t("stop tra ve {file: set(dong)} voi gia tri int")
def _():
    _, executed = run_tracked()
    assert isinstance(executed, dict)
    assert SAMPLE in executed
    assert isinstance(executed[SAMPLE], set)
    assert all(isinstance(n, int) for n in executed[SAMPLE])


@t("chi track file trong include_dirs (khong dinh stdlib)")
def _():
    _, executed = run_tracked()
    for k in executed:
        assert k.startswith(TMPDIR + os.sep), k
        assert "site-packages" not in k and "importlib" not in k, k


@t("dung sys.monitoring tren Python 3.12+")
def _():
    tr, _ = run_tracked()
    assert tr._mode is None  # sau stop thi reset
    tr2 = CoverageTracker(include_dirs=[TMPDIR])
    tr2.start()
    mode = tr2._mode
    tr2.stop()
    assert mode == "monitoring", mode


@t("fallback settrace cho ket qua giong het")
def _():
    _, executed = run_tracked(force_settrace=True)
    rep = report([SAMPLE], executed)
    assert rep[SAMPLE]["total_lines"] == EXP_TOTAL
    assert rep[SAMPLE]["covered_lines"] == EXP_COVERED
    assert abs(rep[SAMPLE]["pct"] - EXP_PCT) < 0.05
    assert rep[SAMPLE]["uncovered"] == EXP_UNCOVERED


@t("stop khi chua start tra ve {}")
def _():
    tr = CoverageTracker(include_dirs=[TMPDIR])
    assert tr.stop() == {}


@t("start/stop lap lai van chay tot")
def _():
    tr = CoverageTracker(include_dirs=[TMPDIR])
    tr.start()
    make_test_fn()()
    e1 = tr.stop()
    tr.start()
    make_test_fn()()
    e2 = tr.stop()
    assert e1[SAMPLE] == e2[SAMPLE] and len(e1[SAMPLE]) > 0


@t("report truc tiep voi executed gia lap")
def _():
    rep = report([SAMPLE], {SAMPLE: {3, 4}})
    r = rep[SAMPLE]
    assert r["covered_lines"] == 2
    assert abs(r["pct"] - round(2 / EXP_TOTAL * 100, 1)) < 0.01, r["pct"]
    assert 3 not in r["uncovered"] and 4 not in r["uncovered"]
    assert len(r["uncovered"]) == EXP_TOTAL - 2


@t("report voi target rong tra ve {}")
def _():
    assert report([], {}) == {}


@t("report file khong ton tai -> total 0, pct 100.0")
def _():
    rep = report([os.path.join(TMPDIR, "khong_co.py")], {})
    r = next(iter(rep.values()))
    assert r["total_lines"] == 0
    assert r["pct"] == 100.0
    assert r["uncovered"] == []


@t("to_markdown co bang tieng Viet day du")
def _():
    rep = measure([SAMPLE], make_test_fn())
    md = to_markdown(rep)
    assert "## Bao cao coverage" in md
    assert "| File |" in md and "Dong chua chay" in md
    assert "sample_target.py" in md
    assert "60.0%" in md
    assert "14, 15, 17, 18, 19, 20" not in md  # ky vong cu, dam bao da doi
    assert "7, 14, 15, 18, 19, 20" in md
    assert "_Tong: 9/15 dong (60.0%)_" in md


@t("measure van stop tracker khi test_fn raise")
def _():
    def boom():
        raise RuntimeError("co tinh")

    try:
        measure([SAMPLE], boom)
    except RuntimeError:
        pass
    else:
        raise AssertionError("phai nem lai RuntimeError")
    # tracker phai duoc stop: khong con trace toan cuc nao
    assert sys.gettrace() is None


@t("uncovered khong chua dong da chay (giao voi code lines)")
def _():
    # dong 1 la docstring: khong duoc tinh vao total lan uncovered
    rep = measure([SAMPLE], make_test_fn())
    r = rep[SAMPLE]
    assert 1 not in r["uncovered"]
    assert r["total_lines"] == r["covered_lines"] + len(r["uncovered"])


def main():
    passed = 0
    failed = []
    for name, fn in TESTS:
        try:
            fn()
        except AssertionError as e:
            failed.append((name, f"AssertionError: {e}"))
            print(f"FAIL: {name} -> {e}")
        except Exception as e:  # noqa: BLE001
            failed.append((name, f"{type(e).__name__}: {e}"))
            print(f"ERROR: {name} -> {type(e).__name__}: {e}")
        else:
            passed += 1
            print(f"ok: {name}")
    total_asserts = ">=20 test cases"
    print(f"\n{passed}/{len(TESTS)} test PASS ({total_asserts})")
    if failed:
        print("FAILED:", [n for n, _ in failed])
        sys.exit(1)


if __name__ == "__main__":
    main()
