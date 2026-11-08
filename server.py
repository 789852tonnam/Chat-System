"""Socket Chat application: queued handlers, sessions and persistent storage."""

from __future__ import annotations

import argparse
import logging
import math
import queue
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from common.protocol import (
    KNOWN_ACTIONS,
    ProtocolError,
    Request,
    error_response,
    success_response,
    validate_content,
    validate_group_name,
    validate_id,
    validate_job_id,
    validate_room_key,
    validate_snapshot_request,
    validate_sync_request,
    validate_username,
)
from server_network import ClientSession, ServerNetwork
from server_ai import AIConfig, generate_summary, load_ai_config
from database import (
    create_group, get_or_create_user, get_room_snapshot, join_group, list_groups,
    open_database, open_private, require_room, save_summary, send_message,
    set_heart, set_pin, summary_source, sync_room,
)

LOGGER = logging.getLogger("socket_chat.server")
_STOP = object()
JOB_TIMEOUT = 60.0
JOB_RETENTION = 3600.0


@dataclass(frozen=True, slots=True)
class ServerConfig:
    host: str = "0.0.0.0"
    port: int = 5000
    queue_capacity: int = 256
    frame_timeout: float = 10.0
    idle_timeout: float = 120.0
    send_timeout: float = 10.0
    # Persistent user storage; tests pass :memory: to avoid sharing disk state.
    database_path: str = "data/chat.sqlite3"

    def __post_init__(self) -> None:
        if not self.host:
            raise ValueError("host must not be empty")
        if type(self.port) is not int or not 0 <= self.port <= 65535:
            raise ValueError("port must be between 0 and 65535")
        if type(self.queue_capacity) is not int or self.queue_capacity < 1:
            raise ValueError("queue_capacity must be a positive integer")
        if type(self.database_path) is not str or not self.database_path:
            raise ValueError("database_path must be a non-empty string")
        for name in ("frame_timeout", "idle_timeout", "send_timeout"):
            value = getattr(self, name)
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive")


@dataclass(slots=True)
class WorkItem:
    # Parsed wire data and the connection-local session travel in the same job.
    request: Request
    session: ClientSession
    # Wake the originating I/O thread after its response is ready.
    done: threading.Event = field(default_factory=threading.Event)
    # Filled by the worker before it sets done.
    response: dict[str, Any] | None = None


# API threads enqueue immutable completions instead of changing jobs or SQLite.
@dataclass(frozen=True, slots=True)
class SummaryCompletion:
    # Identifies the worker-owned status record.
    job_id: str
    # Validated summary text and actual model, never credentials.
    result: tuple[str, str] | None = None
    # Fixed public failure fields; no upstream body or exception text.
    error: dict | None = None


