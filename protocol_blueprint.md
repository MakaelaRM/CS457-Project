# Knight Duel: Protocol Blueprint

**Student:** Makaela Maryanski  
**Course:** CS 457  
**Date:** September 30, 2026

## 1. Game Overview

Knight Duel is a two-player console battle game. Both players choose an action during a selection phase unless they are stunned. The server collects the required actions, resolves who acts first using execution speed, and sends the results to both clients.

| Action | Description |
| --- | --- |
| `SWORD_ATTACK` | Higher damage with a higher stamina cost and lower action speed. |
| `BOW_ATTACK` | Lower damage with a lower stamina cost and higher action speed. |
| `BLOCK` | Activates before attacks, reduces incoming damage for that round, and has a chance to stun the attacker for the next round. |
| `PREPARE` | Costs no stamina, restores HP and stamina, and gives a speed multiplier for the next round only. |

There is no base speed stat. Execution speed is calculated as `action speed × Prepare multiplier`. Without a Prepare bonus, the multiplier is 1. Higher execution speed acts first. If execution speeds tie, the server gives each Knight a 50% chance of acting first. Block's protection activates before attacks regardless of execution speed.

Prepare bonuses do not stack and expire after the following round, even if the Knight is stunned that round. Using Prepare again grants a new bonus for the next round. HP and stamina cannot exceed their starting maximums.

A stunned Knight has execution speed 0 and automatically skips its action for that round, including Block or Prepare. The stun then expires.

A player wins when the opponent reaches 0 HP. The server checks HP after each action and ends the battle immediately after a knockout, so the defeated Knight cannot act and double knockouts are not possible. Completing 30 rounds without a knockout results in a draw. Leaving during battle causes a forfeit.

## 2. Transport and Framing

Knight Duel uses TCP to send JSON messages encoded in UTF-8. Messages use newline-delimited JSON (Option A), meaning each JSON object is sent on one line ending with a newline (`\n`, byte `0x0A`). Newlines inside string values are escaped by the JSON serializer. Since TCP can split a message across multiple receives or deliver several messages together, each connection keeps a byte buffer. The receiver adds incoming bytes to the buffer, extracts each complete newline-terminated message, and decodes and parses its JSON. Incomplete data stays in the buffer until the rest arrives. Each message can contain up to 16,384 bytes, including the newline. If a message exceeds this limit, the server sends an `ERROR` when possible and closes the connection.

### Wire Examples

In these examples, `\n` represents an actual newline byte.

**Example 1: Coalescing (two messages in one receive).** A client joins and immediately quits before receiving its assigned ID. Even if both messages arrive in one receive, the receiver uses the newlines to process them separately.

```text
{"msg_type":"CONNECT","player_id":null,"payload":{"alias":"Makaela"},"timestamp":1790787600}\n{"msg_type":"DISCONNECT","player_id":null,"payload":{"reason":"QUIT"},"timestamp":1790787601}\n
```

**Example 2: Fragmentation (one message split across two receives).** A `MOVE` message arrives in two pieces. After the first `recv()` there is no `\n` in the buffer, so the receiver keeps waiting and does not parse anything. After the second `recv()` the newline is in the buffer, so the full message is extracted and parsed.

```text
recv() #1: {"msg_type":"MOVE","player_id":"Player_1","payload":{"round":1,"act
recv() #2: ion":"BLOCK"},"timestamp":1790787610}\n
```

## 3. Shared Message Fields

Every message contains these required fields:

| Field | Type | Meaning |
| --- | --- | --- |
| `msg_type` | String | One of the eight message types below. |
| `player_id` | String or null | Sender: `Player_1`, `Player_2`, or `SERVER`. A client uses null before receiving an ID. |
| `payload` | Object | Information specific to the message. |
| `timestamp` | Integer | Nonnegative Unix time in seconds, used for logging. |

`SERVER` means the message came from the server, not a third player. The server identifies clients by their connections and does not trust a supplied player ID alone.

