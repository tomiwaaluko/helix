// Helix collector — M2 OTel span ingestion binary.
//
// Receives OTLP/gRPC spans from Python workers and writes them to ClickHouse.
// Reads config from environment variables. Shuts down gracefully on SIGINT / SIGTERM.
package main

import (
	"context"
	"fmt"
	"log/slog"
	"net"
	"os"
	"os/signal"
	"strconv"
	"syscall"

	"google.golang.org/grpc"

	collectorv1 "go.opentelemetry.io/proto/otlp/collector/trace/v1"

	ch "github.com/tomiwaaluko/helix/internal/clickhouse"
	otlpserver "github.com/tomiwaaluko/helix/internal/otlp"
)

func main() {
	log := slog.New(slog.NewJSONHandler(os.Stdout, nil))

	clickhouseURL := envOrDefault("CLICKHOUSE_URL", "clickhouse://localhost:9000?database=default")
	otlpPortStr := envOrDefault("OTLP_GRPC_PORT", "4317")

	otlpPort, err := strconv.Atoi(otlpPortStr)
	if err != nil {
		log.Error("invalid OTLP_GRPC_PORT", "value", otlpPortStr, "err", err)
		os.Exit(1)
	}

	ctx, cancel := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer cancel()

	// 1. Open ClickHouse connection.
	conn, err := ch.Open(ctx, clickhouseURL)
	if err != nil {
		log.Error("clickhouse connect failed", "err", err)
		os.Exit(1)
	}
	defer conn.Close() //nolint:errcheck
	log.Info("clickhouse connected", "url", clickhouseURL)

	// 2. Run DDL (idempotent — CREATE TABLE IF NOT EXISTS).
	if err := ch.RunDDL(ctx, conn); err != nil {
		log.Error("clickhouse DDL failed", "err", err)
		os.Exit(1)
	}
	log.Info("clickhouse schema ready")

	// 3. Create and start the BatchWriter and RetrievalWriter.
	writer := ch.NewBatchWriter(conn, log)
	writer.Start(ctx)

	rw := ch.NewRetrievalWriter(conn, log)
	rw.Start(ctx)

	// 4. Register the OTLP TraceService on a gRPC server.
	grpcAddr := fmt.Sprintf(":%d", otlpPort)
	lis, err := net.Listen("tcp", grpcAddr)
	if err != nil {
		log.Error("grpc listen failed", "addr", grpcAddr, "err", err)
		os.Exit(1)
	}

	grpcSrv := grpc.NewServer()
	collectorv1.RegisterTraceServiceServer(grpcSrv, otlpserver.NewServerWithRetrieval(writer, rw, log))

	// 5. Serve in background.
	go func() {
		log.Info("otlp grpc server starting", "addr", grpcAddr)
		if err := grpcSrv.Serve(lis); err != nil {
			log.Error("grpc server error", "err", err)
		}
	}()

	// 6. Block until signal.
	<-ctx.Done()
	log.Info("shutting down collector")

	// 7. Graceful stop.
	grpcSrv.GracefulStop()
	writer.Stop()
	rw.Stop()
	log.Info("collector stopped")
}

// envOrDefault returns the value of the environment variable named by key,
// or fallback if the variable is not set or empty.
func envOrDefault(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}
