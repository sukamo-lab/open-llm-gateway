from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
import json
import secrets
import sqlite3
import time
import uuid
from collections import OrderedDict

from .config import Limit
from .authorization import BUILTIN_ROLES, apply_key_ceiling, role_permissions
from .migrations import migrate_sqlite


@dataclass(frozen=True)
class AuthContext:
    tenant_id: str
    key_id: str
    limits: tuple[Limit, ...] = ()
    organization_id: str | None = None
    project_id: str | None = None
    principal_id: str | None = None
    permissions: frozenset[str] = frozenset()


class IdentityStore:
    """Tenant and consumer-key store. Plaintext keys are never persisted."""

    def __init__(
        self, database: str, cache_seconds: int = 60, cache_max_entries: int = 10_000,
        rbac_enabled: bool = False,
    ):
        self.database = database
        self.cache_seconds = cache_seconds
        self.cache_max_entries = cache_max_entries
        self.rbac_enabled = rbac_enabled
        self._write_lock = asyncio.Lock()
        self._cache: OrderedDict[str, tuple[float, AuthContext | None]] = OrderedDict()

    def _cache_put(self, digest: str, value: AuthContext | None, expires: float) -> None:
        if self.cache_max_entries <= 0:
            return
        self._cache[digest] = (expires, value)
        self._cache.move_to_end(digest)
        while len(self._cache) > self.cache_max_entries:
            self._cache.popitem(last=False)

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.database, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=30000")
        return db

    async def initialize(self) -> None:
        async with self._write_lock:
            migrate_sqlite(self.database, ("identity",))

    @staticmethod
    def _hash(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    @staticmethod
    def _limits(row: sqlite3.Row) -> tuple[Limit, ...]:
        result = []
        if row["requests_per_minute"]:
            result.append(Limit(requests=row["requests_per_minute"], window_seconds=60))
        if row["tokens_per_day"]:
            result.append(Limit(tokens=row["tokens_per_day"], window_seconds=86400))
        if row["cost_per_30_days"]:
            result.append(Limit(cost_usd=row["cost_per_30_days"], window_seconds=2592000))
        return tuple(result)

    async def authenticate(self, token: str) -> AuthContext | None:
        digest = self._hash(token)
        cached = self._cache.get(digest)
        now = time.monotonic()
        if cached and cached[0] > now:
            self._cache.move_to_end(digest)
            return cached[1]

        def lookup() -> AuthContext | None:
            with self._connect() as db:
                row = db.execute(
                    """SELECT k.id key_id, k.principal_id, k.project_id key_project_id,
                    k.permission_ceiling, t.* FROM consumer_keys k
                    JOIN tenants t ON t.id = k.tenant_id
                    WHERE k.key_hash = ? AND k.active = 1 AND t.active = 1
                    AND (k.expires_at IS NULL OR k.expires_at > ?)""",
                    (digest, time.time()),
                ).fetchone()
                if not row:
                    return None
                permissions: frozenset[str] = frozenset()
                if self.rbac_enabled:
                    if not row["principal_id"] or not row["organization_id"]:
                        return None
                    principal = db.execute(
                        """SELECT p.id FROM principals p JOIN organizations o
                        ON o.id=p.organization_id LEFT JOIN projects q ON q.id=p.project_id
                        WHERE p.id=? AND p.organization_id=? AND p.active=1 AND o.active=1
                        AND (p.project_id IS NULL OR (p.project_id=? AND q.active=1))""",
                        (row["principal_id"], row["organization_id"], row["key_project_id"]),
                    ).fetchone()
                    if not principal:
                        return None
                    roles = db.execute(
                        """SELECT role_name FROM role_bindings WHERE principal_id=?
                        AND organization_id=? AND (project_id IS NULL OR project_id=?)""",
                        (row["principal_id"], row["organization_id"], row["key_project_id"]),
                    ).fetchall()
                    granted = role_permissions([item["role_name"] for item in roles])
                    ceiling = json.loads(row["permission_ceiling"]) if row["permission_ceiling"] else None
                    permissions = apply_key_ceiling(granted, ceiling)
                db.execute("UPDATE consumer_keys SET last_used_at = ? WHERE id = ?", (time.time(), row["key_id"]))
                return AuthContext(
                    row["id"], row["key_id"], self._limits(row), row["organization_id"],
                    row["key_project_id"], row["principal_id"], permissions,
                )

        context = await asyncio.to_thread(lookup)
        self._cache_put(digest, context, now + self.cache_seconds)
        return context

    async def authenticate_oidc(
        self, issuer: str, subject: str, tenant_id: str,
        organization_id: str, project_id: str | None,
    ) -> AuthContext | None:
        canonical_subject = f"{issuer.rstrip('/')}|{subject}"

        def lookup() -> AuthContext | None:
            with self._connect() as db:
                row = db.execute(
                    """SELECT p.id principal_id,t.* FROM principals p JOIN tenants t
                    ON t.organization_id=p.organization_id
                    JOIN organizations o ON o.id=p.organization_id
                    LEFT JOIN projects q ON q.id=p.project_id
                    WHERE p.subject=? AND p.kind='user' AND p.active=1
                    AND o.active=1 AND (p.project_id IS NULL OR q.active=1)
                    AND p.organization_id=? AND (p.project_id IS NULL OR p.project_id=?)
                    AND t.id=? AND t.active=1""",
                    (canonical_subject, organization_id, project_id, tenant_id),
                ).fetchone()
                if not row:
                    return None
                roles = db.execute(
                    """SELECT role_name FROM role_bindings WHERE principal_id=?
                    AND organization_id=? AND (project_id IS NULL OR project_id=?)""",
                    (row["principal_id"], organization_id, project_id),
                ).fetchall()
                return AuthContext(
                    tenant_id, f"oidc:{row['principal_id']}", self._limits(row),
                    organization_id, project_id, row["principal_id"],
                    role_permissions([item["role_name"] for item in roles]),
                )

        return await asyncio.to_thread(lookup)

    async def create_tenant(
        self, name: str, requests_per_minute: int | None = None,
        tokens_per_day: int | None = None, cost_per_30_days: float | None = None,
        organization_id: str | None = None,
    ) -> dict[str, object]:
        tenant_id = str(uuid.uuid4())
        async with self._write_lock:
            with self._connect() as db:
                if organization_id and not db.execute(
                    "SELECT 1 FROM organizations WHERE id=? AND active=1", (organization_id,)
                ).fetchone():
                    raise KeyError("organization not found or disabled")
                db.execute(
                    """INSERT INTO tenants
                    (id,name,active,requests_per_minute,tokens_per_day,cost_per_30_days,created_at,organization_id)
                    VALUES (?, ?, 1, ?, ?, ?, ?, ?)""",
                    (tenant_id, name, requests_per_minute, tokens_per_day,
                     cost_per_30_days, time.time(), organization_id),
                )
        return {"id": tenant_id, "name": name, "active": True, "organization_id": organization_id}

    async def list_tenants(self) -> list[dict[str, object]]:
        with self._connect() as db:
            rows = db.execute(
                """SELECT id, name, active, requests_per_minute, tokens_per_day,
                cost_per_30_days, created_at, organization_id FROM tenants ORDER BY created_at"""
            ).fetchall()
        return [dict(row) for row in rows]

    async def issue_key(
        self, tenant_id: str, name: str, expires_in_days: int | None = None,
        principal_id: str | None = None, project_id: str | None = None,
        permissions: list[str] | None = None,
    ) -> dict[str, object]:
        key_id = uuid.uuid4().hex
        secret = secrets.token_urlsafe(32)
        token = f"lgw_live_{key_id}.{secret}"
        async with self._write_lock:
            with self._connect() as db:
                tenant = db.execute(
                    "SELECT organization_id FROM tenants WHERE id = ? AND active = 1", (tenant_id,)
                ).fetchone()
                if not tenant:
                    raise KeyError("tenant not found or disabled")
                if self.rbac_enabled:
                    if not principal_id or not tenant["organization_id"]:
                        raise ValueError("RBAC keys require a principal and organization-bound tenant")
                    principal = db.execute(
                        "SELECT organization_id,project_id FROM principals WHERE id=? AND active=1",
                        (principal_id,),
                    ).fetchone()
                    if not principal or principal["organization_id"] != tenant["organization_id"]:
                        raise KeyError("principal not found in tenant organization")
                    if not db.execute(
                        "SELECT 1 FROM organizations WHERE id=? AND active=1",
                        (tenant["organization_id"],),
                    ).fetchone():
                        raise KeyError("tenant organization not found or disabled")
                    if project_id and principal["project_id"] not in (None, project_id):
                        raise KeyError("principal is not valid for requested project")
                    roles = db.execute(
                        """SELECT role_name FROM role_bindings WHERE principal_id=?
                        AND organization_id=? AND (project_id IS NULL OR project_id=?)""",
                        (principal_id, tenant["organization_id"], project_id),
                    ).fetchall()
                    granted = role_permissions([item["role_name"] for item in roles])
                    requested = apply_key_ceiling(granted, permissions)
                    if permissions is not None and requested != frozenset(permissions):
                        raise PermissionError("key scope exceeds principal permissions")
                expires_at = time.time() + expires_in_days * 86400 if expires_in_days else None
                db.execute(
                    """INSERT INTO consumer_keys
                    (id,tenant_id,name,key_prefix,key_hash,active,created_at,last_used_at,expires_at,
                    principal_id,project_id,permission_ceiling)
                    VALUES (?, ?, ?, ?, ?, 1, ?, NULL, ?, ?, ?, ?)""",
                    (key_id, tenant_id, name, token[:20], self._hash(token), time.time(), expires_at,
                     principal_id, project_id, json.dumps(permissions) if permissions is not None else None),
                )
        return {"id": key_id, "tenant_id": tenant_id, "name": name, "api_key": token, "expires_at": expires_at}

    async def list_keys(self, tenant_id: str, project_id: str | None = None) -> list[dict[str, object]]:
        with self._connect() as db:
            query = """SELECT id, name, key_prefix, active, created_at, last_used_at, expires_at,
                principal_id,project_id FROM consumer_keys WHERE tenant_id = ?"""
            parameters: tuple[object, ...] = (tenant_id,)
            if project_id:
                query += " AND project_id=?"
                parameters += (project_id,)
            rows = db.execute(query + " ORDER BY created_at", parameters).fetchall()
        return [dict(row) for row in rows]

    async def tenant_organization(self, tenant_id: str) -> str | None:
        with self._connect() as db:
            row = db.execute("SELECT organization_id FROM tenants WHERE id=?", (tenant_id,)).fetchone()
        return row["organization_id"] if row else None

    async def key_organization(self, key_id: str) -> str | None:
        with self._connect() as db:
            row = db.execute(
                """SELECT t.organization_id FROM consumer_keys k
                JOIN tenants t ON t.id=k.tenant_id WHERE k.id=?""", (key_id,)
            ).fetchone()
        return row["organization_id"] if row else None

    async def key_scope(self, key_id: str) -> tuple[str | None, str | None]:
        with self._connect() as db:
            row = db.execute(
                """SELECT t.organization_id,k.project_id FROM consumer_keys k
                JOIN tenants t ON t.id=k.tenant_id WHERE k.id=?""", (key_id,)
            ).fetchone()
        return (row["organization_id"], row["project_id"]) if row else (None, None)

    async def principal_project(self, principal_id: str) -> str | None:
        with self._connect() as db:
            row = db.execute("SELECT project_id FROM principals WHERE id=?", (principal_id,)).fetchone()
        return row["project_id"] if row else None

    async def assign_role(
        self, organization_id: str, principal_id: str, role_name: str,
        project_id: str | None = None,
    ) -> dict[str, object]:
        if role_name not in BUILTIN_ROLES:
            raise ValueError("unknown role")
        item = {
            "id": str(uuid.uuid4()), "organization_id": organization_id,
            "principal_id": principal_id, "role_name": role_name,
            "project_id": project_id, "created_at": time.time(),
        }
        async with self._write_lock:
            with self._connect() as db:
                principal = db.execute(
                    "SELECT organization_id,project_id FROM principals WHERE id=? AND active=1",
                    (principal_id,),
                ).fetchone()
                if not principal or principal["organization_id"] != organization_id:
                    raise KeyError("principal not found in organization")
                if project_id:
                    project = db.execute(
                        "SELECT 1 FROM projects WHERE id=? AND organization_id=? AND active=1",
                        (project_id, organization_id),
                    ).fetchone()
                    if not project or principal["project_id"] not in (None, project_id):
                        raise KeyError("project or principal scope mismatch")
                db.execute(
                    """INSERT INTO role_bindings
                    (id,organization_id,principal_id,role_name,project_id,created_at)
                    VALUES(?,?,?,?,?,?)""",
                    tuple(item.values()),
                )
        self._cache.clear()
        return item

    async def list_role_bindings(self, organization_id: str) -> list[dict[str, object]]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM role_bindings WHERE organization_id=? ORDER BY created_at",
                (organization_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    async def revoke_role_binding(self, organization_id: str, binding_id: str) -> bool:
        async with self._write_lock:
            with self._connect() as db:
                cursor = db.execute(
                    "DELETE FROM role_bindings WHERE id=? AND organization_id=?",
                    (binding_id, organization_id),
                )
        self._cache.clear()
        return cursor.rowcount > 0

    async def revoke_key(self, key_id: str) -> bool:
        async with self._write_lock:
            with self._connect() as db:
                cursor = db.execute("UPDATE consumer_keys SET active = 0 WHERE id = ?", (key_id,))
        self._cache.clear()
        return cursor.rowcount > 0

    async def update_tenant(self, tenant_id: str, changes: dict[str, object]) -> dict[str, object] | None:
        allowed = {
            "name", "active", "requests_per_minute", "tokens_per_day",
            "cost_per_30_days", "organization_id",
        }
        values = {key: value for key, value in changes.items() if key in allowed}
        if values:
            async with self._write_lock:
                with self._connect() as db:
                    organization_id = values.get("organization_id")
                    if organization_id and not db.execute(
                        "SELECT 1 FROM organizations WHERE id=? AND active=1", (organization_id,)
                    ).fetchone():
                        raise KeyError("organization not found or disabled")
                    assignments = ", ".join(f"{key} = ?" for key in values)
                    db.execute(f"UPDATE tenants SET {assignments} WHERE id = ?", (*values.values(), tenant_id))
            self._cache.clear()
        with self._connect() as db:
            row = db.execute("SELECT * FROM tenants WHERE id = ?", (tenant_id,)).fetchone()
        return dict(row) if row else None

    async def create_organization(self, name: str) -> dict[str, object]:
        item = {"id": str(uuid.uuid4()), "name": name, "active": True, "created_at": time.time()}
        async with self._write_lock:
            with self._connect() as db:
                db.execute(
                    "INSERT INTO organizations(id,name,active,created_at) VALUES(?,?,1,?)",
                    (item["id"], name, item["created_at"]),
                )
        return item

    async def list_organizations(self, organization_id: str | None = None) -> list[dict[str, object]]:
        with self._connect() as db:
            if organization_id:
                rows = db.execute("SELECT * FROM organizations WHERE id=?", (organization_id,))
            else:
                rows = db.execute("SELECT * FROM organizations ORDER BY created_at")
            return [dict(row) for row in rows]

    async def create_project(self, organization_id: str, name: str) -> dict[str, object]:
        item = {
            "id": str(uuid.uuid4()), "organization_id": organization_id,
            "name": name, "active": True, "created_at": time.time(),
        }
        async with self._write_lock:
            with self._connect() as db:
                if not db.execute(
                    "SELECT 1 FROM organizations WHERE id=? AND active=1", (organization_id,)
                ).fetchone():
                    raise KeyError("organization not found or disabled")
                db.execute(
                    "INSERT INTO projects(id,organization_id,name,active,created_at) VALUES(?,?,?,1,?)",
                    (item["id"], organization_id, name, item["created_at"]),
                )
        return item

    async def list_projects(self, organization_id: str) -> list[dict[str, object]]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM projects WHERE organization_id=? ORDER BY created_at", (organization_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    async def create_principal(
        self, organization_id: str, kind: str, subject: str,
        display_name: str, project_id: str | None = None,
    ) -> dict[str, object]:
        item = {
            "id": str(uuid.uuid4()), "organization_id": organization_id,
            "project_id": project_id, "kind": kind, "subject": subject,
            "display_name": display_name, "active": True, "created_at": time.time(),
        }
        async with self._write_lock:
            with self._connect() as db:
                if not db.execute(
                    "SELECT 1 FROM organizations WHERE id=? AND active=1", (organization_id,)
                ).fetchone():
                    raise KeyError("organization not found or disabled")
                if project_id and not db.execute(
                    "SELECT 1 FROM projects WHERE id=? AND organization_id=? AND active=1",
                    (project_id, organization_id),
                ).fetchone():
                    raise KeyError("project not found in organization or disabled")
                db.execute(
                    """INSERT INTO principals
                    (id,organization_id,project_id,kind,subject,display_name,active,created_at)
                    VALUES(?,?,?,?,?,?,1,?)""",
                    (item["id"], organization_id, project_id, kind, subject,
                     display_name, item["created_at"]),
                )
        return item

    async def list_principals(
        self, organization_id: str, project_id: str | None = None,
    ) -> list[dict[str, object]]:
        with self._connect() as db:
            query = "SELECT * FROM principals WHERE organization_id=?"
            parameters: tuple[object, ...] = (organization_id,)
            if project_id:
                query += " AND project_id=?"
                parameters += (project_id,)
            rows = db.execute(query + " ORDER BY created_at", parameters).fetchall()
        return [dict(row) for row in rows]

    async def update_organization(
        self, organization_id: str, changes: dict[str, object],
    ) -> dict[str, object] | None:
        return await self._update_hierarchy("organizations", organization_id, changes, {"name", "active"})

    async def update_project(
        self, organization_id: str, project_id: str, changes: dict[str, object],
    ) -> dict[str, object] | None:
        return await self._update_hierarchy(
            "projects", project_id, changes, {"name", "active"}, organization_id,
        )

    async def update_principal(
        self, organization_id: str, principal_id: str, changes: dict[str, object],
    ) -> dict[str, object] | None:
        return await self._update_hierarchy(
            "principals", principal_id, changes, {"display_name", "active"}, organization_id,
        )

    async def _update_hierarchy(
        self, table: str, item_id: str, changes: dict[str, object], allowed: set[str],
        organization_id: str | None = None,
    ) -> dict[str, object] | None:
        values = {key: value for key, value in changes.items() if key in allowed}
        where = "id=?" + (" AND organization_id=?" if organization_id else "")
        parameters: tuple[object, ...] = (item_id,) + ((organization_id,) if organization_id else ())
        async with self._write_lock:
            with self._connect() as db:
                if values:
                    assignments = ",".join(f"{key}=?" for key in values)
                    db.execute(
                        f"UPDATE {table} SET {assignments} WHERE {where}",
                        (*values.values(), *parameters),
                    )
                row = db.execute(f"SELECT * FROM {table} WHERE {where}", parameters).fetchone()
        self._cache.clear()
        return dict(row) if row else None
