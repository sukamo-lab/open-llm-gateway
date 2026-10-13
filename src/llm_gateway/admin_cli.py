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

    commands.add_parser("tenant-list", help="list tenants")

    update = commands.add_parser("tenant-update", help="change or suspend a tenant")
    update.add_argument("tenant_id")
    update.add_argument("--name")
    update.add_argument("--requests-per-minute", type=int)
    update.add_argument("--tokens-per-day", type=int)
    update.add_argument("--cost-per-30-days", type=float)
    state = update.add_mutually_exclusive_group()
    state.add_argument("--enable", action="store_true")
    state.add_argument("--disable", action="store_true")

    issue = commands.add_parser("key-issue", help="issue a consumer key (shown once)")
    issue.add_argument("tenant_id")
    issue.add_argument("name")
    issue.add_argument("--expires-in-days", type=int)

    list_keys = commands.add_parser("key-list", help="list key metadata")
    list_keys.add_argument("tenant_id")

    revoke = commands.add_parser("key-revoke", help="revoke a consumer key")
    revoke.add_argument("key_id")
    revoke.add_argument("--yes", action="store_true", help="confirm revocation")
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
        }.items() if value is not None}
    elif args.command == "tenant-list":
        path = "/admin/tenants"
    elif args.command == "tenant-update":
        method, path = "PATCH", f"/admin/tenants/{args.tenant_id}"
        body = {key: value for key, value in {
            "name": args.name, "requests_per_minute": args.requests_per_minute,
            "tokens_per_day": args.tokens_per_day, "cost_per_30_days": args.cost_per_30_days,
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
    elif args.command == "key-list":
        path = f"/admin/tenants/{args.tenant_id}/keys"
    elif args.command == "key-revoke":
        if not args.yes:
            parser.error("key-revoke requires --yes")
        method, path = "DELETE", f"/admin/keys/{args.key_id}"

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
