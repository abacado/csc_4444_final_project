# Poker AI Final

A Texas Hold'em server built for bot-vs-bot tournaments and a live, read-only poker-table visualization.

The repository includes:

- A Python websocket server for game orchestration
- A Texas Hold'em engine with blinds, betting rounds, community cards, side pots, and showdown logic
- A browser UI for the instructor (table view, chip-stack leaderboard, elimination notifications)
- Bot-controlled hand starts, reconnect handling, and disconnect folds
- Docker support for local deployment and an end-to-end websocket smoke test
- Unit tests for engine rules plus websocket admission/observer behavior

## Local development

1. Create and activate a virtual environment:
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   ```

2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

3. Start the server:
   ```bash
   uvicorn app.server:app --reload --host 0.0.0.0 --port 8000
   ```

4. Open the browser at:
   ```text
   http://localhost:8000/
   ```

## Table rules

| Setting | Value |
|---|---|
| Starting stack | $1000 |
| Blinds | $10 / $20 |
| Max players | 20 |
| Max name length | 24 characters (longer names are truncated) |

A hand can start once at least two connected players have chips. The dealer
button moves to the next player with chips each hand. A player whose stack
reaches $0 is out and is skipped for future hands.

## Websocket protocol

The server exposes a websocket endpoint at `/ws`. Every message, in both
directions, is a JSON object with a `type` field.

The browser is an instructor observer. It sends `{"type": "observe"}`, never
takes a seat or receives cards, and runs the table with a single **Start** /
**Pause** / **Continue** button (see [Continuous play](#continuous-play)). Bots
own all poker actions through `/ws`.

### Messages your bot sends

#### Join

```json
{
  "type": "join",
  "name": "Bot Group 1",
  "player_id": "group-1"
}
```

`player_id` is a stable, non-empty identifier. Reconnecting with the same ID
reclaims that bot's seat and stack (the old connection is closed with code
`4001`). A bot that disconnects during a hand is folded for that hand.

#### Action

```json
{
  "type": "action",
  "action": "raise",
  "amount": 60
}
```

Allowed actions:

| Action | Meaning |
|---|---|
| `fold` | Give up the hand. |
| `check` | Pass without betting. Only legal when your `bet` already equals the highest `bet` at the table. |
| `call` | Match the highest `bet`. If you are short, this puts you all-in. Calling when there is nothing to call behaves like a check. |
| `raise` | Raise the bet. `amount` is required (see below). |

For `raise`, `amount` is your desired **total bet for the current betting
round**, not the extra chips added on top of what you already put in. For
example, if the big blind is $20 and you want to make it $60, send
`"amount": 60`. The raise must:

- be greater than the current highest bet,
- increase the bet by at least the minimum raise (the big blind, or the size of the last full raise this round, whichever is larger),
- not exceed your `bet + stack`. Raising to exactly `bet + stack` is an all-in and is allowed even if it is smaller than a minimum raise.

One edge case: if someone goes all-in for less than a full raise, players who
already acted this round may only call or fold, not re-raise.

`call`, `check`, and `fold` may omit `amount`.

#### Other messages

| Message | Effect |
|---|---|
| `{"type": "start_hand"}` | Deal a single hand if the table is ready and not paused. Usually unnecessary: once the instructor presses Start, hands are dealt automatically. |
| `{"type": "ping"}` | Server replies `{"type": "pong"}`. |

### Messages your bot receives

#### State snapshot

The server broadcasts a fresh snapshot to every connection after **every**
change: a player joins, a hand is dealt, someone acts, a hand pays out.
This is the main message your bot reacts to.

```json
{
  "type": "state",
  "payload": {
    "phase": "FLOP",
    "pot": 120,
    "community_cards": [
      {"rank": 14, "suit": "spades", "label": "AS"},
      {"rank": 10, "suit": "hearts", "label": "10H"},
      {"rank": 7,  "suit": "clubs",  "label": "7C"}
    ],
    "players": [
      {
        "id": "group-1",
        "name": "Bot Group 1",
        "stack": 940,
        "bet": 20,
        "folded": false,
        "all_in": false,
        "connected": true,
        "hole_cards": [
          {"rank": 13, "suit": "diamonds", "label": "KD"},
          {"rank": 13, "suit": "clubs",    "label": "KC"}
        ]
      },
      {
        "id": "group-2",
        "name": "Bot Group 2",
        "stack": 960,
        "bet": 0,
        "folded": false,
        "all_in": false,
        "connected": true,
        "hole_cards": [
          {"rank": "?", "suit": "?", "label": "??"},
          {"rank": "?", "suit": "?", "label": "??"}
        ]
      }
    ],
    "current_turn": "group-2",
    "dealer": "group-1",
    "hand_number": 3,
    "last_action": {"type": "raise", "player": "group-1", "amount": 20, "target": 20},
    "small_blind": 10,
    "big_blind": 20,
    "can_start": false,
    "running": true,
    "paused": false,
    "speed": {"action_delay": 0.0, "showdown_reveal": 3.0}
  }
}
```

**Top-level fields**

| Field | Description |
|---|---|
| `phase` | `WAITING`, `PREFLOP`, `FLOP`, `TURN`, `RIVER`, or `SHOWDOWN` |
| `pot` | Total chips in the pot, including bets on the current round |
| `community_cards` | Board cards dealt so far (0, 3, 4, or 5) |
| `players` | Every seated player, in join order (see below) |
| `current_turn` | `player_id` of the player who must act, or `null` |
| `dealer` | `player_id` of the dealer button, or `null` |
| `hand_number` | Increments every time a hand is dealt |
| `last_action` | The single most recent event (see below), or `null` |
| `small_blind`, `big_blind` | Blind sizes |
| `can_start` | `true` if a new hand can be dealt right now |
| `running` | `true` once the instructor has pressed Start (see [Continuous play](#continuous-play)) |
| `paused` | `true` while the instructor has paused the game |
| `speed` | `{"action_delay": s, "showdown_reveal": s}`, the instructor's pacing settings (see [Game speed](#game-speed)) |

**Player fields**

| Field | Description |
|---|---|
| `id`, `name` | The player's `player_id` and display name |
| `stack` | Chips not yet committed to the pot |
| `bet` | Chips this player has put in **during the current betting round** (resets to 0 each street) |
| `folded` | Folded this hand |
| `all_in` | Has no chips left behind and cannot act further this hand |
| `connected` | Currently has a live websocket |
| `hole_cards` | Your own two cards; other players' cards are `"??"` placeholders (see [Hidden information](#hidden-information)) |

**Cards** are `{"rank": int, "suit": str, "label": str}`. `rank` is 2–14
(11 = J, 12 = Q, 13 = K, 14 = A). `suit` is `hearts`, `diamonds`, `clubs`, or
`spades`. `label` is a short display string such as `AS` or `10H`.

**`last_action` shapes**

The snapshot only carries the most recent event. If your bot wants the full
betting history of a hand, record each `last_action` as snapshots arrive.

```json
{"type": "deal",   "dealer": "group-1", "small_blind": "group-2", "big_blind": "group-3"}
{"type": "fold",   "player": "group-2"}
{"type": "check",  "player": "group-2"}
{"type": "call",   "player": "group-2", "amount": 20}
{"type": "raise",  "player": "group-2", "amount": 50, "target": 60}
{"type": "payout", "winners": ["group-3"], "pot": 140}
{"type": "payout", "winners": ["group-1", "group-3"], "payouts": {"group-1": 70, "group-3": 70}, "pot": 140}
```

For `call` and `raise`, `amount` is the chips added by that action and
`target` is the player's new total `bet` for the round. `payouts` is included
when the pot went to showdown.

**Values the server does not send directly**

Compute these from the snapshot:

```python
me = next(p for p in state["players"] if p["id"] == MY_ID)
highest_bet = max(p["bet"] for p in state["players"])
to_call = highest_bet - me["bet"]          # 0 means you may check
max_raise_target = me["bet"] + me["stack"] # raising to this is all-in
```

The minimum raise is not included in the snapshot. Raising to at least
`highest_bet + big_blind` is always safe on a round with no earlier raises; if
a raise is rejected, the error message states the required increase.

#### Action result

Sent only to the bot that acted, immediately before the next state broadcast.

Success:

```json
{
  "type": "action_result",
  "payload": {
    "ok": true,
    "phase": "FLOP",
    "pot": 120,
    "current_turn": "group-3",
    "action": {"type": "call", "player": "group-2", "amount": 20},
    "state": { "...": "your private snapshot, same shape as above" }
  }
}
```

Rejected (illegal or out-of-turn action):

```json
{
  "type": "action_result",
  "payload": {
    "ok": false,
    "error": "Raise must increase the bet by at least 20"
  }
}
```

A rejected action does **not** advance the game. It is still your turn, and
you must send a legal action. Possible errors include:

- `It is not this player's turn`
- `Cannot check while facing a bet`
- `Raise target must exceed the current bet`
- `Raise exceeds available stack`
- `Raise must increase the bet by at least N`
- `A short all-in did not reopen raising`
- `No hand is in progress`
- `Hand is at showdown; waiting for payout`

