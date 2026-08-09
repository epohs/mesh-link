from __future__ import annotations

import logging
import os
import queue
import socket
import stat
import threading

from pathlib import Path
from typing import Any, Optional

from mesh_link.protocol import (
  ERR_BUSY,
  ERR_INTERNAL,
  ERR_TIMEOUT,
  MAX_FRAME_BYTES,
  ProtocolError,
  SendTextRequest,
  StatusRequest,
  decode_frame,
  encode_frame,
  error_response,
  ok_response,
  parse_request,
)
from mesh_link.socket_path import resolve_socket_path


# The listening half of the control socket, and the arrangement that keeps it
# away from everything it must not touch.
#
# Three threads' worth of responsibility, kept apart on purpose:
#
#   accept thread      owns the listening socket, does nothing but accept
#   connection thread  one per client; reads, parses, writes, and catches
#                      absolutely everything so a client that vanishes mid-write
#                      cannot raise anywhere that matters
#   the caller's own   drains poll() and is the only thread that acts on a
#                      request — the only one that touches a radio or a database
#
# Nothing in this module imports a radio library or a database driver, and it
# never learns what a request means. It hands the caller a validated request and
# a way to answer it, and the caller decides everything else.


DEFAULT_REQUEST_TIMEOUT = 30.0

# Two limits, doing two different jobs, and the order between them matters.
#
# The queue is the real backpressure: it is how many requests may be waiting on
# the single consumer, and a client that arrives when it is full is told `busy`
# and can try again. The connection cap is only a guard against running out of
# file descriptors and threads.
#
# So the cap must stay comfortably above the queue, or it becomes the binding
# constraint and clients get refused while queue slots sit empty — which is the
# opposite of what the queue is for. Connection threads are cheap: each one
# spends its life blocked on an Event waiting for the drain.
DEFAULT_QUEUE_SIZE = 32
DEFAULT_MAX_CONNECTIONS = 64
LISTEN_BACKLOG = 16

# How long a connection thread will wait on a client that has opened a socket
# and then said nothing.
CLIENT_READ_TIMEOUT = 30.0




class ControlSocketBusy(RuntimeError):
  """Raised when the socket path is already being served by a live process."""




class ControlSocketPathUnusable(RuntimeError):
  """Raised when the socket path exists but is not a socket.

  Kept distinct from a stale socket because the response is different: a stale
  socket is cleaned up automatically, whereas some other kind of file at that
  path is a configuration mistake and removing it would be presumptuous.
  """




class PendingRequest:
  """A validated request, plus the only way to answer it.

  Handed to the draining thread. Exactly one of respond()/fail() takes effect;
  later calls are ignored, so a caller that answers in a `finally` after already
  answering cannot overwrite a real result with a generic one.
  """

  def __init__(self, request: SendTextRequest | StatusRequest) -> None:
    self.request = request
    self._reply: Optional[dict[str, Any]] = None
    self._ready = threading.Event()


  def respond(self, result: dict[str, Any]) -> None:
    self._settle(ok_response(result))


  def fail(self, code: str, message: str) -> None:
    self._settle(error_response(code, message))


  def _settle(self, reply: dict[str, Any]) -> None:
    if self._ready.is_set():
      return
    self._reply = reply
    self._ready.set()


  def _await_reply(self, timeout: float) -> dict[str, Any]:
    if not self._ready.wait(timeout):
      return error_response(
        ERR_TIMEOUT,
        f"The collector did not answer within {timeout:g}s.",
      )
    return self._reply if self._reply is not None else error_response(
      ERR_INTERNAL, "The collector settled the request without a reply."
    )