class ChatServer:
    """Single-use server with explicit start/stop for CLI and TCP integration tests."""

    # Prepare application state and connect the transport to _submit().
    # Construction opens no sockets or database; start() owns that lifecycle.
    def __init__(self, config: ServerConfig | None = None, *, ai_config: AIConfig | None = None):
        # Shared configuration validated before any resources are allocated.
        self.config = config or ServerConfig()
        # I/O enqueues WorkItems; only the application worker handles them.
        self._work: queue.Queue[WorkItem | SummaryCompletion | object] = queue.Queue(self.config.queue_capacity)
        # Shared cancellation wakes wait(), transport and queued response waits.
        self._stopping = threading.Event()
        # Prevents overlapping application startup and shutdown.
        self._lifecycle_lock = threading.Lock()
        # Owns sockets and I/O threads; calls _submit(request, session).
        self._network = ServerNetwork(
            self._submit, self._stopping,
            host=self.config.host, port=self.config.port,
            frame_timeout=self.config.frame_timeout,
            idle_timeout=self.config.idle_timeout,
            send_timeout=self.config.send_timeout,
        )
        # Runs application handlers and owns SQLite from open through close.
        self._worker: threading.Thread | None = None
        # Reports database initialization before transport starts accepting.
        self._worker_ready = threading.Event()
        # Propagates database startup errors to the caller of start().
        self._worker_start_error: Exception | None = None
        # Accessed exclusively by the application worker.
        self._database: sqlite3.Connection | None = None
        # Worker-owned logins; several connections can share the same user ID.
        self._sessions: set[ClientSession] = set()
        # Loaded by CLI only; tests/programmatic callers supply explicit fake config.
        self._ai_config = ai_config
        # Worker-owned transient job statuses; credentials never enter these records.
        self._summary_jobs: dict[str, dict] = {}
        # Worker tracks API threads; they never access SQLite or mutate job state.
        self._ai_threads: set[threading.Thread] = set()
        # Each instance is single-use, including after failed startup.
        self._started = False
        # Makes repeated stop() calls harmless.
        self._closed = False

    # Expose the transport's bound host and port, including port=0 allocation.
    # Access before binding raises RuntimeError in the transport.
    @property
    def address(self) -> tuple[str, int]:
        return self._network.address

    # Initialize storage on its worker before accepting TCP connections.
    # Failed storage or listener startup releases all allocated resources.
    # BaseException also covers Ctrl-C during startup; cleanup then re-raises it.
    def start(self) -> None:
        with self._lifecycle_lock:
            if self._started or self._closed:
                raise RuntimeError("Create a new ChatServer for each run")
            self._started = True
            self._worker = threading.Thread(target=self._worker_loop, name="chat-worker")
            try:
                self._worker.start()
                self._worker_ready.wait()
                if self._worker_start_error is not None:
                    raise self._worker_start_error
                self._network.start()
            except BaseException:
                self._closed = True
                self._stopping.set()
                self._network.stop()
                self._stop_worker()
                raise

    # Block the calling thread until the shutdown Event is set.
    # Background I/O and worker threads continue serving requests meanwhile.
    # In main(), Ctrl-C interrupts this wait and finally calls stop().
    def wait(self) -> None:
        self._stopping.wait()

    # Cancel response waits, stop transport I/O, then finish the worker's cleanup.
    # Already-running handlers finish before SQLite closes; queued requests are
    # not guaranteed to execute. Repeated calls are safe for CLI finally blocks.
    def stop(self) -> None:
        with self._lifecycle_lock:
            if self._closed:
                return
            self._closed = True
            self._stopping.set()
            self._network.stop()
            self._stop_worker()

    # Wake an idle worker and wait for its database/session cleanup to finish.
    # A full queue needs no sentinel because the loop also checks shutdown.
    # Handle partial startup where a worker object exists but never started.
    def _stop_worker(self) -> None:
        try:
            self._work.put_nowait(_STOP)
        except queue.Full:
            pass
        if self._worker is not None and self._worker.ident is not None and self._worker is not threading.current_thread():
            self._worker.join()

    # Wrap a request in a WorkItem with its own response slot and completion Event.
    # Called by an I/O thread: enqueue without blocking, then wait for the worker
    # to fill the response and signal done. A full queue returns SERVER_BUSY
    # without executing the request; shutdown while waiting returns None.
    def _submit(self, request: Request, session: ClientSession) -> dict[str, Any] | None:
        item = WorkItem(request, session)
        try:
            self._work.put_nowait(item)
        except queue.Full:
            return error_response(request.request_id, "SERVER_BUSY", "Server work queue is full")
        while not item.done.wait(0.1):
            if self._stopping.is_set():
                return None
        return item.response

    # Run on the single worker thread and process queued requests one at a time.
    # Store each result on its WorkItem and set done to wake the correct I/O thread.
    # Handler exceptions become INTERNAL_ERROR without killing this worker.
    # Own SQLite on this thread from initialization through final close.
    # Report startup failure before accepting clients, and always mark dequeued
    # items complete; the stop sentinel ends the loop.
    # Idle ticks prune sessions and enforce AI deadlines/status retention.
    def _worker_loop(self) -> None:
        try:
            self._database = open_database(self.config.database_path)
        except Exception as exc:
            self._worker_start_error = exc
            self._worker_ready.set()
            return
        self._worker_ready.set()
        try:
            while not self._stopping.is_set():
                try:
                    item = self._work.get(timeout=1.0)
                except queue.Empty:
                    self._prune_sessions()
                    self._maintain_summary_jobs()
                    continue
                try:
                    if item is _STOP:
                        return
                    self._maintain_summary_jobs()
                    if isinstance(item, SummaryCompletion):
                        self._finish_summary(item)
                        continue
                    assert isinstance(item, WorkItem)
                    try:
                        self._prune_sessions()
                        item.response = self._handle_request(item.request, item.session)
                    except ProtocolError as exc:
                        item.response = error_response(item.request.request_id, exc.code, exc.message, details=exc.details)
                    except Exception:
                        LOGGER.exception("Worker request failed")
                        item.response = error_response(
                            item.request.request_id, "INTERNAL_ERROR", "Server could not process this request"
                        )
                    finally:
                        item.done.set()
                finally:
                    self._work.task_done()
        finally:
            self._sessions.clear()
            self._summary_jobs.clear()
            self._database.close()
            self._database = None

    # Remove closed connections on the worker, without sharing a mutable registry
    # with I/O threads. Run before requests and on the idle maintenance tick.
    # Persistent users stay in SQLite; this only changes online visibility.
    def _prune_sessions(self) -> None:
        self._sessions = {session for session in self._sessions if not session.closed.is_set()}

    # Accept one independent job after membership/config/source checks on the worker.
    # Only detached human text crosses to the API thread; acknowledge without waiting.
    def _request_summary(self, room_key: str, user_id: int) -> dict:
        require_room(self._database, room_key, user_id)
        if self._ai_config is None:
            raise ProtocolError("AI_NOT_CONFIGURED", "AI summaries are not configured on the server")
        messages = summary_source(self._database, room_key, user_id)
        if not messages:
            raise ProtocolError("NO_HUMAN_MESSAGES", "No human messages to summarize")
        job_id = str(uuid.uuid4())
        job = {"job_id": job_id, "room_key": room_key, "state": "running",
               "created_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
               "completed_at": None, "result_message_id": None, "error": None,
               "_user_id": user_id, "_source_id": messages[-1]["id"],
               "_deadline": time.monotonic() + JOB_TIMEOUT, "_finished": None}
        self._summary_jobs[job_id] = job
        # shortcut: urllib cannot cancel in-flight HTTPS; daemon threads drop results
        # on shutdown and finish on network timeout. Upgrade if strict cancellation is needed.
        thread = threading.Thread(target=self._run_summary, args=(job_id, messages), name=f"chat-ai-{job_id}", daemon=True)
        self._ai_threads.add(thread)
        try:
            thread.start()
        except RuntimeError:
            self._ai_threads.discard(thread)
            del self._summary_jobs[job_id]
            raise ProtocolError("AI_UNAVAILABLE", "Could not start the summary request") from None
        return {"job_id": job_id, "state": "running"}

    # Run on one API thread. Return only validated output or fixed public errors.
    # Completion uses the central bounded queue, retrying queue insertion only.
    def _run_summary(self, job_id: str, messages: list[dict]) -> None:
        if self._stopping.is_set():
            return
        try:
            result = generate_summary(self._ai_config, messages)
            completion = SummaryCompletion(job_id, result=result)
        except ProtocolError as exc:
            completion = SummaryCompletion(job_id, error={"code": exc.code, "message": exc.message, **(exc.details or {})})
        except Exception:
            completion = SummaryCompletion(job_id, error={"code": "AI_FAILED", "message": "AI summary failed"})
        while not self._stopping.is_set():
            try:
                self._work.put(completion, timeout=0.2)
                return
            except queue.Full:
                continue

    # Complete a job on the worker only; finished/deadline-expired jobs ignore late
    # or duplicate results. The database also enforces one message per summary job.
    def _finish_summary(self, completion: SummaryCompletion) -> None:
        job = self._summary_jobs.get(completion.job_id)
        if job is None or job["state"] != "running":
            return
        error = completion.error
        message_id = None
        if error is None:
            try:
                content, model = completion.result
                message_id = save_summary(self._database, job["room_key"], content, model, job["job_id"], job["_source_id"])
            except Exception:
                error = {"code": "AI_FAILED", "message": "Could not save the AI summary"}
        self._end_summary(job, message_id, error)

    # Record a terminal status without creating an error message in the room.
    def _end_summary(self, job: dict, message_id: int | None, error: dict | None) -> None:
        job.update(state="failed" if error else "succeeded", result_message_id=message_id, error=error,
                   completed_at=datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                   _finished=time.monotonic())

    # Enforce deadlines during idle ticks and before requests/completions. Expire
    # terminal statuses after an hour; persisted AI messages are never removed here.
    def _maintain_summary_jobs(self) -> None:
        now = time.monotonic()
        for job_id, job in list(self._summary_jobs.items()):
            if job["state"] == "running" and now >= job["_deadline"]:
                self._end_summary(job, None, {"code": "AI_TIMEOUT", "message": "AI summary deadline exceeded"})
            elif job["_finished"] is not None and now - job["_finished"] >= JOB_RETENTION:
                del self._summary_jobs[job_id]
        self._ai_threads = {thread for thread in self._ai_threads if thread.is_alive()}

    # Dispatch an application request on the worker thread; do not send socket data.
    # Unknown names return UNKNOWN_ACTION; known but unfinished actions explicitly
    # return ACTION_NOT_IMPLEMENTED. Login creates/reuses a persistent username
    # and binds it to this connection; logout clears only this connection's user.
    # Passwords and a separate registration step are intentionally absent.
    def _handle_request(self, request: Request, session: ClientSession) -> dict[str, Any]:
        """Only the central worker calls application handlers."""
        if request.action not in KNOWN_ACTIONS:
            return error_response(request.request_id, "UNKNOWN_ACTION", "Unknown action")
        if request.action not in {"ping", "login", "logout", "get_directory", "open_private",
                                  "create_group", "join_group", "send_message", "sync_room",
                                  "get_room_snapshot", "set_heart", "set_pin", "request_summary", "get_summary_status"}:
            return error_response(
                request.request_id,
                "ACTION_NOT_IMPLEMENTED",
                "This server build does not implement this action yet",
            )
        if request.action == "login":
            if session.user is not None:
                raise ProtocolError("ALREADY_AUTHENTICATED", "This connection has already joined")
            if set(request.payload) != {"username"}:
                raise ProtocolError("INVALID_PAYLOAD", "login requires only username")
            username = validate_username(request.payload["username"])
            assert self._database is not None
            session.user = get_or_create_user(self._database, username)
            self._sessions.add(session)
            return success_response(request.request_id, {"user": session.user})
        if request.action not in {"ping", "logout"} and session.user is None:
            raise ProtocolError("UNAUTHENTICATED", "Join with a username before using this action")
        if request.action in {"ping", "logout", "get_directory"} and request.payload:
            raise ProtocolError("INVALID_PAYLOAD", f"{request.action} requires an empty payload")
        if request.action == "logout":
            self._sessions.discard(session)
            session.user = None
            return success_response(request.request_id, {"logged_out": True})
        if request.action == "get_directory":
            users = {active.user["id"]: active.user for active in self._sessions}
            online_users = sorted(users.values(), key=lambda user: (user["username"].lower(), user["id"]))
            return success_response(request.request_id, {"online_users": online_users, "groups": list_groups(self._database)})
        if request.action == "open_private":
            if set(request.payload) != {"target_user_id"}:
                raise ProtocolError("INVALID_PAYLOAD", "open_private requires only target_user_id")
            target = validate_id(request.payload["target_user_id"], "target_user_id")
            return success_response(request.request_id, open_private(self._database, session.user["id"], target))
        if request.action == "create_group":
            if set(request.payload) != {"name"}:
                raise ProtocolError("INVALID_PAYLOAD", "create_group requires only name")
            name = validate_group_name(request.payload["name"])
            return success_response(request.request_id, create_group(self._database, session.user["id"], name))
        if request.action == "join_group":
            if set(request.payload) != {"room_key"}:
                raise ProtocolError("INVALID_PAYLOAD", "join_group requires only room_key")
            room_key = validate_room_key(request.payload["room_key"])
            return success_response(request.request_id, join_group(self._database, session.user["id"], room_key))
        if request.action == "send_message":
            if set(request.payload) != {"room_key", "content"}:
                raise ProtocolError("INVALID_PAYLOAD", "send_message requires room_key and content")
            room_key = validate_room_key(request.payload["room_key"])
            content = validate_content(request.payload["content"])
            return success_response(request.request_id, send_message(self._database, room_key, session.user["id"], content))
        if request.action == "sync_room":
            validate_sync_request(request.payload)
            return success_response(request.request_id, sync_room(self._database, session.user["id"], request.payload))
        if request.action == "get_room_snapshot":
            validate_snapshot_request(request.payload)
            return success_response(request.request_id, get_room_snapshot(self._database, session.user["id"], request.payload))
        if request.action == "request_summary":
            if set(request.payload) != {"room_key"}:
                raise ProtocolError("INVALID_PAYLOAD", "request_summary requires only room_key")
            room_key = validate_room_key(request.payload["room_key"])
            return success_response(request.request_id, self._request_summary(room_key, session.user["id"]))
        if request.action == "get_summary_status":
            if set(request.payload) != {"job_id"}:
                raise ProtocolError("INVALID_PAYLOAD", "get_summary_status requires only job_id")
            job_id = validate_job_id(request.payload["job_id"])
            job = self._summary_jobs.get(job_id)
            if job is None or job["_user_id"] != session.user["id"]:
                raise ProtocolError("SUMMARY_JOB_NOT_FOUND", "Summary job not found")
            require_room(self._database, job["room_key"], session.user["id"])
            return success_response(request.request_id, {"job": {name: value for name, value in job.items() if not name.startswith("_")}})
        if request.action in {"set_heart", "set_pin"}:
            expected = {"room_key", "message_id", "hearted"} if request.action == "set_heart" else {"room_key", "message_id"}
            if set(request.payload) != expected:
                raise ProtocolError("INVALID_PAYLOAD", "Invalid mutation fields")
            room_key = validate_room_key(request.payload["room_key"])
            message_id = request.payload["message_id"]
            if request.action == "set_heart" or message_id is not None:
                validate_id(message_id, "message_id")
            if request.action == "set_heart":
                if type(request.payload["hearted"]) is not bool:
                    raise ProtocolError("INVALID_PAYLOAD", "hearted must be a boolean")
                data = set_heart(self._database, room_key, session.user["id"], message_id, request.payload["hearted"])
            else:
                data = set_pin(self._database, room_key, session.user["id"], message_id)
            return success_response(request.request_id, data)
        return success_response(request.request_id, {"pong": True})



# Parse listener/storage settings and keep the process alive after startup.
# Always stop an allocated server, including interruption while SQLite opens.
# Startup errors become CLI errors; Ctrl-C closes owned sockets and threads.
def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument("--database", default="data/chat.sqlite3")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    server = None
    try:
        server = ChatServer(ServerConfig(host=args.host, port=args.port, database_path=args.database), ai_config=load_ai_config())
        server.start()
        LOGGER.info("Listening on %s:%s", *server.address)
        server.wait()
    except KeyboardInterrupt:
        pass
    except (OSError, ValueError, sqlite3.Error) as exc:
        parser.exit(1, f"Cannot start server: {exc}\n")
    finally:
        if server is not None:
            server.stop()


if __name__ == "__main__":
    main()
