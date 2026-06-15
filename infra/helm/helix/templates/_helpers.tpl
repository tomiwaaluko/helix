{{/*
Expand the name of the chart.
*/}}
{{- define "helix.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name.
Truncate at 63 chars because some Kubernetes name fields are limited to this.
*/}}
{{- define "helix.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{/*
Create chart label value (name-version).
*/}}
{{- define "helix.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels applied to every resource.
*/}}
{{- define "helix.labels" -}}
helm.sh/chart: {{ include "helix.chart" . }}
{{ include "helix.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
Selector labels (stable across upgrades — do NOT add mutable fields here).
*/}}
{{- define "helix.selectorLabels" -}}
app.kubernetes.io/name: {{ include "helix.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
Name of the shared secrets object.
*/}}
{{- define "helix.secretName" -}}
{{- include "helix.fullname" . }}-secrets
{{- end }}

{{/*
Derive the DATABASE_URL from values.
*/}}
{{- define "helix.databaseURL" -}}
{{- printf "postgres://helix:%s@%s-postgresql:5432/%s?sslmode=disable" .Values.secrets.postgresPassword (include "helix.fullname" .) .Values.postgresql.database }}
{{- end }}

{{/*
Derive the NATS_URL from values.
*/}}
{{- define "helix.natsURL" -}}
{{- printf "nats://%s-nats:%d" (include "helix.fullname" .) (.Values.nats.port | int) }}
{{- end }}

{{/*
Derive the REDIS_URL from values.
*/}}
{{- define "helix.redisURL" -}}
{{- printf "redis://%s-redis:%d" (include "helix.fullname" .) (.Values.redis.port | int) }}
{{- end }}

{{/*
Derive the ClickHouse native URL from values.
*/}}
{{- define "helix.clickhouseURL" -}}
{{- printf "clickhouse://%s-clickhouse:%d?database=%s" (include "helix.fullname" .) (.Values.clickhouse.nativePort | int) .Values.clickhouse.database }}
{{- end }}

{{/*
Derive the ClickHouse HTTP URL from values.
*/}}
{{- define "helix.clickhouseHTTPURL" -}}
{{- printf "http://%s-clickhouse:%d" (include "helix.fullname" .) (.Values.clickhouse.port | int) }}
{{- end }}

{{/*
Derive the Qdrant URL from values.
*/}}
{{- define "helix.qdrantURL" -}}
{{- printf "http://%s-qdrant:%d" (include "helix.fullname" .) (.Values.qdrant.port | int) }}
{{- end }}

{{/*
Derive the MinIO S3 endpoint from values.
*/}}
{{- define "helix.s3Endpoint" -}}
{{- printf "http://%s-minio:%d" (include "helix.fullname" .) (.Values.minio.apiPort | int) }}
{{- end }}
