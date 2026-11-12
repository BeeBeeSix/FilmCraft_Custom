#!/usr/bin/env python3
"""fc_bridge_keepalive.py - keep an MCP stdio bridge alive for Claude Desktop.

Claude Desktop starts an MCP server once and never restarts it if it exits.
The FilmCraft bridge (filmcraft-cli mcp --bridge ...) exits when FilmCraft
closes or restarts, so the connector stays dead until Desktop is restarted.

This script sits in between:

    Claude Desktop  <-stdio->  fc_bridge_keepalive.py  <-stdio->  bridge

  * It passes messages through unchanged.
  * If the bridge exits, it starts it again (backing off up to 10 s while
    FilmCraft is not running) and replays the MCP handshake, so Desktop's
    session carries on without a restart.
  * Requests that were in flight when the bridge died, or that arrive while
    it is down, get an immediate JSON-RPC error ("reconnecting") instead of
    hanging.

Usage (Desktop config "command"/"args"):
    python fc_bridge_keepalive.py <bridge-exe> [bridge args...]

Everything it logs goes to stderr, which Claude Desktop writes to
mcp-server-filmcraft.log, prefixed with [keepalive].

Limits: it detects a bridge that EXITS. A bridge that stays running but has
silently lost its connection is not detected.
"""
import json
import os
import subprocess
import sys
import threading
import time

HANDSHAKE_ID = "__fc_keepalive_init__"
DOWN_MSG = ("FilmCraft bridge is not connected - reconnecting. Make sure "
            "FilmCraft is running, then try again in a few seconds.")


def log(*a):
    print("[keepalive]", *a, file=sys.stderr, flush=True)


class Proxy:
    def __init__(self, cmd):
        self.cmd = cmd
        self.child = None
        self.lock = threading.RLock()
        self.out_lock = threading.Lock()
        self.pending = {}          # client request id -> method
        self.init_req = None       # raw bytes of the client's initialize
        self.init_done = None      # raw bytes of notifications/initialized
        self.stopping = False

    # ---- output to Claude Desktop ---------------------------------------
    def to_client(self, raw):
        with self.out_lock:
            sys.stdout.buffer.write(raw if raw.endswith(b"\n")
                                    else raw + b"\n")
            sys.stdout.buffer.flush()

    def error_to_client(self, rid, text):
        msg = {"jsonrpc": "2.0", "id": rid,
               "error": {"code": -32000, "message": text}}
        self.to_client(json.dumps(msg).encode("utf-8"))

    # ---- child process ---------------------------------------------------
    def spawn(self):
        flags = 0x08000000 if os.name == "nt" else 0   # no console window
        p = subprocess.Popen(self.cmd, stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=None,
                             bufsize=0, creationflags=flags)
        with self.lock:
            self.child = p
        threading.Thread(target=self.read_child, args=(p,),
                         daemon=True).start()
        return p

    def send_child(self, raw):
        with self.lock:
            p = self.child
            if p is None or p.poll() is not None:
                return False
            try:
                p.stdin.write(raw if raw.endswith(b"\n") else raw + b"\n")
                p.stdin.flush()
                return True
            except (OSError, ValueError):
                return False

    def read_child(self, p):
        for line in iter(p.stdout.readline, b""):
            s = line.strip()
            if not s:
                continue
            try:
                msg = json.loads(s)
            except ValueError:
                self.to_client(s)
                continue
            rid = msg.get("id")
            is_response = "method" not in msg and rid is not None
            if is_response and rid == HANDSHAKE_ID:
                # answer to our own replayed initialize: swallow it
                if self.init_done:
                    self.send_child(self.init_done)
                log("session re-established")
                continue
            if is_response:
                self.pending.pop(rid, None)
            self.to_client(s)

    def handshake(self):
        """Bring a freshly started bridge to the state Desktop expects."""
        if not self.init_req:
            return
        if self.init_done:                     # Desktop already initialised
            msg = json.loads(self.init_req)
            msg["id"] = HANDSHAKE_ID
            self.send_child(json.dumps(msg).encode("utf-8"))
        else:                                  # Desktop is still waiting
            self.send_child(self.init_req)

    def supervise(self):
        delay = 1
        while not self.stopping:
            started = time.time()
            try:
                p = self.spawn()
            except OSError as e:
                log("cannot start bridge (%s); retrying in %ds" % (e, delay))
                time.sleep(delay)
                delay = min(delay * 2, 10)
                continue
            self.handshake()
            code = p.wait()
            with self.lock:
                self.child = None
            if self.stopping:
                break
            for rid, method in list(self.pending.items()):
                if method == "initialize" and not self.init_done:
                    continue            # will be re-sent to the next bridge
                self.pending.pop(rid, None)
                self.error_to_client(rid, DOWN_MSG)
            lived = time.time() - started
            delay = 1 if lived > 10 else min(delay * 2, 10)
            log("bridge exited (code %s) after %.0fs; restarting in %ds"
                % (code, lived, delay))
            time.sleep(delay)

    # ---- input from Claude Desktop ---------------------------------------
    def run(self):
        threading.Thread(target=self.supervise, daemon=True).start()
        for line in sys.stdin.buffer:
            s = line.strip()
            if not s:
                continue
            method = rid = None
            try:
                msg = json.loads(s)
                method, rid = msg.get("method"), msg.get("id")
            except ValueError:
                pass
            if method == "initialize":
                self.init_req, self.init_done = s, None
            elif method == "notifications/initialized":
                self.init_done = s
            is_request = method is not None and rid is not None
            if is_request:
                self.pending[rid] = method
            if not self.send_child(s):
                if is_request and method != "initialize":
                    self.pending.pop(rid, None)
                    self.error_to_client(rid, DOWN_MSG)
                # initialize / notifications are replayed after the restart
        # Desktop closed our stdin: shut down
        self.stopping = True
        with self.lock:
            if self.child and self.child.poll() is None:
                self.child.terminate()


def main():
    if len(sys.argv) < 2:
        sys.exit("usage: fc_bridge_keepalive.py <bridge-exe> [args...]")
    log("supervising:", " ".join(sys.argv[1:]))
    Proxy(sys.argv[1:]).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
