"""Append-only audit log for enterprise: who did what, when.

Every security-relevant action (channel sends, kicks, trigger changes,
permission grants, ...) is recorded as an immutable AuditEvent. The store
is append-only on purpose: there is deliberately no delete/update API,
because an audit trail you can rewrite is not an audit trail.
"""

from audit.models import AuditEvent
from audit.store import AuditStore
from audit.middleware import audit_wrap

__all__ = ["AuditEvent", "AuditStore", "audit_wrap"]
