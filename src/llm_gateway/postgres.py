from __future__ import annotations

import hashlib
import json
import secrets
import time
import uuid
from collections import OrderedDict

import asyncpg

from .config import Limit, ProviderConfig
from .authorization import BUILTIN_ROLES, apply_key_ceiling, role_permissions
from .identity import AuthContext
from .migrations import migrate_postgres, postgres_status
from .quota import Reservation


class PostgresStore:
    """Horizontally safe tenant and quota store using PostgreSQL transactions."""

    def __init__(
        self, dsn: str, cache_seconds: int = 5, cache_max_entries: int = 10_000,
        identity_enabled: bool = True, provider_quotas: bool = True,
        tenant_quotas: bool = True, accounting: bool = True,
        cost_tracking: bool = True, rbac_enabled: bool = False,
        plans: dict[str, object] | None = None,
    ):
        self.dsn = dsn
        self.cache_seconds = cache_seconds
        self.cache_max_entries = cache_max_entries
        self.identity_enabled = identity_enabled
        self.provider_quotas = provider_quotas
        self.tenant_quotas = tenant_quotas
        self.accounting = accounting
        self.cost_tracking = cost_tracking
        self.rbac_enabled = rbac_enabled
        self.plans = plans or {}
        self.usage_enabled = provider_quotas or tenant_quotas or accounting
        self.pool: asyncpg.Pool | None = None
        self._cache: OrderedDict[str, tuple[float, AuthContext | None]] = OrderedDict()

    def _cache_put(self, digest: str, value: AuthContext | None, expires: float) -> None:
        if self.cache_max_entries <= 0:
            return
        self._cache[digest] = (expires, value)
        self._cache.move_to_end(digest)
        while len(self._cache) > self.cache_max_entries:
            self._cache.popitem(last=False)

    async def initialize(self) -> None:
        if self.pool:
            return
        self.pool = await asyncpg.create_pool(self.dsn, min_size=1, max_size=20, command_timeout=30)
        components = []
        if self.usage_enabled:
            components.append("usage")
        if self.identity_enabled:
            components.append("identity")
        await migrate_postgres(self.pool, components)

    async def migration_status(self) -> dict[str, int]:
        assert self.pool
        components = []
        if self.usage_enabled:
            components.append("usage")
        if self.identity_enabled:
            components.append("identity")
        return await postgres_status(self.pool, components)

    async def close(self) -> None:
        if self.pool:
            await self.pool.close()
            self.pool = None

    @staticmethod
    def _within(limit: Limit, requests: int, tokens: int, cost: float, estimate: int, estimate_cost: float) -> bool:
        return (
            (limit.requests is None or requests + 1 <= limit.requests)
            and (limit.tokens is None or tokens + estimate <= limit.tokens)
            and (limit.cost_usd is None or cost + estimate_cost <= limit.cost_usd)
        )

    async def reserve(
        self, provider: ProviderConfig, request_id: str, estimated_tokens: int,
        model: str | None = None, input_cost_per_million: float = 0,
        output_cost_per_million: float = 0, consumer_id: str | None = None,
        consumer_limits: tuple[Limit, ...] = (),
    ) -> Reservation | None:
        assert self.pool
        now = time.time()
        estimated_cost = (
            estimated_tokens * max(input_cost_per_million, output_cost_per_million) / 1_000_000
            if self.cost_tracking else 0
        )
        async with self.pool.acquire() as db, db.transaction():
            # Locks are shared by all gateway replicas and released with the transaction.
            await db.execute("SELECT pg_advisory_xact_lock(hashtext($1))", f"provider:{provider.name}")
            if consumer_id:
                await db.execute("SELECT pg_advisory_xact_lock(hashtext($1))", f"consumer:{consumer_id}")
            for limit, scope, value in [
                *((item, "provider", provider.name) for item in provider.limits if self.provider_quotas),
                *((item, "consumer_id", consumer_id) for item in consumer_limits if self.tenant_quotas),
            ]:
                column = "provider" if scope == "provider" else "consumer_id"
                row = await db.fetchrow(
                    f"""SELECT COALESCE(SUM(requests),0) requests,
                    COALESCE(SUM(tokens),0) tokens, COALESCE(SUM(cost_usd),0) cost
                    FROM usage_events WHERE {column}=$1 AND created_at >= $2
                    AND status != 'failed'""", value, now - limit.window_seconds,
                )
                if not self._within(limit, row["requests"], row["tokens"], row["cost"], estimated_tokens, estimated_cost):
                    return None
            await db.execute(
                """INSERT INTO usage_events
                (provider,request_id,created_at,requests,tokens,status,model,cost_usd,consumer_id)
                VALUES($1,$2,$3,1,$4,'reserved',$5,$6,$7)""",
                provider.name, request_id, now, estimated_tokens, model, estimated_cost, consumer_id,
            )
        return Reservation(
            provider.name, request_id, estimated_tokens, model,
            input_cost_per_million if self.cost_tracking else 0,
            output_cost_per_million if self.cost_tracking else 0,
        )

    async def reconcile(
        self, reservation: Reservation, actual_tokens: int | None, success: bool,
        prompt_tokens: int | None = None, completion_tokens: int | None = None,
    ) -> None:
        assert self.pool
        if success:
            prompt, completion = int(prompt_tokens or 0), int(completion_tokens or 0)
            cost = (prompt * reservation.input_cost_per_million + completion * reservation.output_cost_per_million) / 1_000_000
            if prompt + completion == 0:
                cost = (actual_tokens or reservation.estimated_tokens) * max(
                    reservation.input_cost_per_million, reservation.output_cost_per_million
                ) / 1_000_000
            await self.pool.execute(
                """UPDATE usage_events SET tokens=$1,status='completed',prompt_tokens=$2,
                completion_tokens=$3,cost_usd=$4 WHERE request_id=$5""",
                actual_tokens or reservation.estimated_tokens, prompt, completion, cost, reservation.request_id,
            )
        else:
            await self.pool.execute(
                "UPDATE usage_events SET requests=0,tokens=0,cost_usd=0,status='failed' WHERE request_id=$1",
                reservation.request_id,
            )

    async def snapshot(self) -> list[dict[str, object]]:
        assert self.pool
        rows = await self.pool.fetch("""SELECT provider,SUM(requests) requests,SUM(tokens) tokens,
            MAX(created_at) last_used_at,SUM(prompt_tokens) prompt_tokens,
            SUM(completion_tokens) completion_tokens,SUM(cost_usd) cost_usd
            FROM usage_events WHERE status!='failed' GROUP BY provider""")
        return [dict(row) for row in rows]

    async def consumer_snapshot(self) -> list[dict[str, object]]:
        assert self.pool
        rows = await self.pool.fetch("""SELECT consumer_id tenant_id,SUM(requests) requests,
            SUM(tokens) tokens,SUM(cost_usd) cost_usd,MAX(created_at) last_used_at
            FROM usage_events WHERE status!='failed' AND consumer_id IS NOT NULL GROUP BY consumer_id""")
        return [dict(row) for row in rows]

    async def usage_export(
        self, after: float = 0, after_id: str = "", limit: int = 1000,
    ) -> list[dict[str, object]]:
        assert self.pool
        rows = await self.pool.fetch(
            """SELECT request_id,created_at,provider,model,consumer_id tenant_id,
            requests,tokens,prompt_tokens,completion_tokens,cost_usd,status
            FROM usage_events WHERE status='completed'
            AND (created_at>$1 OR (created_at=$1 AND request_id>$2))
            ORDER BY created_at,request_id LIMIT $3""", after, after_id, limit,
        )
        return [dict(row) for row in rows]

    async def export_checkpoint(self, sink_id: str) -> tuple[float, str]:
        assert self.pool
        row = await self.pool.fetchrow(
            "SELECT after_timestamp,after_request_id FROM usage_export_checkpoints WHERE sink_id=$1",
            sink_id,
        )
        return (float(row["after_timestamp"]), str(row["after_request_id"])) if row else (0, "")

    async def save_export_checkpoint(
        self, sink_id: str, after: float, after_id: str, page_sha256: str,
    ) -> None:
        assert self.pool
        await self.pool.execute(
            """INSERT INTO usage_export_checkpoints
            (sink_id,after_timestamp,after_request_id,page_sha256,updated_at)
            VALUES($1,$2,$3,$4,$5) ON CONFLICT(sink_id) DO UPDATE SET
            after_timestamp=EXCLUDED.after_timestamp,after_request_id=EXCLUDED.after_request_id,
            page_sha256=EXCLUDED.page_sha256,updated_at=EXCLUDED.updated_at""",
            sink_id, after, after_id, page_sha256, time.time(),
        )

    async def cleanup(self, before_timestamp: float) -> int:
        assert self.pool
        result = await self.pool.execute(
            "DELETE FROM usage_events WHERE created_at < $1 AND status!='completed'", before_timestamp,
        )
        return int(result.rsplit(" ", 1)[-1])

    @staticmethod
    def _hash(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    def _limits(self, row) -> tuple[Limit, ...]:
        limits = []
        if row["requests_per_minute"]:
            limits.append(Limit(requests=row["requests_per_minute"], window_seconds=60))
        if row["tokens_per_day"]:
            limits.append(Limit(tokens=row["tokens_per_day"], window_seconds=86400))
        if row["cost_per_30_days"]:
            limits.append(Limit(cost_usd=row["cost_per_30_days"], window_seconds=2592000))
        plan = self.plans.get(row["plan_id"])
        if plan:
            if plan.requests_per_minute:
                limits.append(Limit(requests=plan.requests_per_minute, window_seconds=60))
            if plan.tokens_per_day:
                limits.append(Limit(tokens=plan.tokens_per_day, window_seconds=86400))
            if plan.cost_per_30_days:
                limits.append(Limit(cost_usd=plan.cost_per_30_days, window_seconds=2592000))
        return tuple(limits)

    async def authenticate(self, token: str) -> AuthContext | None:
        assert self.pool
        digest, now = self._hash(token), time.monotonic()
        cached = self._cache.get(digest)
        if cached and cached[0] > now:
            self._cache.move_to_end(digest)
            return cached[1]
        row = await self.pool.fetchrow("""SELECT k.id key_id,k.principal_id,
            k.project_id key_project_id,k.permission_ceiling,t.* FROM consumer_keys k
            JOIN tenants t ON t.id=k.tenant_id WHERE k.key_hash=$1 AND k.active AND t.active
            AND (k.expires_at IS NULL OR k.expires_at > $2)""", digest, time.time())
        context = None
        if row:
            permissions: frozenset[str] = frozenset()
            valid = True
            if self.rbac_enabled:
                valid = bool(row["principal_id"] and row["organization_id"])
                principal = await self.pool.fetchval(
                    """SELECT 1 FROM principals p JOIN organizations o ON o.id=p.organization_id
                    LEFT JOIN projects q ON q.id=p.project_id
                    WHERE p.id=$1 AND p.organization_id=$2 AND p.active AND o.active
                    AND (p.project_id IS NULL OR (p.project_id=$3 AND q.active))""",
                    row["principal_id"], row["organization_id"], row["key_project_id"],
                ) if valid else None
                valid = bool(principal)
                if valid:
                    roles = await self.pool.fetch(
                        """SELECT role_name FROM role_bindings WHERE principal_id=$1
                        AND organization_id=$2 AND (project_id IS NULL OR project_id=$3)""",
                        row["principal_id"], row["organization_id"], row["key_project_id"],
                    )
                    granted = role_permissions([item["role_name"] for item in roles])
                    ceiling = json.loads(row["permission_ceiling"]) if row["permission_ceiling"] else None
                    permissions = apply_key_ceiling(granted, ceiling)
            if valid:
                plan = self.plans.get(row["plan_id"])
                if self.plans and row["plan_id"] and plan is None:
                    valid = False
            if valid:
                context = AuthContext(
                    row["id"], row["key_id"], self._limits(row), row["organization_id"],
                    row["key_project_id"], row["principal_id"], permissions,
                    row["plan_id"] if self.plans else None,
                    frozenset(plan.entitlements) if plan else frozenset(),
                )
        if row:
            await self.pool.execute("UPDATE consumer_keys SET last_used_at=$1 WHERE id=$2", time.time(), row["key_id"])
        self._cache_put(digest, context, now + self.cache_seconds)
        return context

    async def authenticate_oidc(
        self, issuer: str, subject: str, tenant_id: str,
        organization_id: str, project_id: str | None,
    ) -> AuthContext | None:
        assert self.pool
        canonical_subject = f"{issuer.rstrip('/')}|{subject}"
        row = await self.pool.fetchrow(
            """SELECT p.id principal_id,t.* FROM principals p JOIN tenants t
            ON t.organization_id=p.organization_id
            JOIN organizations o ON o.id=p.organization_id
            LEFT JOIN projects q ON q.id=p.project_id
            WHERE p.subject=$1 AND p.kind='user' AND p.active
            AND o.active AND (p.project_id IS NULL OR q.active)
            AND p.organization_id=$2 AND (p.project_id IS NULL OR p.project_id=$3)
            AND t.id=$4 AND t.active""",
            canonical_subject, organization_id, project_id, tenant_id,
        )
        if not row:
            return None
        plan = self.plans.get(row["plan_id"])
        if self.plans and row["plan_id"] and plan is None:
            return None
        roles = await self.pool.fetch(
            """SELECT role_name FROM role_bindings WHERE principal_id=$1
            AND organization_id=$2 AND (project_id IS NULL OR project_id=$3)""",
            row["principal_id"], organization_id, project_id,
        )
        return AuthContext(
            tenant_id, f"oidc:{row['principal_id']}", self._limits(row), organization_id,
            project_id, row["principal_id"], role_permissions([item["role_name"] for item in roles]),
            row["plan_id"] if self.plans else None,
            frozenset(plan.entitlements) if plan else frozenset(),
        )

    async def create_tenant(
        self, name: str, requests_per_minute=None, tokens_per_day=None,
        cost_per_30_days=None, organization_id: str | None = None, plan_id: str | None = None,
    ):
        assert self.pool
        if organization_id and not await self.pool.fetchval(
            "SELECT 1 FROM organizations WHERE id=$1 AND active", organization_id
        ):
            raise KeyError("organization not found or disabled")
        tenant_id = str(uuid.uuid4())
        await self.pool.execute(
            """INSERT INTO tenants
            (id,name,active,requests_per_minute,tokens_per_day,cost_per_30_days,created_at,organization_id,plan_id)
            VALUES($1,$2,TRUE,$3,$4,$5,$6,$7,$8)""",
            tenant_id, name, requests_per_minute, tokens_per_day,
            cost_per_30_days, time.time(), organization_id, plan_id,
        )
        return {"id": tenant_id, "name": name, "active": True, "organization_id": organization_id, "plan_id": plan_id}

    async def list_tenants(self):
        assert self.pool
        return [dict(row) for row in await self.pool.fetch("SELECT * FROM tenants ORDER BY created_at")]

    async def grant_credit(
        self, tenant_id: str, amount_usd: float, source: str,
        expires_at: float | None = None,
    ) -> dict[str, object]:
        assert self.pool
        if not await self.pool.fetchval("SELECT 1 FROM tenants WHERE id=$1", tenant_id):
            raise KeyError("tenant not found")
        item = {
            "id": str(uuid.uuid4()), "tenant_id": tenant_id, "amount_usd": amount_usd,
            "source": source, "created_at": time.time(), "expires_at": expires_at,
        }
        await self.pool.execute(
            """INSERT INTO tenant_credit_grants
            (id,tenant_id,amount_usd,source,created_at,expires_at)
            VALUES($1,$2,$3,$4,$5,$6)""", *item.values(),
        )
        return item

    async def list_credits(self, tenant_id: str) -> list[dict[str, object]]:
        assert self.pool
        rows = await self.pool.fetch(
            """SELECT id,tenant_id,amount_usd,source,created_at,expires_at
            FROM tenant_credit_grants WHERE tenant_id=$1 ORDER BY created_at,id""", tenant_id,
        )
        return [dict(row) for row in rows]

    async def issue_key(
        self, tenant_id: str, name: str, expires_in_days: int | None = None,
        principal_id: str | None = None, project_id: str | None = None,
        permissions: list[str] | None = None,
    ):
        assert self.pool
        tenant = await self.pool.fetchrow(
            "SELECT organization_id FROM tenants WHERE id=$1 AND active", tenant_id
        )
        if not tenant:
            raise KeyError("tenant not found or disabled")
        if self.rbac_enabled:
            if not principal_id or not tenant["organization_id"]:
                raise ValueError("RBAC keys require a principal and organization-bound tenant")
            principal = await self.pool.fetchrow(
                "SELECT organization_id,project_id FROM principals WHERE id=$1 AND active", principal_id
            )
            if not principal or principal["organization_id"] != tenant["organization_id"]:
                raise KeyError("principal not found in tenant organization")
            if not await self.pool.fetchval(
                "SELECT 1 FROM organizations WHERE id=$1 AND active", tenant["organization_id"]
            ):
                raise KeyError("tenant organization not found or disabled")
            if project_id and principal["project_id"] not in (None, project_id):
                raise KeyError("principal is not valid for requested project")
            roles = await self.pool.fetch(
                """SELECT role_name FROM role_bindings WHERE principal_id=$1
                AND organization_id=$2 AND (project_id IS NULL OR project_id=$3)""",
                principal_id, tenant["organization_id"], project_id,
            )
            granted = role_permissions([item["role_name"] for item in roles])
            requested = apply_key_ceiling(granted, permissions)
            if permissions is not None and requested != frozenset(permissions):
                raise PermissionError("key scope exceeds principal permissions")
        key_id, secret = uuid.uuid4().hex, secrets.token_urlsafe(32)
        token = f"lgw_live_{key_id}.{secret}"
        expires_at = time.time() + expires_in_days * 86400 if expires_in_days else None
        await self.pool.execute("""INSERT INTO consumer_keys
            (id,tenant_id,name,key_prefix,key_hash,active,created_at,last_used_at,expires_at,
            principal_id,project_id,permission_ceiling)
            VALUES($1,$2,$3,$4,$5,TRUE,$6,NULL,$7,$8,$9,$10)""",
            key_id, tenant_id, name, token[:20], self._hash(token), time.time(), expires_at,
            principal_id, project_id, json.dumps(permissions) if permissions is not None else None)
        return {"id": key_id, "tenant_id": tenant_id, "name": name, "api_key": token, "expires_at": expires_at}

    async def list_keys(self, tenant_id: str, project_id: str | None = None):
        assert self.pool
        if project_id:
            rows = await self.pool.fetch("""SELECT id,name,key_prefix,active,created_at,last_used_at,
                expires_at,principal_id,project_id FROM consumer_keys
                WHERE tenant_id=$1 AND project_id=$2 ORDER BY created_at""", tenant_id, project_id)
        else:
            rows = await self.pool.fetch("""SELECT id,name,key_prefix,active,created_at,last_used_at,
                expires_at,principal_id,project_id FROM consumer_keys
                WHERE tenant_id=$1 ORDER BY created_at""", tenant_id)
        return [dict(row) for row in rows]

    async def tenant_organization(self, tenant_id: str):
        assert self.pool
        return await self.pool.fetchval("SELECT organization_id FROM tenants WHERE id=$1", tenant_id)

    async def key_organization(self, key_id: str):
        assert self.pool
        return await self.pool.fetchval(
            """SELECT t.organization_id FROM consumer_keys k
            JOIN tenants t ON t.id=k.tenant_id WHERE k.id=$1""", key_id,
        )

    async def key_scope(self, key_id: str):
        assert self.pool
        row = await self.pool.fetchrow("""SELECT t.organization_id,k.project_id FROM consumer_keys k
            JOIN tenants t ON t.id=k.tenant_id WHERE k.id=$1""", key_id)
        return (row["organization_id"], row["project_id"]) if row else (None, None)

    async def principal_project(self, principal_id: str):
        assert self.pool
        return await self.pool.fetchval("SELECT project_id FROM principals WHERE id=$1", principal_id)

    async def revoke_key(self, key_id: str) -> bool:
        assert self.pool
        result = await self.pool.execute("UPDATE consumer_keys SET active=FALSE WHERE id=$1", key_id)
        self._cache.clear()
        return result.endswith("1")

    async def update_tenant(self, tenant_id: str, changes: dict[str, object]):
        assert self.pool
        allowed = {
            "name", "active", "requests_per_minute", "tokens_per_day",
            "cost_per_30_days", "organization_id", "plan_id",
        }
        values = {key: value for key, value in changes.items() if key in allowed}
        if values:
            organization_id = values.get("organization_id")
            if organization_id and not await self.pool.fetchval(
                "SELECT 1 FROM organizations WHERE id=$1 AND active", organization_id
            ):
                raise KeyError("organization not found or disabled")
            assignments = ", ".join(f"{key}=${index}" for index, key in enumerate(values, 1))
            await self.pool.execute(
                f"UPDATE tenants SET {assignments} WHERE id=${len(values) + 1}",
                *values.values(), tenant_id,
            )
            self._cache.clear()
        row = await self.pool.fetchrow("SELECT * FROM tenants WHERE id=$1", tenant_id)
        return dict(row) if row else None

    async def create_organization(self, name: str):
        assert self.pool
        item = {"id": str(uuid.uuid4()), "name": name, "active": True, "created_at": time.time()}
        await self.pool.execute(
            "INSERT INTO organizations(id,name,active,created_at) VALUES($1,$2,TRUE,$3)",
            item["id"], name, item["created_at"],
        )
        return item

    async def list_organizations(self, organization_id: str | None = None):
        assert self.pool
        if organization_id:
            rows = await self.pool.fetch("SELECT * FROM organizations WHERE id=$1", organization_id)
        else:
            rows = await self.pool.fetch("SELECT * FROM organizations ORDER BY created_at")
        return [dict(row) for row in rows]

    async def create_project(self, organization_id: str, name: str):
        assert self.pool
        if not await self.pool.fetchval(
            "SELECT 1 FROM organizations WHERE id=$1 AND active", organization_id
        ):
            raise KeyError("organization not found or disabled")
        item = {
            "id": str(uuid.uuid4()), "organization_id": organization_id,
            "name": name, "active": True, "created_at": time.time(),
        }
        await self.pool.execute(
            "INSERT INTO projects(id,organization_id,name,active,created_at) VALUES($1,$2,$3,TRUE,$4)",
            item["id"], organization_id, name, item["created_at"],
        )
        return item

    async def list_projects(self, organization_id: str):
        assert self.pool
        rows = await self.pool.fetch(
            "SELECT * FROM projects WHERE organization_id=$1 ORDER BY created_at", organization_id
        )
        return [dict(row) for row in rows]

    async def create_principal(
        self, organization_id: str, kind: str, subject: str,
        display_name: str, project_id: str | None = None,
    ):
        assert self.pool
        if not await self.pool.fetchval(
            "SELECT 1 FROM organizations WHERE id=$1 AND active", organization_id
        ):
            raise KeyError("organization not found or disabled")
        if project_id and not await self.pool.fetchval(
            "SELECT 1 FROM projects WHERE id=$1 AND organization_id=$2 AND active",
            project_id, organization_id,
        ):
            raise KeyError("project not found in organization or disabled")
        item = {
            "id": str(uuid.uuid4()), "organization_id": organization_id,
            "project_id": project_id, "kind": kind, "subject": subject,
            "display_name": display_name, "active": True, "created_at": time.time(),
        }
        await self.pool.execute(
            """INSERT INTO principals
            (id,organization_id,project_id,kind,subject,display_name,active,created_at)
            VALUES($1,$2,$3,$4,$5,$6,TRUE,$7)""",
            item["id"], organization_id, project_id, kind, subject,
            display_name, item["created_at"],
        )
        return item

    async def list_principals(self, organization_id: str, project_id: str | None = None):
        assert self.pool
        if project_id:
            rows = await self.pool.fetch(
                """SELECT * FROM principals WHERE organization_id=$1 AND project_id=$2
                ORDER BY created_at""", organization_id, project_id)
        else:
            rows = await self.pool.fetch(
                "SELECT * FROM principals WHERE organization_id=$1 ORDER BY created_at", organization_id
            )
        return [dict(row) for row in rows]

    async def assign_role(
        self, organization_id: str, principal_id: str, role_name: str,
        project_id: str | None = None,
    ):
        assert self.pool
        if role_name not in BUILTIN_ROLES:
            raise ValueError("unknown role")
        principal = await self.pool.fetchrow(
            "SELECT organization_id,project_id FROM principals WHERE id=$1 AND active", principal_id
        )
        if not principal or principal["organization_id"] != organization_id:
            raise KeyError("principal not found in organization")
        if project_id:
            project = await self.pool.fetchval(
                "SELECT 1 FROM projects WHERE id=$1 AND organization_id=$2 AND active",
                project_id, organization_id,
            )
            if not project or principal["project_id"] not in (None, project_id):
                raise KeyError("project or principal scope mismatch")
        item = {
            "id": str(uuid.uuid4()), "organization_id": organization_id,
            "principal_id": principal_id, "role_name": role_name,
            "project_id": project_id, "created_at": time.time(),
        }
        await self.pool.execute(
            """INSERT INTO role_bindings
            (id,organization_id,principal_id,role_name,project_id,created_at)
            VALUES($1,$2,$3,$4,$5,$6)""",
            *item.values(),
        )
        self._cache.clear()
        return item

    async def list_role_bindings(self, organization_id: str):
        assert self.pool
        rows = await self.pool.fetch(
            "SELECT * FROM role_bindings WHERE organization_id=$1 ORDER BY created_at", organization_id
        )
        return [dict(row) for row in rows]

    async def revoke_role_binding(self, organization_id: str, binding_id: str):
        assert self.pool
        result = await self.pool.execute(
            "DELETE FROM role_bindings WHERE id=$1 AND organization_id=$2", binding_id, organization_id
        )
        self._cache.clear()
        return result.endswith("1")

    async def update_organization(self, organization_id: str, changes: dict[str, object]):
        return await self._update_hierarchy("organizations", organization_id, changes, {"name", "active"})

    async def update_project(
        self, organization_id: str, project_id: str, changes: dict[str, object],
    ):
        return await self._update_hierarchy(
            "projects", project_id, changes, {"name", "active"}, organization_id,
        )

    async def update_principal(
        self, organization_id: str, principal_id: str, changes: dict[str, object],
    ):
        return await self._update_hierarchy(
            "principals", principal_id, changes, {"display_name", "active"}, organization_id,
        )

    async def _update_hierarchy(
        self, table: str, item_id: str, changes: dict[str, object], allowed: set[str],
        organization_id: str | None = None,
    ):
        assert self.pool
        values = {key: value for key, value in changes.items() if key in allowed}
        if values:
            assignments = ",".join(f"{key}=${index}" for index, key in enumerate(values, 1))
            where_index = len(values) + 1
            query = f"UPDATE {table} SET {assignments} WHERE id=${where_index}"
            parameters = [*values.values(), item_id]
            if organization_id:
                query += f" AND organization_id=${where_index + 1}"
                parameters.append(organization_id)
            await self.pool.execute(query, *parameters)
        if organization_id:
            row = await self.pool.fetchrow(
                f"SELECT * FROM {table} WHERE id=$1 AND organization_id=$2", item_id, organization_id
            )
        else:
            row = await self.pool.fetchrow(f"SELECT * FROM {table} WHERE id=$1", item_id)
        self._cache.clear()
        return dict(row) if row else None
