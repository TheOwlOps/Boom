"""Test offline cho authz (RBAC). Khong can network, khong can credential."""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent" / "src"))

from authz.models import Role, User, default_roles, BUILTIN_ROLES
from authz.store import AuthzStore
from authz.checker import (
    PermissionDenied, can, require, match_permission, effective_permissions,
)
from authz.tools import build_tools

PASS = 0
FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        print("FAIL:", name)


def fresh_store():
    tmp = tempfile.mkdtemp()
    return AuthzStore(Path(tmp) / "authz.db")


# ---- models ----
r = Role(name="x", permissions=["a.b"])
check("role round-trip", Role.from_dict(r.to_dict()).to_dict() == r.to_dict())
u = User(id="u1", name="A", roles=["admin"])
check("user round-trip", User.from_dict(u.to_dict()).to_dict() == u.to_dict())
drs = {r.name: r for r in default_roles()}
check("3 builtin roles", set(drs) == {"admin", "operator", "viewer"})
check("admin has *", drs["admin"].permissions == ["*"])
check("operator can send", "channel.send" in drs["operator"].permissions)
check("operator can trigger.*", "trigger.*" in drs["operator"].permissions)
check("viewer limited", set(drs["viewer"].permissions) == {
    "channel.info", "trigger.list", "task.status"})
check("BUILTIN_ROLES", BUILTIN_ROLES == frozenset({"admin", "operator", "viewer"}))

# ---- match_permission ----
check("star matches all", match_permission("*", "anything.at.all"))
check("exact", match_permission("channel.send", "channel.send"))
check("exact no", not match_permission("channel.send", "channel.kick"))
check("ns.* matches child", match_permission("trigger.*", "trigger.create"))
check("ns.* matches self", match_permission("trigger.*", "trigger"))
check("ns.* no cross", not match_permission("trigger.*", "triggers.create"))
check("ns.* no prefix trick", not match_permission("admin.*", "administrator.x"))
check("empty granted", not match_permission("", "channel.send"))
check("empty required", not match_permission("*", ""))
check("case sensitive", not match_permission("Channel.Send", "channel.send"))

# ---- store ----
s = fresh_store()
check("seeded builtin", {r.name for r in s.list_roles()} == {"admin", "operator", "viewer"})
uid = s.add_user(User(id="", name="Son", roles=["admin"]))
check("add_user auto id", uid.startswith("usr_"))
got = s.get_user(uid)
check("get_user", got is not None and got.name == "Son" and got.roles == ["admin"])
check("get missing -> None", s.get_user("nope") is None)
try:
    s.add_user(User(id=uid, name="Dup"))
    check("dup id raises", False)
except ValueError:
    check("dup id raises", True)
try:
    s.add_user(User(id="u2", roles=["ghost"]))
    check("unknown role raises", False)
except ValueError:
    check("unknown role raises", True)

s.assign_role(uid, "viewer")
check("assign_role", "viewer" in s.get_user(uid).roles)
s.assign_role(uid, "viewer")  # idempotent
check("assign idempotent", s.get_user(uid).roles.count("viewer") == 1)
s.revoke_role(uid, "viewer")
check("revoke_role", "viewer" not in s.get_user(uid).roles)
s.revoke_role(uid, "viewer")  # no-op
check("revoke missing no-op", True)
try:
    s.assign_role("nouser", "admin")
    check("assign unknown user raises", False)
except ValueError:
    check("assign unknown user raises", True)
try:
    s.assign_role(uid, "ghost")
    check("assign unknown role raises", False)
except ValueError:
    check("assign unknown role raises", True)

s.create_role(Role(name="support", permissions=["channel.info", "task.status"]))
check("create_role", s.get_role("support") is not None)
try:
    s.create_role(Role(name="admin", permissions=[]))
    check("create dup role raises", False)
except ValueError:
    check("create dup role raises", True)
try:
    s.create_role(Role(name="", permissions=[]))
    check("create empty name raises", False)
except ValueError:
    check("create empty name raises", True)
try:
    s.delete_role("admin")
    check("delete builtin raises", False)
except ValueError:
    check("delete builtin raises", True)
check("delete missing -> False", s.delete_role("ghost") is False)
s.assign_role(uid, "support")
try:
    s.delete_role("support")
    check("delete assigned role raises", False)
