package minio

import (
	"testing"
	"time"
)

func TestPresignAttrs_NilPresigner_ReturnsOriginal(t *testing.T) {
	var p *Presigner
	attrs := map[string]string{"model": "claude", "prompt_uri": "s3://helix-blobs/2026/06/15/span1.bin"}
	got, err := p.PresignAttrs(attrs, time.Hour)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if got["model"] != "claude" {
		t.Errorf("want model=claude, got %q", got["model"])
	}
	// nil presigner → original map returned unchanged, including s3:// values
	if got["prompt_uri"] != "s3://helix-blobs/2026/06/15/span1.bin" {
		t.Errorf("nil presigner should return original attrs unchanged")
	}
}

func TestPresignAttrs_EmptyAttrs_ReturnsEmpty(t *testing.T) {
	p := &Presigner{client: nil} // client is unused for empty attrs
	got, err := p.PresignAttrs(map[string]string{}, time.Hour)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if len(got) != 0 {
		t.Errorf("want empty map, got %v", got)
	}
}

func TestPresignAttrs_NilAttrs_ReturnsNil(t *testing.T) {
	p := &Presigner{client: nil}
	got, err := p.PresignAttrs(nil, time.Hour)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if got != nil {
		t.Errorf("want nil, got %v", got)
	}
}

func TestPresignAttrs_NonS3Values_Preserved(t *testing.T) {
	// A presigner with a nil client that won't be called because there are no s3:// values.
	p := &Presigner{client: nil}
	attrs := map[string]string{
		"model":     "claude-sonnet-4-6",
		"cache_hit": "true",
		"cost_usd":  "0.001234",
	}
	got, err := p.PresignAttrs(attrs, time.Hour)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	for k, want := range attrs {
		if got[k] != want {
			t.Errorf("attr %q: want %q, got %q", k, want, got[k])
		}
	}
}

func TestPresignURI_BadScheme_ReturnsError(t *testing.T) {
	p := &Presigner{client: nil}
	_, err := p.presignURI("https://example.com/file.bin", time.Hour)
	if err == nil {
		t.Error("expected error for non-s3 URI")
	}
}

func TestPresignURI_InvalidURI_ReturnsError(t *testing.T) {
	p := &Presigner{client: nil}
	_, err := p.presignURI("not-a-uri-at-all", time.Hour)
	if err == nil {
		t.Error("expected error for invalid URI")
	}
}

func TestFromEnv_NoEndpoint_ReturnsNil(t *testing.T) {
	t.Setenv("S3_ENDPOINT", "")
	p, err := FromEnv()
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if p != nil {
		t.Error("expected nil presigner when S3_ENDPOINT is unset")
	}
}
