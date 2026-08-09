from __future__ import annotations

import os
import tempfile

from pathlib import Path


# Where the control socket lives, and why it is neither in the archive directory
# nor in ~/.local/state.
#
# The socket is runtime state: it means nothing once the process holding it has
# gone, and a leftover file is garbage rather than history. That rules out the
# archive directory, which holds data, and ~/.local/state, which is for state
# that is *supposed* to outlive a process — that is where mesh-console keeps its
# read positions.
#
# XDG_RUNTIME_DIR is the correct answer on Linux and systemd will hand it to a
# unit for free via RuntimeDirectory=. It does not exist on macOS, where /run
# does not either, so the fallback is the per-user temporary directory Python
# already resolves — on macOS that is the private /var/folders/... path the OS
# gives each user, which is mode 0700 and not shared. Both ends resolve the path
# the same way, so a client and a server agree without being configured to.

SOCKET_FILENAME = "control.sock"
SOCKET_DIRNAME = "mesh-collector"

# AF_UNIX paths are bounded by sun_path: 104 bytes on macOS, 108 on Linux. Take
# the smaller, and leave a byte for the terminator.
MAX_SOCKET_PATH_BYTES = 103




class SocketPathTooLong(ValueError):
  """Raised when a socket path will not fit in sun_path.

  Its own error because the failure it replaces — struct.error from deep inside
  the bind — says nothing about what to do.
  """




def default_socket_dir() -> Path:
  """The runtime directory this host should hold the control socket in."""
  runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
  if runtime_dir:
    return Path(runtime_dir) / SOCKET_DIRNAME

  # tempfile honours TMPDIR, which is how macOS hands each user a private dir.
  return Path(tempfile.gettempdir()) / SOCKET_DIRNAME




def default_socket_path() -> Path:
  """The full default path, used when nothing is configured."""
  return default_socket_dir() / SOCKET_FILENAME




def resolve_socket_path(configured: str | os.PathLike[str] | None = None) -> Path:
  """Absolute socket path, from configuration or the platform default.

  `~` is expanded for the same reason the readers expand it in DB_PATH: nothing
  below does it, so a configured `~/run/control.sock` would otherwise mean a
  directory literally named `~`.
  """
  raw = str(configured).strip() if configured is not None else ""

  path = Path(raw).expanduser() if raw else default_socket_path()
  path = path.resolve() if path.is_absolute() else Path.cwd() / path

  encoded = len(str(path).encode("utf-8"))
  if encoded > MAX_SOCKET_PATH_BYTES:
    raise SocketPathTooLong(
      f"Socket path is {encoded} bytes, over the {MAX_SOCKET_PATH_BYTES}-byte "
      f"limit a Unix socket allows: {path}. Choose a shorter path."
    )

  return path
