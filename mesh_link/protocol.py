from __future__ import annotations

import json
import re

from dataclasses import dataclass
from typing import Any, Optional


# The wire protocol spoken over mesh-collector's control socket.
#
# One JSON object per line, in both directions. Newline framing is safe here
# because json.dumps always escapes control characters, so an encoded frame can
# never contain a literal newline no matter what a message says.
#
# This module is the whole contract. It knows nothing about radios, sockets or
# databases — it validates shapes and limits, and both ends import the same
# definitions so neither can drift from the other.

PROTOCOL_VERSION = 1

# A frame is a single short text message. The cap exists so a client that never
# sends a newline cannot make the server buffer without bound.
MAX_FRAME_BYTES = 65536

# meshtastic's mesh_pb2.Constants.DATA_PAYLOAD_LEN. Checked here, against the
# UTF-8 encoding rather than the character count, so an over-long message is a
# protocol error with a useful message instead of a MeshInterfaceError raised
# from inside the send call.
MAX_TEXT_BYTES = 233

# Channel indexes a Meshtastic device can hold.
MAX_CHANNEL_INDEX = 7

# meshtastic's BROADCAST_ADDR.
BROADCAST = "^all"

# Hex node ids, the form nodes.node_id uses. Deliberately strict: these two
# destination forms are the only ones meshtastic's _sendPacket resolves without
# consulting its node database, and the paths that *do* consult it call
# our_exit() — that is sys.exit() — when the lookup fails. Validating here keeps
# a client's typo from taking the collector down with it.
NODE_ID_RE = re.compile(r"^![0-9a-f]{8}$")


# Operations. Kept as plain strings; the set is small and both ends read better
# for the literal appearing in the code.
OP_SEND_TEXT = "send_text"
OP_STATUS = "status"

OPERATIONS = (OP_SEND_TEXT, OP_STATUS)


# Error codes. A client switches on these; the human-readable message beside
# them is free to change.
ERR_INVALID_REQUEST = "invalid_request"
ERR_UNSUPPORTED_VERSION = "unsupported_version"
ERR_FRAME_TOO_LARGE = "frame_too_large"
ERR_TX_DISABLED = "tx_disabled"
ERR_CHANNEL_NOT_TRACKED = "channel_not_tracked"
ERR_SEND_FAILED = "send_failed"
ERR_BUSY = "busy"
ERR_TIMEOUT = "timeout"
ERR_INTERNAL = "internal"




class ProtocolError(Exception):
  """A request that cannot be honoured, carrying the code to report for it.

  Raised while parsing and validating, so the server can turn any failure into a
  well-formed error frame rather than dropping the connection.
  """

  def __init__(self, code: str, message: str) -> None:
    super().__init__(message)
    self.code = code
    self.message = message




@dataclass(frozen=True)
class SendTextRequest:
  """Ask the collector to transmit a text message.

  `want_ack` is None by default and resolved by `resolve_want_ack()` rather than
  here, so the default can depend on the destination: acks are worth having for
  a direct message and are wasted airtime on a broadcast.
  """

  text: str
  destination: str = BROADCAST
  channel_index: int = 0
  want_ack: Optional[bool] = None
  reply_to: Optional[int] = None


  @property
  def is_direct(self) -> bool:
    return self.destination != BROADCAST


  def resolve_want_ack(self) -> bool:
    if self.want_ack is not None:
      return self.want_ack
    return self.is_direct


  def to_wire(self) -> dict[str, Any]:
    return {
      "v": PROTOCOL_VERSION,
      "op": OP_SEND_TEXT,
      "text": self.text,
      "destination": self.destination,
      "channel_index": self.channel_index,
      "want_ack": self.want_ack,
      "reply_to": self.reply_to,
    }




@dataclass(frozen=True)
class StatusRequest:
  """Ask what the collector is and whether it will transmit.

  Exists so a client can find out before offering the user a compose box, and so
  a connection can be proven end to end without putting anything on the air.
  """

  def to_wire(self) -> dict[str, Any]:
    return {"v": PROTOCOL_VERSION, "op": OP_STATUS}




def encode_frame(payload: dict[str, Any]) -> bytes:
  """Serialize one frame, newline-terminated."""
  line = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
  return line.encode("utf-8") + b"\n"




def decode_frame(raw: bytes) -> dict[str, Any]:
  """Parse one frame's bytes into an object, or raise ProtocolError."""
  if len(raw) > MAX_FRAME_BYTES:
    raise ProtocolError(
      ERR_FRAME_TOO_LARGE,
      f"Frame is {len(raw)} bytes; the limit is {MAX_FRAME_BYTES}.",
    )

  try:
    text = raw.decode("utf-8")
  except UnicodeDecodeError as e:
    raise ProtocolError(ERR_INVALID_REQUEST, f"Frame is not valid UTF-8: {e}") from e

  try:
    payload = json.loads(text)
  except json.JSONDecodeError as e:
    raise ProtocolError(ERR_INVALID_REQUEST, f"Frame is not valid JSON: {e}") from e

  if not isinstance(payload, dict):
    raise ProtocolError(
      ERR_INVALID_REQUEST,
      f"Frame must be a JSON object, got {type(payload).__name__}.",
    )

  return payload




