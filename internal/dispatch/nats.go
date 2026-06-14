// Package dispatch handles task distribution via NATS JetStream.
package dispatch

import (
	"context"
	"errors"
	"fmt"

	"github.com/nats-io/nats.go"
	helixv1 "github.com/tomiwaaluko/helix/gen/go/helix/v1"
	"google.golang.org/protobuf/proto"
)

// Publisher is the minimal interface the API layer needs from the dispatcher.
type Publisher interface {
	PublishTaskEnvelope(ctx context.Context, pool string, env *helixv1.TaskEnvelope) error
}

const (
	streamName      = "HELIX_TASKS"
	streamSubjects  = "helix.tasks.dispatch.*"
	completionSubj  = "helix.events.completion"
	durableConsumer = "orchestrator-completions"
)

// Client wraps a NATS connection and JetStream context for task dispatch.
type Client struct {
	conn *nats.Conn
	js   nats.JetStreamContext
}

// NewClient connects to NATS, obtains a JetStream context, and ensures the
// HELIX_TASKS stream exists.
func NewClient(url string) (*Client, error) {
	conn, err := nats.Connect(url)
	if err != nil {
		return nil, fmt.Errorf("dispatch: connect to NATS at %q: %w", url, err)
	}

	js, err := conn.JetStream()
	if err != nil {
		conn.Close()
		return nil, fmt.Errorf("dispatch: get JetStream context: %w", err)
	}

	_, err = js.AddStream(&nats.StreamConfig{
		Name:     streamName,
		Subjects: []string{streamSubjects},
	})
	if err != nil && !errors.Is(err, nats.ErrStreamNameAlreadyInUse) {
		conn.Close()
		return nil, fmt.Errorf("dispatch: ensure stream %q: %w", streamName, err)
	}

	return &Client{conn: conn, js: js}, nil
}

// PublishTaskEnvelope marshals a TaskEnvelope proto and publishes it to
// helix.tasks.dispatch.<pool>.
func (c *Client) PublishTaskEnvelope(ctx context.Context, pool string, env *helixv1.TaskEnvelope) error {
	data, err := proto.Marshal(env)
	if err != nil {
		return fmt.Errorf("dispatch: marshal TaskEnvelope: %w", err)
	}

	subject := "helix.tasks.dispatch." + pool
	if _, err := c.js.Publish(subject, data); err != nil {
		return fmt.Errorf("dispatch: publish to %q: %w", subject, err)
	}
	return nil
}

// SubscribeCompletions subscribes to helix.events.completion on a durable
// consumer and calls handler for each received TaskResult. The subscription
// runs in a background goroutine; errors from handler are logged via the
// returned error channel. The goroutine exits when ctx is cancelled.
func (c *Client) SubscribeCompletions(ctx context.Context, handler func(result *helixv1.TaskResult) error) error {
	sub, err := c.js.QueueSubscribe(
		completionSubj,
		durableConsumer,
		func(msg *nats.Msg) {
			var result helixv1.TaskResult
			if unmarshalErr := proto.Unmarshal(msg.Data, &result); unmarshalErr != nil {
				// Nak so it can be retried; do not ack malformed messages.
				_ = msg.Nak()
				return
			}
			if handlerErr := handler(&result); handlerErr != nil {
				_ = msg.Nak()
				return
			}
			_ = msg.Ack()
		},
		nats.Durable(durableConsumer),
		nats.ManualAck(),
	)
	if err != nil {
		return fmt.Errorf("dispatch: subscribe to completions: %w", err)
	}

	go func() {
		<-ctx.Done()
		_ = sub.Unsubscribe()
	}()

	return nil
}

// Close drains and closes the underlying NATS connection.
func (c *Client) Close() {
	_ = c.conn.Drain()
	c.conn.Close()
}
