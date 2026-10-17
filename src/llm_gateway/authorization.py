from __future__ import annotations

from dataclasses import dataclass


PERMISSIONS = frozenset({
    "gateway.invoke", "models.read", "organizations.read", "organizations.manage",
    "projects.read", "projects.manage", "principals.read", "principals.manage",
    "keys.read", "keys.issue", "keys.revoke", "roles.read", "roles.manage",
    "audit.read", "audit.export", "credentials.manage", "encryption.rotate",
    "residency.manage",
})

BUILTIN_ROLES: dict[str, frozenset[str]] = {
    "organization_admin": frozenset({
        "gateway.invoke", "models.read", "organizations.read", "organizations.manage",
        "projects.read", "projects.manage", "principals.read", "principals.manage",
        "keys.read", "keys.issue", "keys.revoke", "roles.read", "roles.manage",
    }),
    "project_admin": frozenset({
        "gateway.invoke", "models.read", "projects.read", "principals.read",
        "principals.manage", "keys.read", "keys.issue", "keys.revoke",
    }),
    "developer": frozenset({"gateway.invoke", "models.read"}),
    "auditor": frozenset({"organizations.read", "projects.read", "principals.read", "audit.read", "audit.export"}),
    "credential_admin": frozenset({"credentials.manage"}),
    "security_admin": frozenset({"audit.read", "audit.export", "encryption.rotate", "residency.manage"}),
}


@dataclass(frozen=True)
class AuthorizationContext:
    organization_id: str
    project_id: str | None
    permissions: frozenset[str]

    def allows(self, permission: str, organization_id: str | None = None, project_id: str | None = None) -> bool:
        if permission not in self.permissions:
            return False
        if organization_id is not None and organization_id != self.organization_id:
            return False
        if self.project_id is not None and project_id != self.project_id:
            return False
        return True


def role_permissions(role_names: list[str] | tuple[str, ...]) -> frozenset[str]:
    permissions: set[str] = set()
    for name in role_names:
        permissions.update(BUILTIN_ROLES.get(name, ()))
    return frozenset(permissions)


def apply_key_ceiling(granted: frozenset[str], ceiling: list[str] | None) -> frozenset[str]:
    if ceiling is None:
        return granted
    requested = frozenset(ceiling)
    if not requested <= PERMISSIONS:
        raise ValueError("unknown permission in key scope")
    return granted & requested
