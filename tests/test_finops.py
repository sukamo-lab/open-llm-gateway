from fastapi.testclient import TestClient

from llm_gateway.app import create_app


def _provider_yaml() -> str:
    return """
providers:
  - name: provider
    base_url: https://example.test/v1
    api_key_env: PROVIDER_KEY
    models:
      - {name: fast, upstream_model: model}
"""


def test_finops_plan_endpoint_is_removed_when_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("PROVIDER_KEY", "provider-secret")
    path = tmp_path / "disabled.yaml"
    path.write_text(_provider_yaml(), encoding="utf-8")

    with TestClient(create_app(str(path))) as client:
        assert client.get("/admin/plans").status_code == 404


def test_finops_plan_catalog_and_tenant_assignment(tmp_path, monkeypatch):
    admin_key = "admin-key-that-is-long-enough"
    monkeypatch.setenv("PROVIDER_KEY", "provider-secret")
    monkeypatch.setenv("LLM_GATEWAY_ADMIN_KEY", admin_key)
    path = tmp_path / "enabled.yaml"
    database = (tmp_path / "gateway.db").as_posix()
    path.write_text(
        f"""
mode: multi
database: {database}
features:
  finops_plans: true
finops:
  plans:
    - id: starter
      pricing_version: 2026-10
      entitlements: [embeddings]
      requests_per_minute: 10
      cost_per_30_days: 20
      included_credit_usd: 5
      budget_alert_percentages: [50, 100]
""" + _provider_yaml(),
        encoding="utf-8",
    )
    headers = {"Authorization": f"Bearer {admin_key}"}

    with TestClient(create_app(str(path))) as client:
        plans = client.get("/admin/plans", headers=headers)
        tenant = client.post(
            "/admin/tenants", headers=headers, json={"name": "tenant", "plan_id": "starter"},
        )
        unknown = client.post(
            "/admin/tenants", headers=headers, json={"name": "other", "plan_id": "missing"},
        )
        issued = client.post(
            f"/admin/tenants/{tenant.json()['id']}/keys", headers=headers, json={"name": "client"},
        )
        denied = client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {issued.json()['api_key']}"},
            json={"model": "fast", "messages": [{"role": "user", "content": "hello"}]},
        )
        budget = client.get(f"/admin/tenants/{tenant.json()['id']}/budget", headers=headers)

    assert plans.status_code == 200
    assert plans.json()[0]["pricing_version"] == "2026-10"
    assert tenant.status_code == 201 and tenant.json()["plan_id"] == "starter"
    assert unknown.status_code == 400
    assert denied.status_code == 403
    assert budget.status_code == 200
    assert budget.json()["remaining_credit_usd"] == 5