All listed fields are required. Incorrect types, extra fields, and invalid values are rejected. Player aliases must be nonblank strings and unique within the match.

### Message Summary

| Message Type | Direction | Purpose |
| --- | --- | --- |
| `CONNECT` | Client → Server | Request to join the game with an alias. |
| `LOBBY_WAIT` | Server → Waiting client | Assign Player 1's ID and tell them to wait for an opponent. |
| `GAME_START` | Server → Each client | Start the battle and send starting stats. |
| `MOVE` | Client → Server | Submit an action for the current round. |
| `STATE_UPDATE` | Server → Both clients | Send updated stats and the round's combat log. |
| `ERROR` | Server → Client that caused it | Explain why a message or action was rejected. |
| `DISCONNECT` | Client → Server | Tell the server the player is quitting. |
| `GAME_OVER` | Server → Both clients (or the remaining client) | Announce the result and final stats. |

### Knight Stats

The `players` object contains two entries: `Player_1` and `Player_2`. Each entry contains:

| Field | Type | Meaning |
| --- | --- | --- |
| `alias` | String | Player’s display name. |
| `hp` | Integer ≥ 0 | Remaining health. Damage lowers it; Prepare restores it. |
| `stamina` | Integer ≥ 0 | Resource used to pay action costs; Prepare restores it. |
| `execution_speed` | Number ≥ 0 or null | Action speed multiplied by the Prepare multiplier. Null before actions are resolved; 0 when stunned. |
| `speed_multiplier` | Number ≥ 1 | Prepare bonus for the indicated round. A value of 1 means no bonus. |
| `stunned` | Boolean | True if the Knight must skip its action in the indicated round. |

The server calculates all stat changes. Clients receive starting stats in `GAME_START`, updated stats in `STATE_UPDATE`, and final stats in `GAME_OVER`.

During selection, `execution_speed` is null for a Knight that can act, so hidden action choices are not revealed. It is 0 for a stunned Knight. After a round, `STATE_UPDATE` uses the next round's multiplier and stun status and resets execution speed for selection. If the battle has ended, the final state keeps the ending round's values; an execution speed that was never calculated remains null.

Block and stun results are also described in the combat log. Example numbers below are sample values, not final game balance settings.

## 4. Message Definitions

All examples below are JSON messages. **Some are printed across several lines to make them easier to read, but on the wire every message is sent as one line ending with a newline.**

### CONNECT

**Direction:** Client → Server  
**Purpose:** Request to join the game.

**Payload:** `alias` (string). The top-level `player_id` is null.

```json
{"msg_type":"CONNECT","player_id":null,"payload":{"alias":"Makaela"},"timestamp":1790787600}
```

### LOBBY_WAIT

**Direction:** Server → Waiting client  
**Purpose:** Assign the player an ID and tell them to wait for an opponent.

Only the first player receives `LOBBY_WAIT`. The second player to join does not get one and learns their ID from `GAME_START`.

**Payload:**

- `assigned_player_id` (string): `Player_1` or `Player_2`.
- `message` (string): Waiting notice.

```json
{"msg_type":"LOBBY_WAIT","player_id":"SERVER","payload":{"assigned_player_id":"Player_1","message":"Waiting for opponent."},"timestamp":1790787601}
```

### GAME_START

**Direction:** Server → Each client  
**Purpose:** Start the battle and send the initial Knight stats.

**Payload:**

- `assigned_player_id` (string): Recipient’s player ID.
- `round` (integer): 1.
- `phase` (string): `SELECTING`.
- `players` (object): Both Knights using the shared stats format.