class ControlServer:
  """Listens on a Unix socket and queues validated requests for one consumer.

  Construction does nothing; start() binds. The caller drains with poll() and
  answers through the PendingRequest it receives.
  """

  def __init__(
    self,
    socket_path: str | os.PathLike[str] | None = None,
    *,
    queue_size: int = DEFAULT_QUEUE_SIZE,
    request_timeout: float = DEFAULT_REQUEST_TIMEOUT,
    max_connections: int = DEFAULT_MAX_CONNECTIONS,
    logger: Optional[logging.Logger] = None,
  ) -> None:
    self.socket_path: Path = resolve_socket_path(socket_path)
    self.request_timeout = request_timeout
    self.log = logger or logging.getLogger(__name__)

    # See the note on the two limits above. A cap below the queue size would
    # quietly turn "wait your turn" into "refused", so it is raised rather than
    # honoured — the caller asking for a small cap wants a guard, not a policy.
    if max_connections < queue_size:
      max_connections = queue_size

    self._queue: queue.Queue[PendingRequest] = queue.Queue(maxsize=queue_size)
    self._sock: Optional[socket.socket] = None
    self._accept_thread: Optional[threading.Thread] = None
    self._slots = threading.BoundedSemaphore(max_connections)
    self._running = False




  def start(self) -> None:
    """Bind, listen, and start accepting. Safe to call once."""
    if self._running:
      return

    self._prepare_path()

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)

    # umask rather than a chmod after the fact, so the socket is never briefly
    # world-writable. This runs at startup, before any other thread exists,
    # which is what makes touching a process-global safe here.
    previous_umask = os.umask(0o177)
    try:
      sock.bind(str(self.socket_path))
    finally:
      os.umask(previous_umask)

    # Belt and braces: umask can only clear bits, and this states the intent.
    os.chmod(self.socket_path, 0o600)

    sock.listen(LISTEN_BACKLOG)
    # So stop() is noticed promptly rather than at the next connection.
    sock.settimeout(0.5)

    self._sock = sock
    self._running = True

    self._accept_thread = threading.Thread(
      target=self._accept_loop,
      name="mesh-link-accept",
      daemon=True,
    )
    self._accept_thread.start()

    self.log.info("Control socket listening on %s (mode 0600)", self.socket_path)




  def stop(self) -> None:
    """Stop accepting and remove the socket file.

    Removing it here is what keeps the next start() from having to guess whether
    a leftover file is stale. A process killed outright cannot run this, which is
    exactly the case _prepare_path() handles.
    """
    if not self._running:
      return

    self._running = False

    if self._sock is not None:
      try:
        self._sock.close()
      except OSError:
        pass
      self._sock = None

    if self._accept_thread is not None:
      self._accept_thread.join(timeout=2.0)
      self._accept_thread = None

    try:
      self.socket_path.unlink(missing_ok=True)
    except OSError as e:
      self.log.warning("Could not remove control socket %s: %s", self.socket_path, e)

    self.log.info("Control socket closed")




  def poll(self, timeout: float = 1.0) -> Optional[PendingRequest]:
    """Next queued request, or None if none arrived within `timeout`.

    The caller is the single consumer, which is what serializes sends without
    needing a lock around the radio.
    """
    try:
      return self._queue.get(timeout=timeout)
    except queue.Empty:
      return None




  def _prepare_path(self) -> None:
    """Make the directory, and clear a socket left behind by a dead process.

    A leftover file is only removed once it is known to be a socket that nothing
    is listening on. Blindly unlinking would let a second collector steal the
    socket out from under a running one, and would delete whatever else happened
    to be at a mistyped path.
    """
    self.socket_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)

    try:
      mode = self.socket_path.lstat().st_mode
    except FileNotFoundError:
      return

    if not stat.S_ISSOCK(mode):
      raise ControlSocketPathUnusable(
        f"{self.socket_path} exists and is not a socket. Refusing to remove it; "
        f"point the socket path somewhere else."
      )

    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    probe.settimeout(0.5)
    try:
      probe.connect(str(self.socket_path))
    except (ConnectionRefusedError, FileNotFoundError):
      self.log.warning("Removing stale control socket %s", self.socket_path)
      self.socket_path.unlink(missing_ok=True)
    except OSError as e:
      raise ControlSocketBusy(
        f"Cannot determine whether {self.socket_path} is in use ({e}). "
        f"Remove it by hand if no collector is running."
      ) from e
    else:
      raise ControlSocketBusy(
        f"Another process is already listening on {self.socket_path}. "
        f"Only one collector can own the radio."
      )
    finally:
      probe.close()




  def _accept_loop(self) -> None:
    while self._running:
      sock = self._sock
      if sock is None:
        return

      try:
        conn, _ = sock.accept()
      except socket.timeout:
        continue
      except OSError:
        # Expected when stop() closes the listener out from under accept().
        if self._running:
          self.log.exception("Control socket accept failed")
        return
      except Exception:
        self.log.exception("Unexpected error accepting on the control socket")
        continue

      if not self._slots.acquire(blocking=False):
        self._refuse(conn)
        continue

      threading.Thread(
        target=self._serve_connection,
        args=(conn,),
        name="mesh-link-conn",
        daemon=True,
      ).start()




  def _refuse(self, conn: socket.socket) -> None:
    """Turn away a client without occupying a slot to do it."""
    try:
      conn.sendall(
        encode_frame(
          error_response(ERR_BUSY, "Too many control connections; try again.")
        )
      )
    except OSError:
      pass
    finally:
      _close_quietly(conn)




  def _serve_connection(self, conn: socket.socket) -> None:
    """One request, one response, one connection.

    This is the boundary the whole arrangement exists to hold: every failure a
    client can cause is caught here. Nothing raised in this thread reaches the
    accept loop, the drain, or the collector's packet ingest.
    """
    try:
      conn.settimeout(CLIENT_READ_TIMEOUT)
      reply = self._handle(conn)
      if reply is not None:
        conn.sendall(encode_frame(reply))
    except (OSError, socket.timeout):
      # A client that hung up or stalled mid-exchange. Ordinary, not an error.
      self.log.debug("Control client disconnected before the exchange finished")
    except Exception:
      self.log.exception("Unhandled error on a control connection")
    finally:
      _close_quietly(conn)
      self._slots.release()




  def _handle(self, conn: socket.socket) -> Optional[dict[str, Any]]:
    """Read and validate one frame, then wait for the drain to answer it."""
    try:
      raw = _read_frame(conn)
    except ProtocolError as e:
      # An over-long frame means the client is still writing when we stop
      # reading. Closing now would burst its socket buffer and it would see a
      # broken pipe instead of the reason it was refused — so let it finish, up
      # to a bound, and then answer properly. A limit that produces an
      # unreadable error is only half a limit.
      _discard_remaining(conn)
      return error_response(e.code, e.message)

    if raw is None:
      # Clean disconnect before sending anything. Nothing to answer.
      return None

    try:
      payload = decode_frame(raw)
      request = parse_request(payload)
    except ProtocolError as e:
      return error_response(e.code, e.message)

    pending = PendingRequest(request)

    try:
      self._queue.put_nowait(pending)
    except queue.Full:
      return error_response(
        ERR_BUSY,
        "The collector's send queue is full; try again shortly.",
      )

    return pending._await_reply(self.request_timeout)




