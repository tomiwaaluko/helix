// Package otlp implements the OTLP TraceService gRPC interface, writing received
// spans to ClickHouse via a BatchWriter.
package otlp

import (
	"context"
	"encoding/hex"
	"encoding/json"
	"log/slog"
	"strconv"
	"time"

	collectorv1 "go.opentelemetry.io/proto/otlp/collector/trace/v1"
	commonv1 "go.opentelemetry.io/proto/otlp/common/v1"
	tracev1 "go.opentelemetry.io/proto/otlp/trace/v1"

	chwriter "github.com/tomiwaaluko/helix/internal/clickhouse"
)

// spanSink is the minimal write interface required by Server.
// *chwriter.BatchWriter satisfies it.
type spanSink interface {
	Write(chwriter.SpanRow)
}

// retrievalSink is the optional write interface for retrieval rows.
// *chwriter.RetrievalWriter satisfies it.
type retrievalSink interface {
	Write(chwriter.RetrievalRow)
}

// Server implements the OTLP TraceService gRPC server.
type Server struct {
	collectorv1.UnimplementedTraceServiceServer
	writer        spanSink
	retrievalSink retrievalSink
	logger        *slog.Logger
}

// NewServer creates a new Server that writes spans to w.
func NewServer(w spanSink, log *slog.Logger) *Server {
	return &Server{writer: w, logger: log}
}

// NewServerWithRetrieval creates a new Server that writes spans to w and
// retrieval rows to rw when a span with name="retrieve" and kind="retrieval" is received.
func NewServerWithRetrieval(w spanSink, rw retrievalSink, log *slog.Logger) *Server {
	return &Server{writer: w, retrievalSink: rw, logger: log}
}

// Export implements TraceServiceServer.Export.
// It unpacks OTLP ResourceSpans, converts each Span to a SpanRow, and
// enqueues it in the BatchWriter.
func (s *Server) Export(
	ctx context.Context,
	req *collectorv1.ExportTraceServiceRequest,
) (*collectorv1.ExportTraceServiceResponse, error) {
	for _, rs := range req.GetResourceSpans() {
		serviceName := ""
		if res := rs.GetResource(); res != nil {
			for _, kv := range res.GetAttributes() {
				if kv.GetKey() == "service.name" {
					serviceName = kv.GetValue().GetStringValue()
					break
				}
			}
		}

		for _, ss := range rs.GetScopeSpans() {
			for _, span := range ss.GetSpans() {
				attrs := attributesToMap(span.GetAttributes())

				row := chwriter.SpanRow{
					TraceID:      hex.EncodeToString(span.GetTraceId()),
					SpanID:       hex.EncodeToString(span.GetSpanId()),
					ParentSpanID: hex.EncodeToString(span.GetParentSpanId()),
					RunID:        attrs["run_id"],
					TaskID:       attrs["task_id"],
					WorkerID:     attrs["worker_id"],
					Name:         span.GetName(),
					Kind:         kindString(span.GetKind()),
					StartTime:    time.Unix(0, int64(span.GetStartTimeUnixNano())).UTC(),
					EndTime:      time.Unix(0, int64(span.GetEndTimeUnixNano())).UTC(),
					ServiceName:  serviceName,
					Attributes:   attrs,
				}

				if an, ok := attrs["attempt_number"]; ok {
					var n uint32
					for _, c := range an {
						if c >= '0' && c <= '9' {
							n = n*10 + uint32(c-'0')
						}
					}
					row.AttemptNumber = n
				}

				if status := span.GetStatus(); status != nil {
					row.Status = statusString(status.GetCode())
					row.StatusMessage = status.GetMessage()
				} else {
					row.Status = "unset"
				}

				s.writer.Write(row)

				if s.retrievalSink != nil && span.GetName() == "retrieve" && attrs["kind"] == "retrieval" {
					rr := parseRetrievalRow(row, attrs, span)
					s.retrievalSink.Write(rr)
				}
			}
		}
	}

	return &collectorv1.ExportTraceServiceResponse{}, nil
}

// parseRetrievalRow constructs a RetrievalRow from a SpanRow plus its parsed attributes.
func parseRetrievalRow(sr chwriter.SpanRow, attrs map[string]string, span *tracev1.Span) chwriter.RetrievalRow {
	var topK uint32
	if v, err := strconv.ParseUint(attrs["top_k"], 10, 32); err == nil {
		topK = uint32(v)
	}

	// Parse results JSON: [{doc_id, score}, ...]
	type resultEntry struct {
		DocID string  `json:"doc_id"`
		Score float32 `json:"score"`
	}
	var entries []resultEntry
	if raw := attrs["results"]; raw != "" {
		_ = json.Unmarshal([]byte(raw), &entries)
	}
	results := make([]chwriter.ResultTuple, 0, len(entries))
	for i, e := range entries {
		results = append(results, chwriter.ResultTuple{
			PassageID: e.DocID,
			Score:     e.Score,
			Rank:      uint32(i + 1),
		})
	}

	var durationMs uint32
	end := span.GetEndTimeUnixNano()
	start := span.GetStartTimeUnixNano()
	if end > start {
		durationMs = uint32((end - start) / 1_000_000)
	}

	return chwriter.RetrievalRow{
		TraceID:        sr.TraceID,
		SpanID:         sr.SpanID,
		RunID:          sr.RunID,
		Query:          attrs["query"],
		QueryEmbedding: []float32{},
		Retriever:      attrs["retriever"],
		TopK:           topK,
		Results:        results,
		StartTime:      sr.StartTime,
		DurationMs:     durationMs,
	}
}

// attributesToMap converts a slice of OTLP KeyValue attributes to a flat
// string map. Non-string values are skipped.
func attributesToMap(attrs []*commonv1.KeyValue) map[string]string {
	m := make(map[string]string, len(attrs))
	for _, kv := range attrs {
		if kv == nil {
			continue
		}
		v := kv.GetValue()
		if v == nil {
			continue
		}
		if sv, ok := v.GetValue().(*commonv1.AnyValue_StringValue); ok {
			m[kv.GetKey()] = sv.StringValue
		}
	}
	return m
}

// kindString converts a Span_SpanKind enum value to a lowercase string.
func kindString(k tracev1.Span_SpanKind) string {
	switch k {
	case tracev1.Span_SPAN_KIND_INTERNAL:
		return "internal"
	case tracev1.Span_SPAN_KIND_SERVER:
		return "server"
	case tracev1.Span_SPAN_KIND_CLIENT:
		return "client"
	case tracev1.Span_SPAN_KIND_PRODUCER:
		return "producer"
	case tracev1.Span_SPAN_KIND_CONSUMER:
		return "consumer"
	default:
		return "unspecified"
	}
}

// statusString converts a Status_StatusCode enum value to a lowercase string.
func statusString(c tracev1.Status_StatusCode) string {
	switch c {
	case tracev1.Status_STATUS_CODE_OK:
		return "ok"
	case tracev1.Status_STATUS_CODE_ERROR:
		return "error"
	default:
		return "unset"
	}
}