#### Errors

Problems with the message itself, joining, or starting a hand come back as
`error` messages:

```json
{
  "type": "error",
  "message": "Need at least two connected players with chips to start"
}
```

Examples: invalid JSON, a missing `name`/`player_id` on join, sending an
action before joining, a full table, or an unknown `type`.

### Hand lifecycle

1. `WAITING`: no hand in progress. While the table is running, the next hand is dealt automatically after a short gap.
2. `PREFLOP`: blinds are posted, each player gets two hole cards, and the player after the big blind acts first.
3. `FLOP` / `TURN` / `RIVER`: three, one, then one community card. Betting starts with the first active player after the dealer.
4. If everyone but one player folds, that player wins the pot immediately and the phase returns to `WAITING`.
5. Otherwise, after the river betting round (or once everyone left is all-in), the remaining hands are compared:
   - The phase becomes `SHOWDOWN`, `last_action` is the `payout`, and the hole cards of every player who did not fold are revealed to all connections. **Stacks are not yet updated** in this snapshot.
   - After the showdown pause (3 seconds by default, see [Game speed](#game-speed)) the pot is paid out, cards are hidden again, and the phase returns to `WAITING`.
   - Actions sent during `SHOWDOWN` are rejected.

### Game speed

The instructor page has two sliders that set the pace for **every** bot:

- **Pause per action** (0–3s, default 0): after each accepted action, the server waits this long before broadcasting the new state. Your bot sees its turn later, but nothing about the rules changes.
- **Showdown pause** (0–10s, default 3): how long the `SHOWDOWN` reveal lasts before payout.

Observers change these with `{"type": "set_speed", "action_delay": 0.5, "showdown_reveal": 2}`. Values are clamped to the ranges above, and bots cannot send this message.

### Continuous play

The instructor page has one button that runs the table:

- **Start** (`{"type": "resume"}`): begins continuous play. Whenever the table is idle and at least two connected bots have chips, the server deals the next hand after a short gap (0.5s, or the **Pause per action** value if that is longer), so every payout is visible before the next deal. If fewer than two bots have chips, play waits until more join.
- **Pause** (`{"type": "pause"}`): an action already sent is still accepted, but the state that hands the turn to the next bot is held, and so are the showdown payout and the next deal. `start_hand` is rejected while paused.
- **Continue** (`{"type": "resume"}`): picks up exactly where it stopped.

Only observers may send `pause` / `resume`. To your bot, a pause just looks like a long wait for its next turn; no extra message is sent, so your bot should never assume a turn arrives within a fixed time.

### Hidden information

- Each bot's snapshot includes only its **own** hole cards. Opponents' cards are `"??"`.
- Observers (the instructor browser) see no hole cards while betting is live.
- During `SHOWDOWN`, everyone (bots and observers) sees the hole cards of each player who did not fold. Folded hands are never revealed, and no cards are revealed when a hand ends by everyone folding.
- The deck and undealt cards are never sent.

## Bot client example

A minimal bot that plays legally. It acts only on its turn, uses
`to_call` to choose between check and call, and falls back to a safe action if
the server rejects a move. Without that fallback, a bot that keeps sending the
same illegal action will loop forever, because the server re-sends the same
state.

```python
import asyncio
import json
import websockets

MY_ID = "group-1"
MY_NAME = "Bot Group 1"


def choose_action(state):
    """Replace this with your strategy. Return (action, amount)."""
    me = next(p for p in state["players"] if p["id"] == MY_ID)
    highest_bet = max(p["bet"] for p in state["players"])
    to_call = highest_bet - me["bet"]

    if to_call == 0:
        return "check", 0
    return "call", 0


async def main():
    uri = "ws://localhost:8000/ws"
    async with websockets.connect(uri) as websocket:
        await websocket.send(json.dumps({"type": "join", "name": MY_NAME, "player_id": MY_ID}))
        last_sent = None

        while True:
            message = json.loads(await websocket.recv())
            msg_type = message.get("type")

            if msg_type == "state":
                state = message["payload"]
                if state["current_turn"] == MY_ID:
                    action, amount = choose_action(state)
                    last_sent = action
                    await websocket.send(json.dumps({"type": "action", "action": action, "amount": amount}))

            elif msg_type == "action_result":
                result = message["payload"]
                if not result["ok"]:
                    print("Rejected:", result["error"])
                    # Illegal move: it is still our turn. Fall back once to
                    # a call, which is legal whenever it is our turn, then
                    # fix the strategy. Never retry the fallback itself.
                    if last_sent != "call":
                        last_sent = "call"
                        await websocket.send(json.dumps({"type": "action", "action": "call"}))

            elif msg_type == "error":
                print("Server error:", message["message"])


asyncio.run(main())
```

`call` is always legal on your turn: it matches the bet, goes all-in if you
are short, or acts as a check when there is nothing to call. A rejected `call`
means it was not actually your turn (for example, a stale message), so the bot
just waits for the next state.

Tips:

- The same snapshot is broadcast after every change, so your bot will see many states where it is not its turn. Only act when `current_turn` is your ID.
- Use `phase == "SHOWDOWN"` snapshots to see what opponents held, and record `last_action` events if you want to model their betting.
- Use `hand_number` to detect a new hand and reset any per-hand memory.

## Docker deployment

Build the image:

```bash
docker build -t ai-poker-server .
```

Run it directly:

```bash
docker run -p 8000:8000 ai-poker-server
```

Run the server and spectator UI with Compose:

```bash
docker-compose up --build
```

Open [http://localhost:8000](http://localhost:8000) for the instructor table,
and use `http://localhost:8000/health` for a machine-readable health check.


## Testing server

Run the unit and websocket tests locally:

```bash
pytest -q
```

The test suite covers blind/action order, legal betting transitions, folds,
all-ins and side pots, best-five-of-seven hand evaluation, private snapshots,
the showdown reveal, and observer/websocket admission rules.

For a full containerized check that includes real websocket traffic, run:

```bash
docker-compose --profile smoke up --build --abort-on-container-exit --exit-code-from poker-smoke
```

Compose starts the server, waits for `/health`, then `poker-smoke` connects
three simple call/check bots. The command succeeds only after the server deals,
plays, and pays out one complete hand. Stop and remove the containers afterward
with `docker-compose down`.

### Manual UI testing/demo with simple bots

The demo bots only call or check. They never fold or raise, so every hand goes
to showdown. They exist to exercise the server and UI, not to play well.

Start the server and three persistent demo bots in Docker:

```bash
docker-compose --profile demo up --build
```

Then open [http://localhost:8000](http://localhost:8000). Once the three demo
bots appear, press **Start**. Hands then deal continuously and the bots call
or check until you press **Pause**. Stop the demo with `Ctrl+C`, then run
`docker-compose down`.

Alternatively, run the bots from a terminal while the server is already running:

```bash
source .venv/bin/activate
python scripts/demo_bots.py
```

Options:

| Flag | Effect |
|---|---|
| `--count N` | Number of bots to seat, 2–20 (default 3) |
| `--auto-start` | The first demo bot starts every hand without the UI |
| `--delay S` | Wait `S` seconds before each bot action (default 0). Usually unnecessary: the instructor page's speed sliders pace all bots. |
| `--url URL` | Websocket endpoint (default `ws://localhost:8000/ws`, or `$POKER_WS_URL`) |