```json
{
  "msg_type": "GAME_START",
  "player_id": "SERVER",
  "payload": {
    "assigned_player_id": "Player_1",
    "round": 1,
    "phase": "SELECTING",
    "players": {
      "Player_1": {
        "alias": "Makaela",
        "hp": 100,
        "stamina": 50,
        "execution_speed": null,
        "speed_multiplier": 1,
        "stunned": false
      },
      "Player_2": {
        "alias": "Opponent",
        "hp": 100,
        "stamina": 50,
        "execution_speed": null,
        "speed_multiplier": 1,
        "stunned": false
      }
    }
  },
  "timestamp": 1790787605
}
```

The other client receives the same state with its own assigned ID.

### MOVE

**Direction:** Client → Server  
**Purpose:** Submit an action for the current round.

**Payload:**

- `round` (integer): Current round, from 1 through 30.
- `action` (string): `SWORD_ATTACK`, `BOW_ATTACK`, `BLOCK`, or `PREPARE`.

```json
{"msg_type":"MOVE","player_id":"Player_1","payload":{"round":1,"action":"SWORD_ATTACK"},"timestamp":1790787610}
```

A stunned player does not send `MOVE`. The server automatically skips that player's action.

### STATE_UPDATE

**Direction:** Server → Both clients  
**Purpose:** Send updated Knight stats and the completed round’s combat log.

**Payload:**

- `completed_round` (integer): Round just resolved, from 1 through 30.
- `round` (integer): Next selection round, or final round if the battle ended.
- `phase` (string): `SELECTING` or `GAME_OVER`.
- `players` (object): Both Knights’ updated stats.
- `combat_log` (array of strings): Events in execution order.

```json
{
  "msg_type": "STATE_UPDATE",
  "player_id": "SERVER",
  "payload": {
    "completed_round": 1,
    "round": 2,
    "phase": "SELECTING",
    "players": {
      "Player_1": {
        "alias": "Makaela",
        "hp": 100,
        "stamina": 35,
        "execution_speed": 0,
        "speed_multiplier": 1,
        "stunned": true
      },
      "Player_2": {
        "alias": "Opponent",
        "hp": 90,
        "stamina": 45,
        "execution_speed": null,
        "speed_multiplier": 1,
        "stunned": false
      }
    },
    "combat_log": [
      "Player_2 blocked.",
      "Player_1 attacked for 10 damage.",
      "Player_2's Block stunned Player_1 for round 2."
    ]
  },
  "timestamp": 1790787615
}
```

Clients replace their displayed stats with the server’s values.

### ERROR

**Direction:** Server → Client that caused the error  
**Purpose:** Explain why a message or action was rejected.

**Payload:**

- `code` (string): Error code from Section 5.
- `message` (string): Explanation.

```json
{"msg_type":"ERROR","player_id":"SERVER","payload":{"code":"INVALID_MOVE","message":"Not enough stamina for this action."},"timestamp":1790787616}
```

### DISCONNECT

**Direction:** Client → Server  
**Purpose:** Tell the server the player is quitting.

**Payload:** `reason` (string): `QUIT`.

```json
{"msg_type":"DISCONNECT","player_id":"Player_1","payload":{"reason":"QUIT"},"timestamp":1790787620}
```

A client leaving before receiving its ID uses null. The server identifies the departing player from their connection.

### GAME_OVER

**Direction:** Server → Both connected clients, or the remaining client after a forfeit  
**Purpose:** Announce the result and final Knight stats.

**Payload:**

- `winner` (string or null): `Player_1`, `Player_2`, or null for a draw.
- `reason` (string): `KNOCKOUT`, `ROUND_LIMIT`, or `FORFEIT`.
- `round` (integer): Ending round, from 1 through 30.
- `players` (object): Both Knights’ final stats.

A `FORFEIT` happens when a player sends `DISCONNECT`, the connection drops, or the player runs out of time to submit an action (see Section 6).

