# Mesh Link

<!-- TODO(seam): one-line bold premise, in the shape of README.md:3. This one is a
     library rather than a program, so the sentence has to say what it is *for* —
     asking the collector to transmit — rather than what it does when you run it. -->

Mesh Link is the protocol Mesh Console and Mesh Collector speak when something wants to send a message. It is a library, not a program: there is nothing here to run, and installing it on its own accomplishes nothing.

Mesh Collector owns the serial port, because something has to. Rather than hand the radio around, it opens a Unix socket and listens on it, and this package is both halves of what travels over that socket — the wire format, the server the collector runs, and the client anything else uses. That keeps the number of processes that can talk to the radio at one.

This project is built for personal use and experimentation, prioritizing clarity, safety, and ease of maintenance over features.

> [!WARNING]
> **The socket's file permissions are the entire authorization model.** Anyone who can write to that socket can transmit on your radio, under your node's identity, to whoever can hear it. The socket is created mode `0600` and owned by the user Mesh Collector runs as, so that user is the one who can send. Do not relax those permissions to make something else work.

Transmitting is off by default and takes two deliberate steps to switch on — the `tx` extra at install time and `ENABLE_TX` in the config — so an archive-only collector never opens a socket at all.


## Installation & Getting Started

This project uses [uv](https://docs.astral.sh/uv/) for Python dependency management and virtual environments.

### Prerequisites
- Python 3.10 or newer
- `uv` installed globally
- A working [Mesh Collector](https://github.com/epohs/mesh-collector) install

### Clone the repository

```
git clone https://github.com/epohs/mesh-link.git
cd mesh-link
```

### Create the virtual environment


Each project in this suite uses its own virtual environment.

```
# Create environment
uv init
# Install dependencies
uv sync
```

### Installing it

Nothing imports Mesh Link by cloning it. It is a dependency of whichever project is going to use it, and Mesh Collector declares it as an optional extra:

```
cd ../mesh-collector
uv sync --extra tx
```

A collector installed without that extra has no `mesh_link` in its import path, so `ENABLE_TX` has nothing to switch on. That is the same reasoning that keeps the Meshtastic library out of RxOnly: what a process cannot import, it cannot be talked into doing.


## The protocol

One JSON object per line, in both directions, over a Unix stream socket. Newline framing is safe because JSON escapes control characters, so no encoded frame can contain a literal newline however strange a message is.

A request:

```json
{"v": 1, "op": "send_text", "text": "on my way", "destination": "^all", "channel_index": 0}
```

A reply, which carries the packet id the radio assigned so the caller can find what it sent:

```json
{"v": 1, "ok": true, "result": {"message_id": 1170358273, "archived": true}}
```

Or, when it could not be done:

```json
{"v": 1, "ok": false, "error": {"code": "channel_not_tracked", "message": "Channel 3 is not tracked by this collector; it archives [0]."}}
```

`destination` is either `^all` for a channel broadcast or a hex node id like `!eeb826a4` for a direct message. Those two forms are the only ones accepted, and the strictness is deliberate: every other form sends the Meshtastic library looking in its own node database, and the code path it takes when the lookup fails calls `sys.exit()`. A client's typo should not be able to stop the collector.

Messages are capped at 233 bytes once encoded as UTF-8, which is what fits in one packet. The check is on bytes rather than characters, so a message of emoji runs out sooner than a message of English.


## Using it

Serving, which is what Mesh Collector does — the loop below is the only thread that touches the radio, which is what serializes sends without a lock:

```python
from mesh_link import ControlServer

server = ControlServer(socket_path)
server.start()

while running:
    pending = server.poll(timeout=1.0)
    if pending is not None:
        ...                                    # act on pending.request
        pending.respond({"message_id": 1234})  # or pending.fail(code, message)
```

Asking, which is what a client does:

```python
from mesh_link import ControlClient, ControlError

with ControlClient(socket_path) as link:
    try:
        result = link.send_text("on my way", destination="!eeb826a4")
    except ControlError as e:
        print(e.code, e.message)
```

`ControlClient.is_available()` answers whether a collector is listening without putting anything on the air, which is what to check before offering somebody a compose box.


## Where the socket lives

The socket is runtime state — it means nothing once the process holding it is gone — so it belongs in neither the archive directory nor `~/.local/state`. With no path configured, Mesh Link uses `$XDG_RUNTIME_DIR/mesh-collector/control.sock` where that exists, and the per-user temporary directory otherwise, which is what macOS has instead. Set `CONTROL_SOCKET_PATH` in the collector's config to put it somewhere specific; a `systemd` unit should use `RuntimeDirectory=mesh-collector` and point at `/run/mesh-collector/control.sock`.

A socket left behind by a collector that was killed outright is cleaned up on the next start, but only after checking that nothing is listening on it and that it is in fact a socket. Neither a second running collector nor an unrelated file at a mistyped path gets removed.


Licensed under the GNU AGPL-3.0
Copyright (c) 2026 epohs