except ValueError:
    check("delete assigned role raises", True)
s.revoke_role(uid, "support")
check("delete_role ok", s.delete_role("support") is True)
check("list_users", len(s.list_users()) == 1)
check("delete_user", s.delete_user(uid) is True)
check("delete_user missing", s.delete_user(uid) is False)
s.close()

# ---- checker ----
s = fresh_store()
admin = User(id="a", roles=["admin"])
op = User(id="o", roles=["operator"])
viewer = User(id="v", roles=["viewer"])
nobody = User(id="n", roles=[])
multi = User(id="m", roles=["viewer", "operator"])

check("admin can all", can(admin, "channel.kick", s) and can(admin, "whatever.x", s))
check("operator send", can(op, "channel.send", s))
check("operator trigger.*", can(op, "trigger.delete", s))
check("operator no kick", not can(op, "channel.kick", s))
check("operator no authz", not can(op, "authz.manage", s))
check("viewer info", can(viewer, "channel.info", s))
check("viewer no send", not can(viewer, "channel.send", s))
check("nobody denied", not can(nobody, "channel.info", s))
check("multi union", can(multi, "channel.send", s) and can(multi, "channel.info", s))
check("empty perm", not can(op, "", s))
check("ghost role ignored",
      not can(User(id="g", roles=["ghost"]), "channel.send", s))

try:
    require(op, "channel.send", s)
    check("require ok", True)
except PermissionDenied:
    check("require ok", False)
try:
    require(viewer, "channel.send", s)
    check("require raises", False)
except PermissionDenied as e:
    check("require raises", "viewer" in str(e) or "v" in str(e))
    check("require msg has perm", "channel.send" in str(e))

perms = effective_permissions(multi, s)
check("effective union dedup",
      "channel.info" in perms and perms.count("channel.info") == 1)
s.close()

# ---- tools ----
tmp = tempfile.mkdtemp()
tools = build_tools(tmp)
check("7 tools", len(tools) == 7)
for tname, t in tools.items():
    check(f"tool {tname} shape",
          set(t) == {"name", "func", "description", "schema"}
          and t["name"] == tname and callable(t["func"]))

r1 = tools["authz_add_user"]["func"](name="Son", roles=["operator"])
check("tool add_user", "user" in r1 and r1["user"]["name"] == "Son")
uid2 = r1["user"]["id"]
r2 = tools["authz_add_user"]["func"](user_id=uid2, name="Dup")
check("tool add_user dup -> error", "error" in r2)
r3 = tools["authz_check"]["func"](uid2, "channel.send")
check("tool check allowed", r3.get("allowed") is True)
r4 = tools["authz_check"]["func"](uid2, "channel.kick")
check("tool check denied", r4.get("allowed") is False)
r5 = tools["authz_check"]["func"]("ghost", "channel.send")
check("tool check unknown user -> error", "error" in r5)
r6 = tools["authz_assign_role"]["func"](uid2, "viewer")
check("tool assign", r6.get("ok") is True)
r7 = tools["authz_assign_role"]["func"](uid2, "ghost")
check("tool assign bad role -> error", "error" in r7)
r8 = tools["authz_revoke_role"]["func"](uid2, "viewer")
check("tool revoke", r8.get("ok") is True)
r9 = tools["authz_list_users"]["func"]()
check("tool list_users", len(r9.get("users", [])) == 1)
r10 = tools["authz_create_role"]["func"]("mod", ["channel.info"])
check("tool create_role", r10.get("ok") is True)
r11 = tools["authz_create_role"]["func"]("admin", ["x"])
check("tool create_role dup -> error", "error" in r11)
r12 = tools["authz_list_roles"]["func"]()
check("tool list_roles", len(r12.get("roles", [])) == 4)
r13 = tools["authz_add_user"]["func"](name="Bad", roles=["ghost"])
check("tool add_user bad role -> error", "error" in r13)
# tool khong bao gio raise
try:
    tools["authz_check"]["func"]("", "")
    check("tool never raises", True)
except Exception:
    check("tool never raises", False)

print(f"========================================\nPASS: {PASS}  FAIL: {FAIL}")
sys.exit(1 if FAIL else 0)
