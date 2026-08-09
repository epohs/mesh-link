"""mesh-link — the control-socket protocol between mesh-collector and its clients.

This package is a library, not a daemon. It carries the protocol and both ends
of it; the process that owns the serial port is the one that serves, and it does
so inside mesh-collector rather than beside it. That is a settled decision —
running the link as its own daemon would break the invariant that the collector
alone is sufficient, and would make archive-only users run two processes to get
nothing they asked for.

Nothing here imports a radio library or a database driver. The server hands the
collector a validated request and a way to answer it; what a request *means* is
the collector's business, and it stays there.

  Serving, inside the collector:

      server = ControlServer(socket_path)
      server.start()
      while running:
        pending = server.poll(timeout=1.0)
        if pending is not None:
          ...                       # act on pending.request
          pending.respond({...})    # or pending.fail(code, message)

  Asking, from anywhere that may use the radio:

      with ControlClient(socket_path) as link:
        if link.is_available():
          link.send_text("on my way", destination="!eeb826a4")
"""

from mesh_link.client import (
  ControlClient,
  ControlError,
)
from mesh_link.protocol import (
  BROADCAST,
  ERR_BUSY,
  ERR_CHANNEL_NOT_TRACKED,
  ERR_FRAME_TOO_LARGE,
  ERR_INTERNAL,
  ERR_INVALID_REQUEST,
  ERR_SEND_FAILED,
  ERR_TIMEOUT,
  ERR_TX_DISABLED,
  ERR_UNSUPPORTED_VERSION,
  MAX_CHANNEL_INDEX,
  MAX_TEXT_BYTES,
  PROTOCOL_VERSION,
  ProtocolError,
  SendTextRequest,
  StatusRequest,
)
from mesh_link.server import (
  ControlServer,
  ControlSocketBusy,
  ControlSocketPathUnusable,
  PendingRequest,
)
from mesh_link.socket_path import (
  SocketPathTooLong,
  default_socket_path,
  resolve_socket_path,
)


__all__ = [
  "BROADCAST",
  "ControlClient",
  "ControlError",
  "ControlServer",
  "ControlSocketBusy",
  "ControlSocketPathUnusable",
  "ERR_BUSY",
  "ERR_CHANNEL_NOT_TRACKED",
  "ERR_FRAME_TOO_LARGE",
  "ERR_INTERNAL",
  "ERR_INVALID_REQUEST",
  "ERR_SEND_FAILED",
  "ERR_TIMEOUT",
  "ERR_TX_DISABLED",
  "ERR_UNSUPPORTED_VERSION",
  "MAX_CHANNEL_INDEX",
  "MAX_TEXT_BYTES",
  "PROTOCOL_VERSION",
  "PendingRequest",
  "ProtocolError",
  "SendTextRequest",
  "SocketPathTooLong",
  "StatusRequest",
  "default_socket_path",
  "resolve_socket_path",
]
