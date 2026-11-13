#!/usr/bin/env python3
"""Knight Duel server (CS 457, Sprint 3).

A single-threaded, non-blocking TCP server built on ``selectors`` (the
"Option B" concurrency model from docs/concurrency_design.md).  It runs one
two-player match at a time and drives the FSM from docs/fsm_specification.md:

    INIT -> WAITING_FOR_PLAYERS -> GAME_START -> PLAYER_TURN
         -> EVALUATE_MOVE -> (PLAYER_TURN | GAME_OVER) -> CLEANUP -> ...

Wire format (docs/protocol_blueprint.md): one UTF-8 JSON object per line,
terminated by ``\\n``.  Each message has ``msg_type``, ``player_id``,
``payload`` and ``timestamp``.

Usage:
    python3 server.py -h
    python3 server.py -p 8080
"""

import argparse
import json
import random
import selectors
import socket
import sys
import time
from enum import Enum

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------
BIND_HOST = "0.0.0.0"          # listen on every interface
MIN_PORT, MAX_PORT = 1024, 65535
MAX_MESSAGE_BYTES = 16384      # includes the trailing newline
RECV_SIZE = 4096
SELECT_DEADLINE_SECS = 120     # time to submit a move during PLAYER_TURN
WRITE_TIMEOUT_SECS = 10        # a socket that cannot send this long is dead
MAX_ROUNDS = 30

# Game balance (Sprint 1 left these "to be finalized"; chosen here).
START_HP = 100
START_STAMINA = 50
PREPARE_HEAL = 15
PREPARE_STAMINA = 15
PREPARE_MULTIPLIER = 1.5
BLOCK_DAMAGE_REDUCTION = 0.75  # blocked attacks deal 25% of normal damage
BLOCK_STUN_CHANCE = 0.35

# action -> stamina cost, base damage, action speed
ACTIONS = {
    "SWORD_ATTACK": {"cost": 15, "damage": 25, "speed": 1},
    "BOW_ATTACK": {"cost": 5, "damage": 12, "speed": 2},
    "BLOCK": {"cost": 5, "damage": 0, "speed": 3},
    "PREPARE": {"cost": 0, "damage": 0, "speed": 2},
}

PLAYER_IDS = ("Player_1", "Player_2")
CLIENT_TYPES = {"CONNECT", "MOVE", "DISCONNECT"}
SERVER_TYPES = {"LOBBY_WAIT", "GAME_START", "STATE_UPDATE", "ERROR",
                "GAME_OVER"}
ALL_TYPES = CLIENT_TYPES | SERVER_TYPES
MAX_ALIAS_LEN = 32


class State(Enum):
    """Server-side FSM states (docs/fsm_specification.md, section 2)."""
    INIT = "INIT"
    WAITING_FOR_PLAYERS = "WAITING_FOR_PLAYERS"
    GAME_START = "GAME_START"
    PLAYER_TURN = "PLAYER_TURN"
    EVALUATE_MOVE = "EVALUATE_MOVE"
    GAME_OVER = "GAME_OVER"
    CLEANUP = "CLEANUP"


def log(text):
    """Print a timestamped server log line."""
    print(f"[{time.strftime('%H:%M:%S')}] {text}", flush=True)


def now():
    """Current Unix time in whole seconds (protocol timestamp)."""
    return int(time.time())


# --------------------------------------------------------------------------
# Data holders
# --------------------------------------------------------------------------
class Connection:
    """Per-socket record: buffers, identity and teardown flags."""

    def __init__(self, sock, addr):
        self.sock = sock
        self.addr = addr
        self.inbuf = b""            # partial-line input buffer
        self.outbuf = b""           # bytes not yet sent
        self.player_id = None       # Player_1 / Player_2 once joined
        self.alias = None
        self.disconnected = False   # departure already handled (once only)
        self.closing = False        # close as soon as outbuf drains
        self.stalled_since = None   # when sending last made no progress

    def label(self):
        return self.player_id or f"{self.addr[0]}:{self.addr[1]}"


class Knight:
    """Stats for one player (the protocol's 'Knight Stats' object)."""

    def __init__(self, alias):
        self.alias = alias
        self.hp = START_HP
        self.stamina = START_STAMINA
        self.execution_speed = None
        self.multiplier = 1
        self.stunned = False

    def to_dict(self):
        return {
            "alias": self.alias,
            "hp": self.hp,
            "stamina": self.stamina,
            "execution_speed": self.execution_speed,
            "speed_multiplier": self.multiplier,
            "stunned": self.stunned,
        }


