import pytest

from llm_gateway.authorization import (
    AuthorizationContext, BUILTIN_ROLES, PERMISSIONS, apply_key_ceiling, role_permissions,
)


def test_builtin_role_matrix_and_scope_boundaries():
    developer = role_permissions(["developer"])
    assert developer == {"gateway.invoke", "models.read"}
    assert "roles.manage" not in developer
    context = AuthorizationContext("org-a", "project-a", developer)
    assert context.allows("gateway.invoke", "org-a", "project-a")
    assert not context.allows("gateway.invoke", "org-b", "project-a")
    assert not context.allows("gateway.invoke", "org-a", "project-b")
    assert not context.allows("roles.manage", "org-a", "project-a")


def test_scoped_key_ceiling_can_only_reduce_permissions():
    granted = role_permissions(["organization_admin"])
    assert apply_key_ceiling(granted, ["gateway.invoke"]) == {"gateway.invoke"}


@pytest.mark.parametrize("role_name", sorted(BUILTIN_ROLES))
def test_every_builtin_role_grants_only_declared_permissions(role_name):
    granted = role_permissions([role_name])
    assert granted == BUILTIN_ROLES[role_name]
    assert granted <= PERMISSIONS
    assert role_permissions([role_name, "unknown-role"]) == granted


@pytest.mark.parametrize("role_name", sorted(BUILTIN_ROLES))
@pytest.mark.parametrize("permission", sorted(PERMISSIONS))
def test_full_role_permission_matrix_is_deny_by_default(role_name, permission):
    context = AuthorizationContext("organization", None, role_permissions([role_name]))
    assert context.allows(permission, "organization") is (permission in BUILTIN_ROLES[role_name])
    assert context.allows(permission, "other-organization") is False
