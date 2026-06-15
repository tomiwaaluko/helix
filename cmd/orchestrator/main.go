// Helix orchestrator — control plane binary.
//
// Reads config from env, runs Postgres migrations, starts the gRPC server and
// HTTP REST server concurrently. Shuts down gracefully on SIGINT / SIGTERM.
package main

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"log/slog"
	"net"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/golang-migrate/migrate/v4"
	"github.com/golang-migrate/migrate/v4/database/postgres"
	_ "github.com/golang-migrate/migrate/v4/source/file"
	_ "github.com/lib/pq"
	"google.golang.org/grpc"

	"github.com/tomiwaaluko/helix/internal/api"
	"github.com/tomiwaaluko/helix/internal/clickhouse"
	"github.com/tomiwaaluko/helix/internal/config"
	"github.com/tomiwaaluko/helix/internal/dispatch"
	grpcserver "github.com/tomiwaaluko/helix/internal/grpc"
	helixminio "github.com/tomiwaaluko/helix/internal/minio"
	"github.com/tomiwaaluko/helix/internal/store"

	helixv1 "github.com/tomiwaaluko/helix/gen/go/helix/v1"
)

func main() {
	log := slog.New(slog.NewJSONHandler(os.Stdout, nil))

	cfg, err := config.Load()
	if err != nil {
		log.Error("config error", "err", err)
		os.Exit(1)
	}

	ctx, cancel := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer cancel()

	if err := runMigrations(cfg.DatabaseURL); err != nil {
		log.Error("migrations failed", "err", err)
		os.Exit(1)
	}
	log.Info("migrations applied")

	pg, err := store.NewPostgresStore(ctx, cfg.DatabaseURL)
	if err != nil {
		log.Error("postgres connect failed", "err", err)
		os.Exit(1)
	}
	defer pg.Close()

	nc, err := dispatch.NewClient(cfg.NATSURL)
	if err != nil {
		log.Error("nats connect failed", "err", err)
		os.Exit(1)
	}
	defer nc.Close()

	// Build the HTTP handler, wiring in optional trace components.
	h := api.NewHandler(pg, nc, log, cfg.APIToken)
	if cfg.ClickHouseURL != "" {
		chConn, err := clickhouse.Open(ctx, cfg.ClickHouseURL)
		if err != nil {
			log.Warn("clickhouse connect failed — trace endpoint disabled", "err", err)
		} else {
			presigner, err := helixminio.FromEnv()
			if err != nil {
				log.Warn("minio presigner init failed — blob URIs will not be presigned", "err", err)
			}
			sr := clickhouse.NewSpanReader(chConn)
			h = h.WithTrace(sr, presigner)
			log.Info("trace endpoint enabled", "clickhouse_url", cfg.ClickHouseURL)

			// Eval read/write share the same connection.
			ew := clickhouse.NewEvalWriter(chConn)
			er := clickhouse.NewEvalReader(chConn)
			h = h.WithEvals(ew, er)
			log.Info("eval endpoints enabled")

			rr := clickhouse.NewRetrievalReader(chConn)
			h = h.WithRetrievals(rr)
			log.Info("retrieval endpoint enabled")

			lcr := clickhouse.NewLlmCallReader(chConn)
			h = h.WithLlmCalls(lcr)
			log.Info("llm-calls endpoint enabled")
		}
	}

	// Start gRPC server
	grpcAddr := fmt.Sprintf(":%d", cfg.GRPCPort)
	lis, err := net.Listen("tcp", grpcAddr)
	if err != nil {
		log.Error("grpc listen failed", "addr", grpcAddr, "err", err)
		os.Exit(1)
	}

	grpcSrv := grpc.NewServer()
	helixv1.RegisterOrchestratorServer(grpcSrv, grpcserver.NewServer(pg, nc, log))

	// Start HTTP server
	httpAddr := fmt.Sprintf(":%d", cfg.HTTPPort)
	httpSrv := &http.Server{
		Addr:              httpAddr,
		Handler:           h.Router(),
		ReadHeaderTimeout: 10 * time.Second,
	}

	go func() {
		log.Info("grpc server starting", "addr", grpcAddr)
		if err := grpcSrv.Serve(lis); err != nil {
			log.Error("grpc server error", "err", err)
		}
	}()

	go func() {
		log.Info("http server starting", "addr", httpAddr)
		if err := httpSrv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			log.Error("http server error", "err", err)
		}
	}()

	<-ctx.Done()
	log.Info("shutting down")

	shutdownCtx, shutdownCancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer shutdownCancel()

	if err := httpSrv.Shutdown(shutdownCtx); err != nil {
		log.Error("http shutdown error", "err", err)
	}
	grpcSrv.GracefulStop()
}

func runMigrations(databaseURL string) error {
	db, err := sql.Open("postgres", databaseURL)
	if err != nil {
		return fmt.Errorf("open db for migrations: %w", err)
	}
	defer db.Close() //nolint:errcheck

	driver, err := postgres.WithInstance(db, &postgres.Config{})
	if err != nil {
		return fmt.Errorf("migration driver: %w", err)
	}

	m, err := migrate.NewWithDatabaseInstance("file://migrations", "postgres", driver)
	if err != nil {
		return fmt.Errorf("migrate init: %w", err)
	}

	if err := m.Up(); err != nil && !errors.Is(err, migrate.ErrNoChange) {
		return fmt.Errorf("migrate up: %w", err)
	}
	return nil
}