```json
{
  "msg_type": "GAME_OVER",
  "player_id": "SERVER",
  "payload": {
    "winner": "Player_2",
    "reason": "FORFEIT",
    "round": 2,
    "players": {
      "Player_1": {
        "alias": "Makaela",
        "hp": 100,
        "stamina": 35,
        "execution_speed": 0,
        "speed_multiplier": 1,
        "stunned": true
      },
      "Player_2": {
        "alias": "Opponent",
        "hp": 90,
        "stamina": 45,
        "execution_speed": null,
        "speed_multiplier": 1,
        "stunned": false
      }
    }
  },
  "timestamp": 1790787621
}
```

Knight Duel uses the result and final stats instead of a separate numeric score.

## 5. Validation and Round Handling

The server assigns the first available player slot. The first player waits until the second joins. A third client receives `ROOM_FULL` and is disconnected.

During `SELECTING`, each non-stunned player may submit one valid action. The server checks the player’s identity, round number, action, stun status, and available stamina. Invalid actions do not use up a choice or change the game state. Stunned players automatically skip their action and do not need to submit.

Accepted actions cannot be changed and remain hidden until all required actions are received. The server calculates execution speeds once for the round, activates any valid Blocks, and resolves the remaining actions in speed order. Ties are decided with a 50/50 choice. A knockout stops resolution immediately. The server then sends `STATE_UPDATE`. Moves outside the selection phase are rejected.

If both Knights are stunned, the server skips both actions and completes the round without waiting for input. A Block stun applies to the following round, not the current one.

| Error code | Meaning |
| --- | --- |
| `INVALID_MESSAGE` | Invalid UTF-8/JSON, empty line, unknown message type, or incorrect fields. |
| `INVALID_PLAYER` | Invalid/duplicate alias, client has not joined, or ID does not match the connection. |
| `INVALID_MOVE` | Unknown action, wrong round, duplicate action submission, insufficient stamina, or an action submitted while stunned. |
| `INVALID_PHASE` | Message is not allowed now or in that direction. |
| `ROOM_FULL` | Two players already joined. |
| `MESSAGE_TOO_LARGE` | Message exceeds the size limit. |

Ordinary errors send `ERROR` and leave the connection open so the client can retry when allowed. `ROOM_FULL` and `MESSAGE_TOO_LARGE` close the affected connection after an attempted error response.

A completed final round sends `STATE_UPDATE` with phase `GAME_OVER`, followed by `GAME_OVER`. Round 31 never starts.

## 6. Disconnects and Cleanup

- **Intentional quit:** The client sends `DISCONNECT` before closing its socket.
- **Clean TCP closure:** TCP FIN closes the transport connection. This is separate from the application’s `DISCONNECT` message.
- **EOF:** If `recv()` returns `b""`, stop reading that socket and handle the disconnect. Continuing to read would cause an infinite loop.
- **Unexpected failure:** Catch `ConnectionResetError`, `BrokenPipeError`, `ConnectionAbortedError`, and socket timeout errors. A reset may signal a failed connection; a silent network drop may require a timeout.
- **Before battle:** Free the disconnected player’s slot and keep the remaining player waiting.
- **During battle:** The remaining player wins by forfeit and receives `GAME_OVER`.
- **Cleanup:** Close affected sockets and discard their buffers, incomplete messages, and stored actions. Handle each departure only once.

After the match, the server closes the match connections and resets for new players. Players reconnect for another match. If both connections are lost before a result is recorded, the server cleans up without assigning a winner. Later disconnects do not change an already recorded result.

**Timeout policy:** Non-stunned players have 120 seconds from the start of selection to submit a valid action. Invalid messages do not restart the deadline. One missing required action causes a forfeit, so the server sends `GAME_OVER` with `reason` set to `FORFEIT` and the player who did submit wins. If both players are required to act and neither submits, close both connections and reset without a winner. A player who already submitted or is automatically skipping due to stun is not penalized for waiting. Socket writes time out after 10 seconds.

## 7. Remaining Game Decisions

Exact starting HP and stamina, damage, stamina costs, action speeds, Prepare recovery amounts and multiplier, Block damage reduction, and Block stun likelihood will be finalized later.