# --------------------------------------------------------------------------
# Message validation helpers
# --------------------------------------------------------------------------
def is_int(value):
    """True for real ints (bool is excluded)."""
    return isinstance(value, int) and not isinstance(value, bool)


def check_envelope(msg):
    """Return an error string if ``msg`` breaks the shared-field rules."""
    if not isinstance(msg, dict):
        return "Message must be a JSON object."
    if set(msg) != {"msg_type", "player_id", "payload", "timestamp"}:
        return "Message must have exactly msg_type, player_id, payload, " \
               "timestamp."
    kind = msg["msg_type"]
    if not isinstance(kind, str) or kind not in ALL_TYPES:
        return "Unknown msg_type."
    if msg["player_id"] is not None and not isinstance(msg["player_id"], str):
        return "player_id must be a string or null."
    if not isinstance(msg["payload"], dict):
        return "payload must be an object."
    if not is_int(msg["timestamp"]) or msg["timestamp"] < 0:
        return "timestamp must be a nonnegative integer."
    return None


def check_payload(msg):
    """Return an error string if a client payload has wrong fields/types."""
    kind, payload = msg["msg_type"], msg["payload"]
    if kind == "CONNECT":
        if set(payload) != {"alias"} or not isinstance(payload["alias"], str):
            return "CONNECT payload must be {alias: string}."
    elif kind == "MOVE":
        if (set(payload) != {"round", "action"}
                or not is_int(payload["round"])
                or not isinstance(payload["action"], str)):
            return "MOVE payload must be {round: int, action: string}."
    elif kind == "DISCONNECT":
        if set(payload) != {"reason"} or payload["reason"] != "QUIT":
            return "DISCONNECT payload must be {reason: \"QUIT\"}."
    return None


