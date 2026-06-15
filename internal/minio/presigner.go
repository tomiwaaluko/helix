// Package minio provides a MinIO / S3 presigner for the orchestrator's trace endpoint.
// It rewrites s3:// blob URIs in span attributes to presigned HTTPS GET URLs,
// so the dashboard can fetch large payloads (prompts, completions) directly.
package minio

import (
	"context"
	"fmt"
	"net/url"
	"os"
	"strings"
	"time"

	miniogo "github.com/minio/minio-go/v7"
	"github.com/minio/minio-go/v7/pkg/credentials"
)

// Presigner generates presigned GET URLs for s3:// blob URIs stored in span attributes.
type Presigner struct {
	client *miniogo.Client
}

// FromEnv creates a Presigner from S3_ENDPOINT / S3_ACCESS_KEY / S3_SECRET_KEY env vars.
// Returns nil, nil when S3_ENDPOINT is not set (presigning is disabled; blob URIs are
// omitted from trace responses rather than returned as s3:// references).
func FromEnv() (*Presigner, error) {
	endpoint := os.Getenv("S3_ENDPOINT")
	if endpoint == "" {
		return nil, nil
	}

	useSSL := strings.HasPrefix(endpoint, "https://")
	// minio-go expects "host:port", not "scheme://host:port".
	endpoint = strings.TrimPrefix(endpoint, "https://")
	endpoint = strings.TrimPrefix(endpoint, "http://")

	accessKey := os.Getenv("S3_ACCESS_KEY")
	if accessKey == "" {
		accessKey = "helix"
	}
	secretKey := os.Getenv("S3_SECRET_KEY")
	if secretKey == "" {
		secretKey = "helixhelix"
	}

	client, err := miniogo.New(endpoint, &miniogo.Options{
		Creds:  credentials.NewStaticV4(accessKey, secretKey, ""),
		Secure: useSSL,
	})
	if err != nil {
		return nil, fmt.Errorf("minio client init: %w", err)
	}
	return &Presigner{client: client}, nil
}

// PresignAttrs returns a copy of attrs where any value starting with "s3://" is replaced
// by a presigned HTTPS GET URL valid for the given expiry duration. Other values are
// preserved unchanged. Returns the original attrs map unchanged when p is nil.
func (p *Presigner) PresignAttrs(attrs map[string]string, expiry time.Duration) (map[string]string, error) {
	if p == nil || len(attrs) == 0 {
		return attrs, nil
	}
	out := make(map[string]string, len(attrs))
	for k, v := range attrs {
		if strings.HasPrefix(v, "s3://") {
			signed, err := p.presignURI(v, expiry)
			if err != nil {
				return nil, fmt.Errorf("presign attr %q: %w", k, err)
			}
			out[k] = signed
		} else {
			out[k] = v
		}
	}
	return out, nil
}

// presignURI converts an s3://bucket/key URI to a presigned HTTPS GET URL.
func (p *Presigner) presignURI(uri string, expiry time.Duration) (string, error) {
	u, err := url.Parse(uri)
	if err != nil || u.Scheme != "s3" {
		return "", fmt.Errorf("not an s3 URI: %q", uri)
	}
	bucket := u.Host
	key := strings.TrimPrefix(u.Path, "/")

	presigned, err := p.client.PresignedGetObject(context.Background(), bucket, key, expiry, nil)
	if err != nil {
		return "", fmt.Errorf("presign s3://%s/%s: %w", bucket, key, err)
	}
	return presigned.String(), nil
}
