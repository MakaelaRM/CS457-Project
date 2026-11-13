#!/usr/bin/env python3
"""Knight Duel client (CS 457, Sprint 3).

Connects to the Knight Duel server, joins a match and lets the player pick an
action each round.  Messages are newline-delimited UTF-8 JSON
(docs/protocol_blueprint.md).

Usage:
    python3 client.py -h
    python3 client.py -i server.maryanski.edu -p 8080
    python3 client.py -i localhost -p 8080
"""

import argparse
import json
import os
import queue
import socket
import sys
import threading
import time

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------
MIN_PORT, MAX_PORT = 1024, 65535
MAX_MESSAGE_BYTES = 16384
CONNECT_TIMEOUT_SECS = 10
RECV_SIZE = 4096
BAR_WIDTH = 20
START_HP = 100            # used only to scale the display bars
START_STAMINA = 50

# Menu: number -> (action name sent on the wire, label shown to the player)
MENU = {
    "1": ("SWORD_ATTACK", "Sword Attack  (heavy damage, costs 15 stamina)"),
    "2": ("BOW_ATTACK", "Bow Attack    (light damage, costs 5 stamina, fast)"),
    "3": ("BLOCK", "Block         (cuts damage, may stun attacker, costs 5)"),
    "4": ("PREPARE", "Prepare       (heal + stamina, speed boost next round)"),
}


def now():
    """Unix time in whole seconds (protocol timestamp)."""
    return int(time.time())


# --------------------------------------------------------------------------
# Command-line handling and DNS resolution
# --------------------------------------------------------------------------
def port_number(text):
    """argparse type: an integer port in 1024-65535."""
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"'{text}' is not a number")
    if not MIN_PORT <= value <= MAX_PORT:
        raise argparse.ArgumentTypeError(
            f"port must be between {MIN_PORT} and {MAX_PORT}")
    return value


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Knight Duel client: join a 1v1 Knight battle.",
        epilog="example: python3 client.py -i server.maryanski.edu -p 8080")
    parser.add_argument("-i", dest="hostname", required=True,
                        metavar="HOSTNAME",
                        help="server DNS hostname (e.g. server.maryanski.edu)")
    parser.add_argument("-p", dest="port", type=port_number, required=True,
                        metavar="PORT",
                        help=f"server port ({MIN_PORT}-{MAX_PORT})")
    return parser.parse_args(argv)


def resolve_hostname(hostname):
    """Resolve ``hostname`` via DNS; exit(1) with a friendly message if not."""
    try:
        return socket.gethostbyname(hostname)
    except (socket.gaierror, UnicodeError):
        print(f"[ERROR] Unable to resolve DNS hostname '{hostname}'.")
        print("Please check your Cisco router DNS configuration "
              "('ip dns server' / 'ip host') or verify domain spelling.")
        sys.exit(1)


