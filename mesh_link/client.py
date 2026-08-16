from __future__ import annotations

import os
import socket

from pathlib import Path
from typing import Any, Optional

from mesh_link.protocol import (
  BROADCAST,
  MAX_FRAME_BYTES,
  ProtocolError,
  SendTextRequest,
  StatusRequest,
  decode_frame,
  encode_frame,
)
from mesh_link.socket_path import resolve_socket_path


# The asking half. Connects, sends one request, reads one response, closes.
#
# A connection per request rather than a persistent one: requests are rare and
# small, and the alternative means a client holding a slot open across the
# minutes a person spends typing. Reconnecting costs nothing on a Unix socket.
#
# Every failure arrives as a ControlError with a `code` from protocol.py, so a
# caller can tell "you are not allowed to send" from "nothing is listening"
# without matching on message text.

DEFAULT_TIMEOUT = 35.0

# Local to the client: what went wrong on this side, before or after the server
# had a say. Exported from the package alongside protocol.py's codes, and for the
# same reason — a caller branching on `ControlError.code` meets these two exactly
# as often as it meets the server's, and with nothing to import it had no choice
# but to spell them as literals.
ERR_UNREACHABLE = "unreachable"
ERR_BAD_RESPONSE = "bad_response"




class ControlError(RuntimeError):
  """A request that did not succeed, whatever the reason."""

  def __init__(self, code: str, message: str) -> None:
    super().__init__(message)
    self.code = code
    self.message = message




class ControlClient:
  """Talks to a collector's control socket.

  Usable directly or as a context manager; there is no persistent state to
  manage either way, so the context manager is a courtesy rather than a
  requirement.
  """

  def __init__(
    self,
    socket_path: str | os.PathLike[str] | None = None,
    *,
    timeout: float = DEFAULT_TIMEOUT,
  ) -> None:
    self.socket_path: Path = resolve_socket_path(socket_path)
    self.timeout = timeout


  def __enter__(self) -> ControlClient:
    return self


  def __exit__(self, *exc: object) -> None:
    return None




  def is_available(self) -> bool:
    """Whether a collector is listening right now.

    Cheap enough to call before offering someone a compose box, and it puts
    nothing on the air.
    """
    try:
      self.status()
    except ControlError:
      return False
    return True




  def status(self) -> dict[str, Any]:
    """Ask what the collector is and whether it will transmit."""
    return self._exchange(StatusRequest())




  def send_text(
    self,
    text: str,
    *,
    destination: str = BROADCAST,
    channel_index: int = 0,
    want_ack: Optional[bool] = None,
    reply_to: Optional[int] = None,
    emoji: Optional[bool] = None,
  ) -> dict[str, Any]:
    """Ask the collector to transmit, and return what it did.

    The result carries the `message_id` the radio assigned and whether the row
    was archived, so a caller can find what it sent rather than guessing.

    `emoji` asks for a reaction rather than a message and is only meaningful with
    a `reply_to`. Not rejected here when it arrives without one, because the
    collector is the one place that decides what reaches the radio and a second
    opinion on the socket's client side would be a second place to keep in step.
    """
    request = SendTextRequest(
      text=text,
      destination=destination,
      channel_index=channel_index,
      want_ack=want_ack,
      reply_to=reply_to,
      emoji=emoji,
    )
    return self._exchange(request)




  def _exchange(self, request: SendTextRequest | StatusRequest) -> dict[str, Any]:
    payload = self._round_trip(encode_frame(request.to_wire()))

    if not payload.get("ok"):
      error = payload.get("error") or {}
      raise ControlError(
        str(error.get("code", ERR_BAD_RESPONSE)),
        str(error.get("message", "The collector refused the request.")),
      )

    result = payload.get("result")
    if not isinstance(result, dict):
      raise ControlError(
        ERR_BAD_RESPONSE,
        f"Expected an object in `result`, got {type(result).__name__}.",
      )

    return result




  def _round_trip(self, frame: bytes) -> dict[str, Any]:
    conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    conn.settimeout(self.timeout)

    try:
      try:
        conn.connect(str(self.socket_path))
      except FileNotFoundError as e:
        raise ControlError(
          ERR_UNREACHABLE,
          f"No control socket at {self.socket_path}. The collector creates it "
          f"when transmitting is enabled.",
        ) from e
      except ConnectionRefusedError as e:
        raise ControlError(
          ERR_UNREACHABLE,
          f"Nothing is listening on {self.socket_path}; the socket is left over "
          f"from a collector that is no longer running.",
        ) from e
      except PermissionError as e:
        raise ControlError(
          ERR_UNREACHABLE,
          f"Not permitted to use {self.socket_path}. The socket is owned by the "
          f"user the collector runs as, and only that user may transmit.",
        ) from e
      except OSError as e:
        raise ControlError(
          ERR_UNREACHABLE, f"Could not reach {self.socket_path}: {e}"
        ) from e

      try:
        conn.sendall(frame)
        raw = _read_frame(conn)
      except socket.timeout as e:
        raise ControlError(
          ERR_UNREACHABLE,
          f"The collector did not answer within {self.timeout:g}s.",
        ) from e
      except OSError as e:
        raise ControlError(
          ERR_UNREACHABLE, f"Lost the connection to the collector: {e}"
        ) from e

      if raw is None:
        raise ControlError(
          ERR_BAD_RESPONSE, "The collector closed the connection without answering."
        )

      try:
        return decode_frame(raw)
      except ProtocolError as e:
        raise ControlError(e.code, e.message) from e

    finally:
      try:
        conn.close()
      except OSError:
        pass




def _read_frame(conn: socket.socket) -> Optional[bytes]:
  """Read one newline-terminated frame, with the same cap the server applies."""
  buffer = bytearray()

  while True:
    chunk = conn.recv(4096)
    if not chunk:
      return bytes(buffer) if buffer else None

    buffer.extend(chunk)

    newline = buffer.find(b"\n")
    if newline != -1:
      return bytes(buffer[:newline])

    if len(buffer) > MAX_FRAME_BYTES:
      raise ControlError(
        ERR_BAD_RESPONSE,
        f"The collector's reply exceeded {MAX_FRAME_BYTES} bytes.",
      )
