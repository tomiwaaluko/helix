"""Remote engine — connects a Python worker to the Go orchestrator.

The worker subscribes to NATS for TaskEnvelopes, executes the workflow handler
in local mode (no current_engine set), and reports completion via gRPC
CompleteTask. The Go orchestrator owns all state; this process owns execution.

Usage (via ``make worker`` / ``python -m helix.worker``):

    async with RemoteEngine(
        orchestrator_url="grpc://localhost:50051",
        nats_url="nats://localhost:4222",
        pool="research",
    ) as engine:
        engine.register_workflow("deep_research", deep_research_handler)
        await engine.run()
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import Callable, Coroutine
from types import TracebackType
from typing import Any

import grpc
import nats
import nats.js
import nats.js.errors

from helix.logging import SpanLogger

# Proto stubs live in helix/v1/ — generated, do not edit by hand.
from helix.v1 import orchestrator_pb2, orchestrator_pb2_grpc, types_pb2

logger = logging.getLogger(__name__)

WorkflowHandler = Callable[..., Coroutine[Any, Any, Any]]


class RemoteEngine:
    """Manages the gRPC + NATS worker lifecycle."""

    def __init__(
        self,
        *,
        orchestrator_url: str,
        nats_url: str,
        pool: str,
    ) -> None:
        # Strip scheme for gRPC target
        grpc_target = orchestrator_url.removeprefix("grpc://").removeprefix("grpcs://")
        self._grpc_target = grpc_target
        self._nats_url = nats_url
        self._pool = pool
        self._workflows: dict[str, WorkflowHandler] = {}
        self._worker_id: str = ""
        self._channel: grpc.aio.Channel | None = None
        self._stub: orchestrator_pb2_grpc.OrchestratorStub | None = None
        self._nc: nats.NATS | None = None
        self._js: nats.js.JetStreamContext | None = None
        self._span_logger = SpanLogger()
        self._running = False

    def register_workflow(self, name: str, handler: WorkflowHandler) -> None:
        self._workflows[name] = handler

    async def __aenter__(self) -> RemoteEngine:
        await self._connect()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        await self._disconnect()

    async def _connect(self) -> None:
        self._channel = grpc.aio.insecure_channel(self._grpc_target)
        self._stub = orchestrator_pb2_grpc.OrchestratorStub(self._channel)

        resp = await self._stub.RegisterWorker(
            orchestrator_pb2.RegisterWorkerRequest(
                pool=self._pool,
                capabilities=list(self._workflows.keys()),
                max_concurrency=4,
            )
        )
        self._worker_id = resp.worker_id
        logger.info("registered worker %s in pool %s", self._worker_id, self._pool)

        self._nc = await nats.connect(self._nats_url)
        self._js = self._nc.jetstream()

    async def _disconnect(self) -> None:
        self._running = False
        if self._nc is not None:
            await self._nc.drain()
            self._nc = None
        if self._channel is not None:
            await self._channel.close()
            self._channel = None

    async def run(self) -> None:
        """Enter the NATS consume loop. Blocks until cancelled."""
        if self._js is None or self._stub is None:
            raise RuntimeError("RemoteEngine not connected; use async with")

        self._running = True
        subject = f"helix.tasks.dispatch.{self._pool}"

        # Start heartbeat background task
        hb_task = asyncio.create_task(self._heartbeat_loop())

        try:
            sub = await self._js.subscribe(subject, durable=f"worker-{self._pool}")
            async for msg in sub.messages:
                if not self._running:
                    await msg.nak()
                    break
                envelope = types_pb2.TaskEnvelope()
                envelope.ParseFromString(msg.data)
                await msg.ack()
                asyncio.create_task(self._handle_envelope(envelope))
        finally:
            hb_task.cancel()
            try:
                await hb_task
            except asyncio.CancelledError:
                pass

    async def _heartbeat_loop(self) -> None:
        """Send heartbeats every 10 seconds."""
        if self._stub is None:
            return

        async def request_iter() -> Any:
            while self._running:
                yield orchestrator_pb2.HeartbeatRequest(
                    worker_id=self._worker_id,
                    in_flight=0,
                    capacity=4,
                )
                await asyncio.sleep(10)

        try:
            async for _ in self._stub.Heartbeat(request_iter()):
                pass
        except grpc.aio.AioRpcError as e:
            logger.warning("heartbeat stream ended: %s", e)

    async def _handle_envelope(self, envelope: types_pb2.TaskEnvelope) -> None:
        """Execute a single task envelope and report completion."""
        task_id = envelope.task_id
        handler = self._workflows.get(envelope.workflow_name)

        if handler is None:
            await self._complete(
                task_id=task_id,
                attempt=envelope.attempt_number,
                status="failed",
                output=b"{}",
                error=f"unknown workflow {envelope.workflow_name!r}",
                span_id="",
                duration_ms=0,
            )
            return

        input_data: dict[str, Any] = json.loads(envelope.input_json or b"{}")
        start_ms = int(time.monotonic() * 1000)
        span_id = uuid.uuid4().hex

        try:
            result = await handler(**input_data)
            duration_ms = int(time.monotonic() * 1000) - start_ms
            output_json = json.dumps(
                result if isinstance(result, dict) else {"result": str(result)}
            ).encode()
            await self._complete(
                task_id=task_id,
                attempt=envelope.attempt_number,
                status="succeeded",
                output=output_json,
                error="",
                span_id=span_id,
                duration_ms=duration_ms,
            )
        except Exception as exc:  # noqa: BLE001
            duration_ms = int(time.monotonic() * 1000) - start_ms
            logger.exception("task %s failed: %s", task_id, exc)
            await self._complete(
                task_id=task_id,
                attempt=envelope.attempt_number,
                status="failed",
                output=b"{}",
                error=str(exc),
                span_id=span_id,
                duration_ms=duration_ms,
            )

    async def _complete(
        self,
        *,
        task_id: str,
        attempt: int,
        status: str,
        output: bytes,
        error: str,
        span_id: str,
        duration_ms: int,
    ) -> None:
        if self._stub is None:
            return
        result = types_pb2.TaskResult(
            task_id=task_id,
            attempt_number=attempt,
            worker_id=self._worker_id,
            status=status,
            output_json=output,
            error=error,
            span_id=span_id,
            duration_ms=duration_ms,
        )
        try:
            resp = await self._stub.CompleteTask(
                orchestrator_pb2.CompleteTaskRequest(result=result)
            )
            logger.info("task %s completed: %s", task_id, resp.disposition)
        except grpc.aio.AioRpcError as e:
            logger.error("CompleteTask RPC failed for %s: %s", task_id, e)