def open_connection(server_ip, port):
    """Create the TCP socket and connect; exit(1) with a message on failure."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(CONNECT_TIMEOUT_SECS)
    try:
        sock.connect((server_ip, port))
    except (ConnectionRefusedError, socket.timeout, OSError) as exc:
        print(f"[ERROR] Could not connect to {server_ip}:{port} ({exc}).")
        print("Is the server running, and is the port correct?")
        sock.close()
        sys.exit(1)
    sock.settimeout(None)
    return sock


# --------------------------------------------------------------------------
# Wire helpers (newline-delimited JSON framing)
# --------------------------------------------------------------------------
def send_message(sock, msg_type, player_id, payload):
    """Serialize and send one framed message."""
    msg = {"msg_type": msg_type, "player_id": player_id,
           "payload": payload, "timestamp": now()}
    data = (json.dumps(msg, separators=(",", ":")) + "\n").encode("utf-8")
    sock.sendall(data)


def reader_thread(sock, events):
    """Read bytes, split on newlines and queue parsed messages.

    TCP may split one message across recv() calls or merge several into one,
    so incoming bytes accumulate in ``buffer`` until a full line is present.
    """
    buffer = b""
    try:
        while True:
            data = sock.recv(RECV_SIZE)
            if data == b"":
                events.put(("closed", "server closed the connection"))
                return
            buffer += data
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                try:
                    events.put(("net", json.loads(line.decode("utf-8"))))
                except (UnicodeDecodeError, ValueError):
                    events.put(("bad", "received a malformed message"))
            if len(buffer) >= MAX_MESSAGE_BYTES:
                events.put(("closed", "server sent an oversized message"))
                return
    except OSError as exc:
        events.put(("closed", f"connection error: {exc}"))


def stdin_thread(events):
    """Forward typed lines to the main loop (keeps socket events live)."""
    while True:
        line = sys.stdin.readline()
        if line == "":
            events.put(("eof", None))
            return
        events.put(("line", line.strip()))


# --------------------------------------------------------------------------
# Display helpers
# --------------------------------------------------------------------------
def bar(value, maximum):
    filled = max(0, min(BAR_WIDTH, round(BAR_WIDTH * value / maximum)))
    return "[" + "#" * filled + "-" * (BAR_WIDTH - filled) + "]"


def render_board(players, my_id, round_no):
    """Return a text scoreboard: HP/stamina bars for both Knights."""
    lines = ["", "=" * 62, f"  ROUND {round_no}".ljust(61), "=" * 62]
    for pid in ("Player_1", "Player_2"):
        k = players[pid]
        tag = "YOU " if pid == my_id else "FOE "
        status = "  ** STUNNED **" if k["stunned"] else ""
        if k["speed_multiplier"] > 1:
            status += f"  (speed x{k['speed_multiplier']})"
        lines.append(f" {tag}{pid} \"{k['alias']}\"{status}")
        lines.append(f"   HP      {bar(k['hp'], START_HP)} {k['hp']:>3}")
        lines.append(f"   Stamina {bar(k['stamina'], START_STAMINA)} "
                     f"{k['stamina']:>3}")
    lines.append("=" * 62)
    return "\n".join(lines)


def print_menu():
    print("Choose your action:")
    for key, (_, label) in MENU.items():
        print(f"  {key}) {label}")
    print("  q) Quit (forfeits the match)")


# --------------------------------------------------------------------------
# Game client
# --------------------------------------------------------------------------
class GameClient:
    """Event-driven console client."""

    def __init__(self, sock, events):
        self.sock = sock
        self.events = events
        self.my_id = None
        self.round = 0
        self.players = None
        self.mode = "CONNECTING"   # CONNECTING, WAITING, SELECTING, SUBMITTED
        self.exit_code = None

    # -- helpers ----------------------------------------------------------
    def opponent(self):
        return "Player_2" if self.my_id == "Player_1" else "Player_1"

    def ask_alias(self):
        """Prompt for an alias, then send CONNECT (stdin read directly)."""
        while True:
            print("Enter your Knight's name: ", end="", flush=True)
            kind, value = self.events.get()
            if kind == "line" and value:
                break
            if kind in ("eof", "closed"):
                self.finish(1, "Input closed before joining.")
                return
        send_message(self.sock, "CONNECT", None, {"alias": value})
        print("Joining the game...")

    def finish(self, code, message=None):
        if message:
            print(message)
        self.exit_code = code

    # -- main loop ----------------------------------------------------------
    def run(self):
        self.ask_alias()
        while self.exit_code is None:
            kind, value = self.events.get()
            if kind == "net":
                self.on_message(value)
            elif kind == "line":
                self.on_input(value)
            elif kind in ("eof", "closed"):
                self.on_closed(kind, value)
            elif kind == "bad":
                print(f"[WARN] {value}")
        return self.exit_code

    def on_closed(self, kind, value):
        if kind == "eof":                       # stdin ended: leave cleanly
            self.quit_game()
        else:
            self.finish(1, f"\n[ERROR] Lost connection: {value}.")

    # -- server messages --------------------------------------------------
    def on_message(self, msg):
        kind, payload = msg.get("msg_type"), msg.get("payload", {})
        handler = {
            "LOBBY_WAIT": self.on_lobby_wait,
            "GAME_START": self.on_game_start,
            "STATE_UPDATE": self.on_state_update,
            "ERROR": self.on_error,
            "GAME_OVER": self.on_game_over,
        }.get(kind)
        if handler:
            handler(payload)
        else:
            print(f"[WARN] ignoring unexpected message {kind}")

    def on_lobby_wait(self, payload):
        self.my_id = payload["assigned_player_id"]
        self.mode = "WAITING"
        print(f"You are {self.my_id}. Waiting for Player 2 to join...")

    def on_game_start(self, payload):
        self.my_id = payload["assigned_player_id"]
        self.players = payload["players"]
        self.round = payload["round"]
        foe = self.players[self.opponent()]["alias"]
        print(f"\n*** Duel starting! You are {self.my_id}, facing "
              f"\"{foe}\". ***")
        self.start_selection()

    def on_state_update(self, payload):
        self.players = payload["players"]
        self.round = payload["round"]
        print("\n--- Round %d results ---" % payload["completed_round"])
        for line in payload["combat_log"]:
            print("  *", line)
        if payload["phase"] == "SELECTING":
            self.start_selection()
        else:
            print(render_board(self.players, self.my_id, self.round))
            self.mode = "WAITING"

    def start_selection(self):
        print(render_board(self.players, self.my_id, self.round))
        if self.players[self.my_id]["stunned"]:
            self.mode = "WAITING"
            print("You are stunned - your action is skipped this round.")
            print(f"Waiting for {self.opponent()}...")
            return
        self.mode = "SELECTING"
        print(f"\n>>> Your Turn (round {self.round}) <<<")
        print_menu()
        print("> ", end="", flush=True)

    def on_error(self, payload):
        code, text = payload.get("code"), payload.get("message")
        print(f"\n[SERVER ERROR] {code}: {text}")
        if code == "ROOM_FULL":
            self.finish(1, "The game already has two players.")
        elif self.mode == "CONNECTING":
            self.ask_alias()                    # alias rejected: try again
        elif self.mode == "SUBMITTED":
            self.mode = "SELECTING"
            print("Please choose again.")
            print("> ", end="", flush=True)

    def on_game_over(self, payload):
        players = payload["players"]
        print("\n" + "#" * 62)
        print("  GAME OVER".ljust(61))
        print("#" * 62)
        winner = payload["winner"]
        if winner is None:
            print("  Result: DRAW")
        elif winner == self.my_id:
            print("  Result: VICTORY! You win!")
        else:
            print("  Result: DEFEAT. Better luck next time.")
        print(f"  Reason: {payload['reason']}  (round {payload['round']})")
        print("  Final score:")
        for pid in ("Player_1", "Player_2"):
            k = players[pid]
            print(f"    {pid} \"{k['alias']}\": HP {k['hp']}, "
                  f"Stamina {k['stamina']}")
        self.finish(0)

    # -- player input -----------------------------------------------------
    def on_input(self, text):
        if text.lower() in ("q", "quit"):
            self.quit_game()
            return
        if self.mode != "SELECTING":
            print("(Not your turn - input ignored.)")
            return
        choice = text.strip().upper()
        action = None
        if choice in MENU:
            action = MENU[choice][0]
        else:
            for name, _ in MENU.values():
                if choice in (name, name.split("_")[0]):
                    action = name
        if action is None:
            print("Invalid choice. Enter 1-4 or q.")
            print("> ", end="", flush=True)
            return
        send_message(self.sock, "MOVE", self.my_id,
                     {"round": self.round, "action": action})
        self.mode = "SUBMITTED"
        print(f"Move sent: {action}. Waiting for {self.opponent()}...")

    def quit_game(self):
        try:
            send_message(self.sock, "DISCONNECT", self.my_id,
                         {"reason": "QUIT"})
        except OSError:
            pass
        self.finish(0, "You left the game.")


def main():
    args = parse_args()
    server_ip = resolve_hostname(args.hostname)
    sock = open_connection(server_ip, args.port)
    print(f"Connected to {args.hostname} ({server_ip}):{args.port}")

    events = queue.Queue()
    threading.Thread(target=reader_thread, args=(sock, events),
                     daemon=True).start()
    threading.Thread(target=stdin_thread, args=(events,),
                     daemon=True).start()

    client = GameClient(sock, events)
    try:
        code = client.run()
    except KeyboardInterrupt:
        client.quit_game()
        code = 0
    except (BrokenPipeError, ConnectionError) as exc:
        print(f"\n[ERROR] Lost connection: {exc}")
        code = 1
    sys.stdout.flush()
    try:
        sock.close()
    except OSError:
        pass
    os._exit(code)      # daemon threads may still be blocked on I/O


if __name__ == "__main__":
    main()