# --------------------------------------------------------------------------
# Server
# --------------------------------------------------------------------------
class KnightDuelServer:
    """Event-loop server; the only code that mutates game state."""

    def __init__(self, port):
        self.port = port
        self.state = State.INIT
        self.sel = selectors.DefaultSelector()
        self.listener = None
        self.conns = {}                       # socket -> Connection
        self.reset_match()

    # ---- FSM bookkeeping -------------------------------------------------
    def set_state(self, new_state, why):
        """Move to ``new_state`` and log the transition."""
        log(f"[FSM] {self.state.value} -> {new_state.value}  ({why})")
        self.state = new_state

    def reset_match(self):
        """Clear everything belonging to one match."""
        self.slots = {pid: None for pid in PLAYER_IDS}   # pid -> Connection
        self.knights = {}                                # pid -> Knight
        self.round = 0
        self.actions = {}                                # pid -> action
        self.deadline = None

    # ---- startup / main loop --------------------------------------------
    def start(self):
        """INIT: create the non-blocking listening socket."""
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener.bind((BIND_HOST, self.port))
        self.listener.listen(5)
        self.listener.setblocking(False)
        self.sel.register(self.listener, selectors.EVENT_READ, data=None)
        log(f"Knight Duel server listening on {BIND_HOST}:{self.port}")
        self.set_state(State.WAITING_FOR_PLAYERS, "socket ready")

    def run(self):
        """Main event loop: one thread, one select() call."""
        while True:
            for key, mask in self.sel.select(self.next_timeout()):
                if key.data is None:
                    self.on_accept()
                    continue
                conn = key.data
                if conn.disconnected and not conn.closing:
                    continue
                if mask & selectors.EVENT_READ and not conn.closing:
                    self.on_readable(conn)
                if mask & selectors.EVENT_WRITE and conn.sock in self.conns:
                    self.flush(conn)
            self.check_timers()

    def next_timeout(self):
        """Seconds until the nearest deadline (None = wait forever)."""
        times = []
        if self.state == State.PLAYER_TURN and self.deadline is not None:
            times.append(self.deadline)
        for conn in self.conns.values():
            if conn.stalled_since is not None:
                times.append(conn.stalled_since + WRITE_TIMEOUT_SECS)
        if not times:
            return None
        return max(0.0, min(times) - time.monotonic())

    def check_timers(self):
        """Handle the 120 s selection deadline and stalled writes."""
        t = time.monotonic()
        for conn in list(self.conns.values()):
            if (conn.stalled_since is not None
                    and t - conn.stalled_since >= WRITE_TIMEOUT_SECS):
                self.drop(conn, "write timeout")
        if (self.state == State.PLAYER_TURN and self.deadline is not None
                and t >= self.deadline):
            self.on_selection_timeout()

    def on_selection_timeout(self):
        """Players who owed a move and sent nothing forfeit (section 6)."""
        missing = [p for p in self.required_players()
                   if p not in self.actions]
        log(f"Selection timed out; missing moves from {missing}")
        if len(missing) == 1:
            other = "Player_2" if missing[0] == "Player_1" else "Player_1"
            self.end_game(other, "FORFEIT")
        else:
            self.cleanup("both players timed out")

    # ---- accept / read / write -------------------------------------------
    def on_accept(self):
        """Accept a new TCP connection (non-blocking)."""
        try:
            sock, addr = self.listener.accept()
        except (BlockingIOError, InterruptedError):
            return
        except OSError as exc:
            log(f"accept() failed: {exc}")
            return
        sock.setblocking(False)
        conn = Connection(sock, addr)
        self.conns[sock] = conn
        self.sel.register(sock, selectors.EVENT_READ, data=conn)
        log(f"Connection from {addr[0]}:{addr[1]}")
        if self.match_is_full():
            self.reject_full(conn)

    def on_readable(self, conn):
        """Read bytes, extract complete newline-terminated messages."""
        try:
            data = conn.sock.recv(RECV_SIZE)
        except (BlockingIOError, InterruptedError):
            return
        except (ConnectionResetError, BrokenPipeError,
                ConnectionAbortedError, socket.timeout, OSError) as exc:
            self.drop(conn, f"socket error: {exc}")
            return
        if data == b"":                       # EOF: peer closed
            self.drop(conn, "EOF")
            return
        conn.inbuf += data
        while b"\n" in conn.inbuf and not conn.disconnected:
            line, conn.inbuf = conn.inbuf.split(b"\n", 1)
            if len(line) + 1 > MAX_MESSAGE_BYTES:
                self.too_large(conn)
                return
            self.handle_line(conn, line)
        if len(conn.inbuf) >= MAX_MESSAGE_BYTES and not conn.disconnected:
            self.too_large(conn)

    def too_large(self, conn):
        """Oversized message: send MESSAGE_TOO_LARGE and close."""
        self.send_error(conn, "MESSAGE_TOO_LARGE",
                        f"Messages are limited to {MAX_MESSAGE_BYTES} bytes.")
        self.drop(conn, "message too large", graceful=True)

    def send(self, conn, msg_type, payload):
        """Queue one framed JSON message and try to send it right away."""
        if conn.disconnected and not conn.closing:
            return
        msg = {"msg_type": msg_type, "player_id": "SERVER",
               "payload": payload, "timestamp": now()}
        conn.outbuf += (json.dumps(msg, separators=(",", ":")) + "\n").encode()
        self.flush(conn)

    def send_error(self, conn, code, message):
        log(f"-> {conn.label()}: ERROR {code}: {message}")
        self.send(conn, "ERROR", {"code": code, "message": message})

    def flush(self, conn):
        """Send as much of the output buffer as the socket accepts."""
        if conn.sock not in self.conns:
            return
        while conn.outbuf:
            try:
                sent = conn.sock.send(conn.outbuf)
            except (BlockingIOError, InterruptedError):
                break
            except OSError as exc:            # reset, broken pipe, ...
                self.drop(conn, f"send error: {exc}")
                return
            conn.outbuf = conn.outbuf[sent:]
            conn.stalled_since = None         # made progress
        if conn.outbuf:
            if conn.stalled_since is None:
                conn.stalled_since = time.monotonic()
            self.set_interest(conn, writable=True)
        else:
            conn.stalled_since = None
            if conn.closing:
                self.close_socket(conn)
            else:
                self.set_interest(conn, writable=False)

    def set_interest(self, conn, writable):
        """Register for read (unless closing) and optionally write events."""
        events = 0 if conn.closing else selectors.EVENT_READ
        if writable:
            events |= selectors.EVENT_WRITE
        try:
            self.sel.modify(conn.sock, events, data=conn)
        except (KeyError, ValueError, OSError):
            pass

    def close_socket(self, conn):
        """Unregister and close a socket (safe to call repeatedly)."""
        if conn.sock in self.conns:
            del self.conns[conn.sock]
            try:
                self.sel.unregister(conn.sock)
            except (KeyError, ValueError):
                pass
            try:
                conn.sock.close()
            except OSError:
                pass
        conn.inbuf = b""
        conn.outbuf = b""

    def mark_closing(self, conn):
        """Close the socket once pending output has been delivered."""
        conn.disconnected = True
        conn.closing = True
        if conn.sock not in self.conns:
            return
        if conn.outbuf:
            self.set_interest(conn, writable=True)
        else:
            self.close_socket(conn)

    # ---- message dispatch -------------------------------------------------
    def handle_line(self, conn, line):
        """Parse and validate one framed message, then dispatch it."""
        if not line.strip():
            self.send_error(conn, "INVALID_MESSAGE", "Empty message.")
            return
        try:
            msg = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            self.send_error(conn, "INVALID_MESSAGE",
                            "Message is not valid UTF-8 JSON.")
            return
        problem = check_envelope(msg)
        if problem:
            self.send_error(conn, "INVALID_MESSAGE", problem)
            return
        kind = msg["msg_type"]
        if kind in SERVER_TYPES:
            self.send_error(conn, "INVALID_PHASE",
                            f"{kind} is a server-only message.")
            return
        problem = check_payload(msg)
        if problem:
            self.send_error(conn, "INVALID_MESSAGE", problem)
            return
        log(f"<- {conn.label()}: {kind} {json.dumps(msg['payload'])}")
        if kind == "CONNECT":
            self.handle_connect(conn, msg)
        elif kind == "MOVE":
            self.handle_move(conn, msg)
        else:
            self.drop(conn, "DISCONNECT message")

    def match_is_full(self):
        return all(self.slots[p] is not None for p in PLAYER_IDS)

    def reject_full(self, conn):
        """Third client: ROOM_FULL, then close only that socket."""
        self.send_error(conn, "ROOM_FULL", "Two players already joined.")
        self.mark_closing(conn)

    # ---- WAITING_FOR_PLAYERS: CONNECT -------------------------------------
    def handle_connect(self, conn, msg):
        if conn.player_id is not None:
            self.send_error(conn, "INVALID_PHASE", "Already joined.")
            return
        if msg["player_id"] is not None:
            self.send_error(conn, "INVALID_PLAYER",
                            "CONNECT must use a null player_id.")
            return
        if self.match_is_full() or self.state != State.WAITING_FOR_PLAYERS:
            self.reject_full(conn)
            return
        alias = msg["payload"]["alias"].strip()
        if not alias or len(alias) > MAX_ALIAS_LEN:
            self.send_error(conn, "INVALID_PLAYER",
                            f"Alias must be 1-{MAX_ALIAS_LEN} characters.")
            return
        if any(c.alias == alias for c in self.slots.values() if c):
            self.send_error(conn, "INVALID_PLAYER",
                            "That alias is already taken.")
            return
        pid = next(p for p in PLAYER_IDS if self.slots[p] is None)
        self.slots[pid] = conn
        conn.player_id, conn.alias = pid, alias
        log(f"{alias} joined as {pid}")
        if self.match_is_full():
            self.start_match()
        else:
            self.send(conn, "LOBBY_WAIT",
                      {"assigned_player_id": pid,
                       "message": "Waiting for opponent."})

    def start_match(self):
        """GAME_START: set starting stats, tell both clients, begin round 1."""
        self.set_state(State.GAME_START, "second player joined")
        self.knights = {p: Knight(self.slots[p].alias) for p in PLAYER_IDS}
        self.round = 1
        for pid in PLAYER_IDS:
            self.send(self.slots[pid], "GAME_START", {
                "assigned_player_id": pid,
                "round": 1,
                "phase": "SELECTING",
                "players": self.players_snapshot(),
            })
        self.set_state(State.PLAYER_TURN, "stats initialized")
        self.begin_selection()

    # ---- PLAYER_TURN: MOVE ------------------------------------------------
    def required_players(self):
        """Players who must submit a move this round (not stunned)."""
        return [p for p in PLAYER_IDS if not self.knights[p].stunned]

    def begin_selection(self):
        """Start a round's selection phase; auto-evaluate if nobody acts."""
        self.actions = {}
        self.deadline = time.monotonic() + SELECT_DEADLINE_SECS
        log(f"--- Round {self.round}: waiting for moves from "
            f"{self.required_players() or 'nobody (both stunned)'} ---")
        self.maybe_evaluate()

    def handle_move(self, conn, msg):
        """Validate a MOVE with the seven rules of FSM section 4."""
        if self.state != State.PLAYER_TURN:
            self.send_error(conn, "INVALID_PHASE",
                            "Moves are only accepted while a round is in "
                            "selection.")
            return
        pid = conn.player_id
        if pid is None or msg["player_id"] != pid:
            self.send_error(conn, "INVALID_PLAYER",
                            "player_id does not match this connection.")
            return
        payload = msg["payload"]
        action = payload["action"]
        knight = self.knights[pid]
        if payload["round"] != self.round:
            self.send_error(conn, "INVALID_MOVE",
                            f"Wrong round; the current round is "
                            f"{self.round}.")
        elif action not in ACTIONS:
            self.send_error(conn, "INVALID_MOVE", "Unknown action.")
        elif knight.stunned:
            self.send_error(conn, "INVALID_MOVE",
                            "You are stunned and skip this round.")
        elif pid in self.actions:
            self.send_error(conn, "INVALID_MOVE",
                            "You already submitted a move this round.")
        elif knight.stamina < ACTIONS[action]["cost"]:
            self.send_error(conn, "INVALID_MOVE",
                            "Not enough stamina for this action.")
        else:
            self.actions[pid] = action        # stored, hidden from opponent
            log(f"{pid} locked in a move (hidden)")
            self.maybe_evaluate()

    def maybe_evaluate(self):
        """Evaluate rounds while every required action is in."""
        while (self.state == State.PLAYER_TURN
               and all(p in self.actions for p in self.required_players())):
            self.set_state(State.EVALUATE_MOVE, "all actions in")
            self.evaluate_round()

    # ---- EVALUATE_MOVE ----------------------------------------------------
    def evaluate_round(self):
        """Resolve one round (FSM section 7) and broadcast the result."""
        rnd = self.round
        combat_log = []
        winner = None
        blocking, prepared, next_stun = set(), set(), set()

        # 1. execution speeds (stunned = 0)
        for pid in PLAYER_IDS:
            k = self.knights[pid]
            if k.stunned:
                k.execution_speed = 0
                combat_log.append(f"{pid} is stunned and skips its action.")
            else:
                speed = ACTIONS[self.actions[pid]]["speed"]
                k.execution_speed = speed * k.multiplier

        # Order by speed (high first); ties broken 50/50 by shuffling first.
        order = [p for p in PLAYER_IDS if not self.knights[p].stunned]
        random.shuffle(order)
        order.sort(key=lambda p: -self.knights[p].execution_speed)

        # 2. Blocks activate before any attack.
        for pid in order:
            if self.actions[pid] == "BLOCK":
                self.knights[pid].stamina -= ACTIONS["BLOCK"]["cost"]
                blocking.add(pid)
                combat_log.append(f"{pid} blocked.")

        # 3. Remaining actions in speed order; a knockout stops everything.
        for pid in order:
            action = self.actions[pid]
            if action == "BLOCK":
                continue
            me = self.knights[pid]
            foe_id = "Player_2" if pid == "Player_1" else "Player_1"
            foe = self.knights[foe_id]
            me.stamina -= ACTIONS[action]["cost"]
            if action == "PREPARE":
                me.hp = min(START_HP, me.hp + PREPARE_HEAL)
                me.stamina = min(START_STAMINA, me.stamina + PREPARE_STAMINA)
                prepared.add(pid)
                combat_log.append(f"{pid} prepared (+{PREPARE_HEAL} HP, "
                                  f"+{PREPARE_STAMINA} stamina).")
                continue
            damage = ACTIONS[action]["damage"]
            name = "attacked with the sword" if action == "SWORD_ATTACK" \
                else "attacked with the bow"
            if foe_id in blocking:
                damage = int(round(damage * (1 - BLOCK_DAMAGE_REDUCTION)))
                combat_log.append(f"{pid} {name} for {damage} damage "
                                  f"(reduced by Block).")
                if random.random() < BLOCK_STUN_CHANCE:
                    next_stun.add(pid)
                    combat_log.append(f"{foe_id}'s Block stunned {pid} for "
                                      f"round {rnd + 1}.")
            else:
                combat_log.append(f"{pid} {name} for {damage} damage.")
            foe.hp = max(0, foe.hp - damage)
            if foe.hp == 0:
                combat_log.append(f"{foe_id} was knocked out!")
                winner = pid
                break

        ended = winner is not None or rnd >= MAX_ROUNDS
        if ended:
            # Final state keeps the ending round's values.
            if winner is None:
                combat_log.append("Round limit reached; the duel is a draw.")
            self.broadcast_update(rnd, rnd, "GAME_OVER", combat_log)
            if winner:
                self.end_game(winner, "KNOCKOUT")
            else:
                self.end_game(None, "ROUND_LIMIT")
            return

        # Prepare the next round: expire old bonuses/stuns, apply new ones.
        for pid in PLAYER_IDS:
            k = self.knights[pid]
            k.multiplier = PREPARE_MULTIPLIER if pid in prepared else 1
            k.stunned = pid in next_stun
            k.execution_speed = 0 if k.stunned else None
        self.round += 1
        self.broadcast_update(rnd, self.round, "SELECTING", combat_log)
        if self.state != State.EVALUATE_MOVE:
            return          # a send failure ended the match mid-broadcast
        self.set_state(State.PLAYER_TURN, "next round")
        self.begin_selection()

    def players_snapshot(self):
        return {p: self.knights[p].to_dict() for p in PLAYER_IDS}

    def broadcast_update(self, completed, next_round, phase, combat_log):
        """Send the same STATE_UPDATE to both players back-to-back."""
        payload = {
            "completed_round": completed,
            "round": next_round,
            "phase": phase,
            "players": self.players_snapshot(),
            "combat_log": combat_log,
        }
        for line in combat_log:
            log(f"   {line}")
        for pid in PLAYER_IDS:
            conn = self.slots[pid]
            if conn and not conn.disconnected:
                self.send(conn, "STATE_UPDATE", payload)

    # ---- GAME_OVER / CLEANUP ---------------------------------------------
    def end_game(self, winner, reason):
        """GAME_OVER: announce result to connected players, then CLEANUP."""
        if self.state in (State.GAME_OVER, State.CLEANUP):
            return
        self.set_state(State.GAME_OVER, f"{reason}, winner={winner}")
        payload = {
            "winner": winner,
            "reason": reason,
            "round": min(self.round, MAX_ROUNDS),
            "players": self.players_snapshot(),
        }
        for pid in PLAYER_IDS:
            conn = self.slots[pid]
            if conn and not conn.disconnected:
                self.send(conn, "GAME_OVER", payload)
        self.cleanup("result recorded")

    def cleanup(self, why):
        """CLEANUP: close match sockets (after flushing), reset the match."""
        if self.state == State.CLEANUP:
            return
        self.set_state(State.CLEANUP, why)
        for pid in PLAYER_IDS:
            conn = self.slots[pid]
            if conn:
                self.mark_closing(conn)
        self.reset_match()
        self.set_state(State.WAITING_FOR_PLAYERS, "reset")

    # ---- disconnect handling (single teardown path) -----------------------
    def drop(self, conn, reason, graceful=False):
        """Handle a departure exactly once, then close the socket."""
        first_time = not conn.disconnected
        conn.disconnected = True
        if first_time:
            log(f"{conn.label()} disconnected ({reason})")
            self.handle_departure(conn)
        if graceful:
            conn.closing = True
            if conn.outbuf and conn.sock in self.conns:
                self.set_interest(conn, writable=True)
                return
        self.close_socket(conn)

    def handle_departure(self, conn):
        """Apply the FSM section 6 table for a player leaving."""
        pid = conn.player_id
        if pid is None:
            return                                  # never joined
        if self.state == State.WAITING_FOR_PLAYERS:
            self.slots[pid] = None                  # free the slot
        elif self.state in (State.PLAYER_TURN, State.GAME_START,
                            State.EVALUATE_MOVE):
            other = "Player_2" if pid == "Player_1" else "Player_1"
            other_conn = self.slots[other]
            if other_conn is not None and not other_conn.disconnected:
                self.end_game(other, "FORFEIT")     # remaining player wins
            else:
                self.cleanup("both players lost")


# --------------------------------------------------------------------------
# CLI
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
        description="Knight Duel server: hosts one 1v1 match at a time.",
        epilog="example: python3 server.py -p 8080")
    parser.add_argument("-p", dest="port", type=port_number, required=True,
                        metavar="PORT",
                        help=f"listening port ({MIN_PORT}-{MAX_PORT}); binds "
                             f"to {BIND_HOST}")
    return parser.parse_args(argv)


def main():
    args = parse_args()
    server = KnightDuelServer(args.port)
    try:
        server.start()
        server.run()
    except OSError as exc:
        print(f"[ERROR] Cannot start server on port {args.port}: {exc}")
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nServer shutting down.")
    finally:
        for conn in list(server.conns.values()):
            server.close_socket(conn)
        if server.listener:
            server.listener.close()


if __name__ == "__main__":
    main()
