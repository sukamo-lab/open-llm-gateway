from __future__ import annotations

import argparse
import json
import os
import sys

import httpx

from .cli import load_env_file


def main() -> None:
    parser = argparse.ArgumentParser(description="Administer LLM Gateway tenants and keys")
    parser.add_argument("--url", help="gateway URL (or LLM_GATEWAY_URL)")
    parser.add_argument("--env-file", default=".env")
    commands = parser.add_subparsers(dest="command", required=True)

    create = commands.add_parser("tenant-create", help="create a tenant")
    create.add_argument("name")
    create.add_argument("--requests-per-minute", type=int)
    create.add_argument("--tokens-per-day", type=int)
    create.add_argument("--cost-per-30-days", type=float)
    create.add_argument("--organization-id")

    commands.add_parser("tenant-list", help="list tenants")

    update = commands.add_parser("tenant-update", help="change or suspend a tenant")
    update.add_argument("tenant_id")
    update.add_argument("--name")
    update.add_argument("--requests-per-minute", type=int)
    update.add_argument("--tokens-per-day", type=int)
    update.add_argument("--cost-per-30-days", type=float)
    update.add_argument("--organization-id")
    state = update.add_mutually_exclusive_group()
    state.add_argument("--enable", action="store_true")
    state.add_argument("--disable", action="store_true")

    issue = commands.add_parser("key-issue", help="issue a consumer key (shown once)")
    issue.add_argument("tenant_id")
    issue.add_argument("name")
    issue.add_argument("--expires-in-days", type=int)
    issue.add_argument("--principal-id")
    issue.add_argument("--project-id")
    issue.add_argument("--permission", action="append", dest="permissions")

    list_keys = commands.add_parser("key-list", help="list key metadata")
    list_keys.add_argument("tenant_id")

    revoke = commands.add_parser("key-revoke", help="revoke a consumer key")
    revoke.add_argument("key_id")
    revoke.add_argument("--yes", action="store_true", help="confirm revocation")

    org_create = commands.add_parser("organization-create", help="create an organization")
    org_create.add_argument("name")
    commands.add_parser("organization-list", help="list organizations")
    org_update = commands.add_parser("organization-update", help="rename or change organization state")
    org_update.add_argument("organization_id")
    org_update.add_argument("--name")
    org_state = org_update.add_mutually_exclusive_group()
    org_state.add_argument("--enable", action="store_true")
    org_state.add_argument("--disable", action="store_true")
    project_create = commands.add_parser("project-create", help="create an organization project")
    project_create.add_argument("organization_id")
    project_create.add_argument("name")
    project_list = commands.add_parser("project-list", help="list organization projects")
    project_list.add_argument("organization_id")
    project_update = commands.add_parser("project-update", help="rename or change project state")
    project_update.add_argument("organization_id")
    project_update.add_argument("project_id")
    project_update.add_argument("--name")
    project_state = project_update.add_mutually_exclusive_group()
    project_state.add_argument("--enable", action="store_true")
    project_state.add_argument("--disable", action="store_true")
    principal_create = commands.add_parser("principal-create", help="create a user or service account")
    principal_create.add_argument("organization_id")
    principal_create.add_argument("kind", choices=("user", "service_account"))
    principal_create.add_argument("subject")
    principal_create.add_argument("display_name")
    principal_create.add_argument("--project-id")
    principal_list = commands.add_parser("principal-list", help="list organization principals")
    principal_list.add_argument("organization_id")
    principal_update = commands.add_parser("principal-update", help="rename or change principal state")
    principal_update.add_argument("organization_id")
    principal_update.add_argument("principal_id")
    principal_update.add_argument("--display-name")
    principal_state = principal_update.add_mutually_exclusive_group()
    principal_state.add_argument("--enable", action="store_true")
    principal_state.add_argument("--disable", action="store_true")
    role_assign = commands.add_parser("role-assign", help="assign a built-in role")
    role_assign.add_argument("organization_id")
    role_assign.add_argument("principal_id")
    role_assign.add_argument("role_name")
    role_assign.add_argument("--project-id")
    role_list = commands.add_parser("role-list", help="list organization role bindings")
    role_list.add_argument("organization_id")
    role_revoke = commands.add_parser("role-revoke", help="revoke a role binding")
    role_revoke.add_argument("organization_id")
    role_revoke.add_argument("binding_id")
    role_revoke.add_argument("--yes", action="store_true")
    commands.add_parser("audit-verify", help="verify the complete audit chain")
    commands.add_parser("audit-export", help="export the next configured audit batch")
    commands.add_parser("encryption-rotate", help="rotate the next encryption batch")
    args = parser.parse_args()

    load_env_file(args.env_file)
    base_url = args.url or os.getenv("LLM_GATEWAY_URL", "http://localhost:8000")
    admin_key = os.getenv("LLM_GATEWAY_ADMIN_KEY")
    if not admin_key:
        parser.error("LLM_GATEWAY_ADMIN_KEY is not set")
    headers = {"Authorization": f"Bearer {admin_key}"}

    method, path, body = "GET", "", None
    if args.command == "tenant-create":
        method, path = "POST", "/admin/tenants"
        body = {key: value for key, value in {
            "name": args.name, "requests_per_minute": args.requests_per_minute,
            "tokens_per_day": args.tokens_per_day, "cost_per_30_days": args.cost_per_30_days,
            "organization_id": args.organization_id,
        }.items() if value is not None}
    elif args.command == "tenant-list":
        path = "/admin/tenants"
    elif args.command == "tenant-update":
        method, path = "PATCH", f"/admin/tenants/{args.tenant_id}"
        body = {key: value for key, value in {
            "name": args.name, "requests_per_minute": args.requests_per_minute,
            "tokens_per_day": args.tokens_per_day, "cost_per_30_days": args.cost_per_30_days,
            "organization_id": args.organization_id,
        }.items() if value is not None}
        if args.enable or args.disable:
            body["active"] = args.enable
        if not body:
            parser.error("tenant-update requires at least one change")
    elif args.command == "key-issue":
        method, path = "POST", f"/admin/tenants/{args.tenant_id}/keys"
        body = {"name": args.name}
        if args.expires_in_days:
            body["expires_in_days"] = args.expires_in_days
        if args.principal_id:
            body["principal_id"] = args.principal_id
        if args.project_id:
            body["project_id"] = args.project_id
        if args.permissions:
            body["permissions"] = args.permissions
    elif args.command == "key-list":
        path = f"/admin/tenants/{args.tenant_id}/keys"
    elif args.command == "key-revoke":
        if not args.yes:
            parser.error("key-revoke requires --yes")
        method, path = "DELETE", f"/admin/keys/{args.key_id}"
    elif args.command == "organization-create":
        method, path, body = "POST", "/admin/organizations", {"name": args.name}
    elif args.command == "organization-list":
        path = "/admin/organizations"
    elif args.command == "organization-update":
        method, path = "PATCH", f"/admin/organizations/{args.organization_id}"
        body = {"name": args.name} if args.name else {}
        if args.enable or args.disable:
            body["active"] = args.enable
        if not body:
            parser.error("organization-update requires at least one change")
    elif args.command == "project-create":
        method, path, body = (
            "POST", f"/admin/organizations/{args.organization_id}/projects", {"name": args.name}
        )
    elif args.command == "project-list":
        path = f"/admin/organizations/{args.organization_id}/projects"
    elif args.command == "project-update":
        method, path = "PATCH", f"/admin/organizations/{args.organization_id}/projects/{args.project_id}"
        body = {"name": args.name} if args.name else {}
        if args.enable or args.disable:
            body["active"] = args.enable
        if not body:
            parser.error("project-update requires at least one change")
    elif args.command == "principal-create":
        method, path = "POST", f"/admin/organizations/{args.organization_id}/principals"
        body = {"kind": args.kind, "subject": args.subject, "display_name": args.display_name}
        if args.project_id:
            body["project_id"] = args.project_id
    elif args.command == "principal-list":
        path = f"/admin/organizations/{args.organization_id}/principals"
    elif args.command == "principal-update":
        method, path = "PATCH", f"/admin/organizations/{args.organization_id}/principals/{args.principal_id}"
        body = {"display_name": args.display_name} if args.display_name else {}
        if args.enable or args.disable:
            body["active"] = args.enable
        if not body:
            parser.error("principal-update requires at least one change")
    elif args.command == "role-assign":
        method, path = "POST", f"/admin/organizations/{args.organization_id}/role-bindings"
        body = {"principal_id": args.principal_id, "role_name": args.role_name}
        if args.project_id:
            body["project_id"] = args.project_id
    elif args.command == "role-list":
        path = f"/admin/organizations/{args.organization_id}/role-bindings"
    elif args.command == "role-revoke":
        if not args.yes:
            parser.error("role-revoke requires --yes")
        method, path = "DELETE", (
            f"/admin/organizations/{args.organization_id}/role-bindings/{args.binding_id}"
        )
    elif args.command == "audit-verify":
        path = "/admin/audit/verify"
    elif args.command == "audit-export":
        method, path = "POST", "/admin/audit/export"
    elif args.command == "encryption-rotate":
        method, path = "POST", "/admin/encryption/rotate"

    try:
        kwargs = {"json": body} if body is not None else {}
        response = httpx.request(
            method, base_url.rstrip("/") + path, headers=headers, timeout=30, **kwargs
        )
    except httpx.HTTPError as exc:
        print(f"Gateway connection failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    if response.is_error:
        try:
            message = response.json().get("error", {}).get("message", response.text)
        except ValueError:
            message = response.text
        print(f"Gateway returned HTTP {response.status_code}: {message}", file=sys.stderr)
        raise SystemExit(1)
    if response.status_code == 204:
        print("OK")
    else:
        print(json.dumps(response.json(), indent=2))


if __name__ == "__main__":
    main()