def ok_response(result: dict[str, Any]) -> dict[str, Any]:
  return {"v": PROTOCOL_VERSION, "ok": True, "result": result}




def error_response(code: str, message: str) -> dict[str, Any]:
  return {
    "v": PROTOCOL_VERSION,
    "ok": False,
    "error": {"code": code, "message": message},
  }




def validate_destination(destination: Any) -> str:
  """Accept only '^all' or a lowercase hex node id.

  See NODE_ID_RE for why this is strict rather than forgiving.
  """
  if not isinstance(destination, str):
    raise ProtocolError(
      ERR_INVALID_REQUEST,
      f"destination must be a string, got {type(destination).__name__}.",
    )

  if destination == BROADCAST:
    return destination

  if NODE_ID_RE.match(destination):
    return destination

  raise ProtocolError(
    ERR_INVALID_REQUEST,
    f"destination must be '{BROADCAST}' or a hex node id like '!eeb826a4', "
    f"got {destination!r}.",
  )




def validate_text(text: Any) -> str:
  """Non-empty, and short enough for one packet once encoded."""
  if not isinstance(text, str):
    raise ProtocolError(
      ERR_INVALID_REQUEST,
      f"text must be a string, got {type(text).__name__}.",
    )

  if not text:
    raise ProtocolError(ERR_INVALID_REQUEST, "text must not be empty.")

  encoded = len(text.encode("utf-8"))
  if encoded > MAX_TEXT_BYTES:
    raise ProtocolError(
      ERR_INVALID_REQUEST,
      f"text is {encoded} bytes encoded; a message must fit {MAX_TEXT_BYTES}.",
    )

  return text




def validate_channel_index(channel_index: Any) -> int:
  """A device channel slot. Whether the collector will *use* it is its own call."""
  # bool is an int subclass, and True would otherwise pass as channel 1.
  if isinstance(channel_index, bool) or not isinstance(channel_index, int):
    raise ProtocolError(
      ERR_INVALID_REQUEST,
      f"channel_index must be an integer, got {type(channel_index).__name__}.",
    )

  if not 0 <= channel_index <= MAX_CHANNEL_INDEX:
    raise ProtocolError(
      ERR_INVALID_REQUEST,
      f"channel_index must be 0-{MAX_CHANNEL_INDEX}, got {channel_index}.",
    )

  return channel_index




def _validate_optional_bool(value: Any, field: str) -> Optional[bool]:
  if value is None or isinstance(value, bool):
    return value
  raise ProtocolError(
    ERR_INVALID_REQUEST,
    f"{field} must be true, false or null, got {type(value).__name__}.",
  )




def _validate_optional_packet_id(value: Any, field: str) -> Optional[int]:
  if value is None:
    return None
  if isinstance(value, bool) or not isinstance(value, int):
    raise ProtocolError(
      ERR_INVALID_REQUEST,
      f"{field} must be an integer or null, got {type(value).__name__}.",
    )
  if not 0 <= value <= 0xFFFFFFFF:
    raise ProtocolError(
      ERR_INVALID_REQUEST,
      f"{field} must be a 32-bit packet id, got {value}.",
    )
  return value




def parse_request(payload: dict[str, Any]) -> SendTextRequest | StatusRequest:
  """Turn a decoded frame into a request object, or raise ProtocolError.

  Everything a client can get wrong is rejected here, on the socket thread,
  before anything reaches the radio or the archive.
  """
  version = payload.get("v")
  if version != PROTOCOL_VERSION:
    raise ProtocolError(
      ERR_UNSUPPORTED_VERSION,
      f"This collector speaks mesh-link protocol {PROTOCOL_VERSION}, "
      f"the request said {version!r}.",
    )

  op = payload.get("op")
  if op not in OPERATIONS:
    raise ProtocolError(
      ERR_INVALID_REQUEST,
      f"Unknown op {op!r}; expected one of {', '.join(OPERATIONS)}.",
    )

  if op == OP_STATUS:
    return StatusRequest()

  return SendTextRequest(
    text=validate_text(payload.get("text")),
    destination=validate_destination(payload.get("destination", BROADCAST)),
    channel_index=validate_channel_index(payload.get("channel_index", 0)),
    want_ack=_validate_optional_bool(payload.get("want_ack"), "want_ack"),
    reply_to=_validate_optional_packet_id(payload.get("reply_to"), "reply_to"),
  )