def _read_frame(conn: socket.socket) -> Optional[bytes]:
  """Read up to the first newline, refusing anything oversized.

  Returns None if the client closed without sending anything. Reads a chunk at a
  time rather than through makefile() so the byte cap is enforced as the data
  arrives, not after a client has already made us buffer it.
  """
  buffer = bytearray()

  while True:
    chunk = conn.recv(4096)
    if not chunk:
      if not buffer:
        return None
      # Ran out without a newline; treat what arrived as the frame so a client
      # that forgot the terminator gets a real error rather than silence.
      return bytes(buffer)

    buffer.extend(chunk)

    newline = buffer.find(b"\n")
    if newline != -1:
      return bytes(buffer[:newline])

    if len(buffer) > MAX_FRAME_BYTES:
      raise ProtocolError(
        "frame_too_large",
        f"No newline within {MAX_FRAME_BYTES} bytes; giving up on this frame.",
      )




def _discard_remaining(conn: socket.socket, limit: int = 4 * MAX_FRAME_BYTES) -> None:
  """Read and throw away what a rejected client is still sending.

  Bounded in both bytes and time, because the client being humoured here is by
  definition one that already sent more than it should have. Anything that goes
  wrong is ignored: this runs only to make the error frame deliverable, and
  failing to deliver it is no worse than not trying.
  """
  previous_timeout = conn.gettimeout()
  seen = 0
  try:
    conn.settimeout(0.5)
    while seen < limit:
      chunk = conn.recv(4096)
      if not chunk:
        return
      seen += len(chunk)
  except OSError:
    return
  finally:
    try:
      conn.settimeout(previous_timeout)
    except OSError:
      pass




def _close_quietly(conn: socket.socket) -> None:
  try:
    conn.shutdown(socket.SHUT_RDWR)
  except OSError:
    pass
  try:
    conn.close()
  except OSError:
    pass
