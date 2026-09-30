# Knight Duel: AI Prompting & Constraint Strategy

**Student:** Makaela Maryanski  
**Course:** CS 457  
**Date:** September 30, 2026

## 1. My Strategy

The AI needs to follow my docs instead of making up its own design choices. When I use AI to write code, I:

1. Start every session with the system prompt below.
2. Ask for one small piece of code at a time.
3. Tell the AI what it is not allowed to do.
4. Check the code against my blueprint before I use it. If the AI breaks a rule, I tell it which rule it broke and ask again.

## 2. System Prompt

I paste this at the start of every AI coding session.

```text
You are helping me write a TCP client and server in Python for a two-player
game called Knight Duel. I attached my protocol_blueprint.md and
fsm_specification.md. They are the source of truth. Follow them exactly and
do not invent anything new.

Key rules:
- Messages are newline-delimited JSON (one line, ends with \n). No other
  framing.
- Never assume one recv() is one whole message. Use a buffer per connection.
- If recv() returns b"", the peer closed the connection. Stop reading.
- Every message has exactly msg_type, player_id, payload, and timestamp.
- Only use the message types, actions, error codes, and states in my docs.
- Bad input gets an ERROR reply and must not crash the loop.
- Only use the Python standard library. Do not add features I did not ask
  for. If something is not covered in my docs, ask me.
```

## 3. Parser and Serialization Prompts


**Receive buffer (parser)**

```text
Write a function that reads newline-delimited JSON messages from a socket.
Keep a bytes buffer, call recv(4096), and if it returns b"" tell the caller
the connection closed. While there is a \n in the buffer, split off one
line, decode it as UTF-8, and parse it with json.loads. If a line is over
16384 bytes, return MESSAGE_TOO_LARGE. If the JSON or UTF-8 is bad, return
INVALID_MESSAGE and keep going. Keep any leftover partial message in the
buffer. Do not use length prefixes.
```

**Sending messages (serialization)**

```text
Write send_message(sock, msg_type, player_id, payload). Build a dict with
exactly msg_type, player_id, payload, and timestamp (int(time.time())). Use
json.dumps with separators=(",", ":") so it stays on one line, add b"\n" to
the end, and send it with sendall. Only allow the 8 message types. Catch
BrokenPipeError and ConnectionResetError and return False.
```


## 4. Fixing Bad AI Output

If the AI breaks my design, I tell it which rule it broke. 
