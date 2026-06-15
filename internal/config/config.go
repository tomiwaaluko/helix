// Package config loads orchestrator configuration from environment variables.
package config

import (
	"errors"
	"fmt"
	"os"
	"strconv"
)

// Config holds all runtime configuration for the orchestrator.
type Config struct {
	DatabaseURL   string
	NATSURL       string
	GRPCPort      int
	HTTPPort      int
	APIToken      string
	ClickHouseURL string // optional; enables GET /api/v1/runs/{id}/trace when set
}

// Load reads configuration from environment variables and returns a validated Config.
// Environment variables:
//   - DATABASE_URL  (default: postgres://helix:helix@localhost:5432/helix?sslmode=disable)
//   - NATS_URL      (default: nats://localhost:4222)
//   - GRPC_PORT     (default: 50051)
//   - HTTP_PORT     (default: 8080)
//   - HELIX_API_TOKEN (required)
func Load() (Config, error) {
	cfg := Config{
		DatabaseURL: envOrDefault("DATABASE_URL", "postgres://helix:helix@localhost:5432/helix?sslmode=disable"),
		NATSURL:     envOrDefault("NATS_URL", "nats://localhost:4222"),
		GRPCPort:    50051,
		HTTPPort:    8080,
	}

	if raw := os.Getenv("GRPC_PORT"); raw != "" {
		port, err := strconv.Atoi(raw)
		if err != nil {
			return Config{}, fmt.Errorf("config: invalid GRPC_PORT %q: %w", raw, err)
		}
		cfg.GRPCPort = port
	}

	if raw := os.Getenv("HTTP_PORT"); raw != "" {
		port, err := strconv.Atoi(raw)
		if err != nil {
			return Config{}, fmt.Errorf("config: invalid HTTP_PORT %q: %w", raw, err)
		}
		cfg.HTTPPort = port
	}

	cfg.APIToken = os.Getenv("HELIX_API_TOKEN")
	if cfg.APIToken == "" {
		return Config{}, errors.New("config: HELIX_API_TOKEN is required but not set")
	}

	cfg.ClickHouseURL = os.Getenv("CLICKHOUSE_URL")

	return cfg, nil
}

func envOrDefault(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}
