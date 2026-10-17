import pytest

from llm_gateway.identity import IdentityStore


@pytest.mark.asyncio
async def test_key_lifecycle_and_bounded_cache(tmp_path):
    store = IdentityStore(str(tmp_path / "identity.db"), cache_seconds=60, cache_max_entries=2)
    await store.initialize()
    tenant = await store.create_tenant("tenant", requests_per_minute=5)
    issued = await store.issue_key(tenant["id"], "service", expires_in_days=1)
    context = await store.authenticate(issued["api_key"])
    assert context and context.tenant_id == tenant["id"]
    await store.authenticate("invalid-one")
    await store.authenticate("invalid-two")
    assert len(store._cache) <= 2
    assert await store.revoke_key(issued["id"])
    assert await store.authenticate(issued["api_key"]) is None


@pytest.mark.asyncio
async def test_organization_project_and_principal_isolation(tmp_path):
    store = IdentityStore(str(tmp_path / "identity.db"))
    await store.initialize()
    first = await store.create_organization("first")
    second = await store.create_organization("second")
    project = await store.create_project(first["id"], "production")
    user = await store.create_principal(
        first["id"], "user", "oidc|alice", "Alice", project["id"]
    )
    service = await store.create_principal(
        first["id"], "service_account", "worker", "Worker"
    )

    assert [item["id"] for item in await store.list_projects(first["id"])] == [project["id"]]
    assert await store.list_projects(second["id"]) == []
    assert {item["id"] for item in await store.list_principals(first["id"])} == {
        user["id"], service["id"],
    }
    assert await store.list_principals(second["id"]) == []
    with pytest.raises(KeyError, match="project not found in organization"):
        await store.create_principal(second["id"], "user", "oidc|mallory", "Mallory", project["id"])


@pytest.mark.asyncio
async def test_rbac_is_deny_by_default_and_key_scope_cannot_escalate(tmp_path):
    store = IdentityStore(str(tmp_path / "rbac.db"), rbac_enabled=True)
    await store.initialize()
    organization = await store.create_organization("org")
    project = await store.create_project(organization["id"], "project")
    principal = await store.create_principal(
        organization["id"], "service_account", "worker", "Worker", project["id"]
    )
    tenant = await store.create_tenant("tenant", organization_id=organization["id"])

    with pytest.raises(PermissionError, match="exceeds"):
        await store.issue_key(
            tenant["id"], "unprivileged", principal_id=principal["id"],
            project_id=project["id"], permissions=["gateway.invoke"],
        )

    await store.assign_role(organization["id"], principal["id"], "developer", project["id"])
    issued = await store.issue_key(
        tenant["id"], "scoped", principal_id=principal["id"],
        project_id=project["id"], permissions=["gateway.invoke"],
    )
    context = await store.authenticate(issued["api_key"])
    assert context and context.permissions == frozenset({"gateway.invoke"})
    assert context.organization_id == organization["id"]
    assert context.project_id == project["id"]


@pytest.mark.asyncio
async def test_rbac_rejects_cross_organization_principal_key(tmp_path):
    store = IdentityStore(str(tmp_path / "rbac-isolation.db"), rbac_enabled=True)
    await store.initialize()
    first = await store.create_organization("first")
    second = await store.create_organization("second")
    principal = await store.create_principal(first["id"], "user", "alice", "Alice")
    tenant = await store.create_tenant("tenant", organization_id=second["id"])
    with pytest.raises(KeyError, match="tenant organization"):
        await store.issue_key(tenant["id"], "crossed", principal_id=principal["id"])


@pytest.mark.asyncio
async def test_hierarchy_deactivation_and_role_revocation_invalidate_authentication(tmp_path):
    store = IdentityStore(str(tmp_path / "lifecycle.db"), cache_seconds=60, rbac_enabled=True)
    await store.initialize()
    organization = await store.create_organization("org")
    project = await store.create_project(organization["id"], "project")
    principal = await store.create_principal(
        organization["id"], "service_account", "worker", "Worker", project["id"]
    )
    binding = await store.assign_role(
        organization["id"], principal["id"], "developer", project["id"]
    )
    tenant = await store.create_tenant("tenant", organization_id=organization["id"])
    issued = await store.issue_key(
        tenant["id"], "key", principal_id=principal["id"], project_id=project["id"]
    )
    assert await store.authenticate(issued["api_key"])

    assert await store.revoke_role_binding(organization["id"], binding["id"])
    context = await store.authenticate(issued["api_key"])
    assert context and context.permissions == frozenset()
    await store.assign_role(organization["id"], principal["id"], "developer", project["id"])
    await store.update_project(organization["id"], project["id"], {"active": False})
    assert await store.authenticate(issued["api_key"]) is None

    await store.update_project(organization["id"], project["id"], {"active": True})
    await store.update_principal(organization["id"], principal["id"], {"active": False})
    assert await store.authenticate(issued["api_key"]) is None

