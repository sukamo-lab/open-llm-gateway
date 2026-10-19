# LLM Gateway Helm chart

Create the referenced configuration ConfigMap and Secret through your deployment system, then install with an immutable image tag:

```sh
helm upgrade --install llm-gateway ./deploy/helm/llm-gateway \
  --set image.tag=1.0.0 --set config.existingConfigMap=llm-gateway-config \
  --set secrets.existingSecret=llm-gateway-secrets
```

The chart never creates secret values. Use External Secrets Operator, Secrets Store CSI, Sealed Secrets, or your platform's equivalent to materialize `llm-gateway-secrets`. The pod service account has token automount disabled; enable workload identity only in a reviewed overlay when a secret provider requires it.

Network policy denies unspecified ingress and permits DNS plus explicitly configured HTTPS CIDRs. Provider hostnames cannot be represented directly in Kubernetes NetworkPolicy, so resolve and manage approved egress ranges through your CNI/firewall or service mesh. Run `helm lint` and `helm template` in CI with every production values overlay.
