{{- define "llm-gateway.name" -}}llm-gateway{{- end }}
{{- define "llm-gateway.labels" -}}
app.kubernetes.io/name: {{ include "llm-gateway.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}
