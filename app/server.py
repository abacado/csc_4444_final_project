import asyncio
import json
from pathlib import Path
from typing import Dict, Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.engine import PokerEngine

BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = BASE_DIR / "frontend" / "static"

app = FastAPI(title="Poker AI Final Showdown")
engine = PokerEngine(starting_stack=1000, small_blind=10, big_blind=20)
connections: Dict[str, WebSocket] = {}
spectators: set[WebSocket] = set()
# How long a contested showdown holds with hole cards revealed before the
# pot pays out and the table reopens. Tests shrink this to stay fast.
SHOWDOWN_REVEAL_SECONDS = 3.0
_showdown_reveal_task: Optional[asyncio.Task] = None
# Pause after each accepted action before the new state goes out, so the
# instructor can pace any bots (not just the demo ones) from the browser.
ACTION_DELAY_SECONDS = 0.0
MAX_ACTION_DELAY_SECONDS = 3.0
MAX_SHOWDOWN_REVEAL_SECONDS = 10.0
# While paused, the server withholds the broadcast that follows an action
# (and the showdown payout), so no bot learns it is their turn. A plain flag
# polled by _wait_while_paused() avoids binding an asyncio.Event to one loop.
paused = False
PAUSE_POLL_SECONDS = 0.05
# Once the instructor presses Start, the table plays continuously: each time
# a hand pays out, the next one is dealt after a short gap (long enough for
# observers to see the payout) until the instructor pauses.
running = False
MIN_HAND_GAP_SECONDS = 0.5
_next_hand_task: Optional[asyncio.Task] = None


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/health")
def health():
    return {"status": "ok", "players": len(engine.players), "phase": engine.phase}


async def _send(websocket: WebSocket, payload: dict) -> bool:
    try:
        await websocket.send_json({"type": "state", "payload": payload})
        return True
    except Exception:
        return False


def _table_controls() -> dict:
    """Public metadata for the instructor UI; gameplay remains engine-owned."""
    speed = {"action_delay": ACTION_DELAY_SECONDS, "showdown_reveal": SHOWDOWN_REVEAL_SECONDS}
    return {"can_start": engine.can_start_hand(), "running": running, "paused": paused, "speed": speed}


async def _wait_while_paused() -> None:
    while paused:
        await asyncio.sleep(PAUSE_POLL_SECONDS)


def _clamp_seconds(value, upper: float) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return min(max(float(value), 0.0), upper)


def _schedule_next_hand() -> None:
    """While the table is running, deal the next hand once the table is idle.
    Runs after the WAITING broadcast so observers see each payout."""
    global _next_hand_task
    if not running or paused or not engine.can_start_hand():
        return
    if _next_hand_task is not None and not _next_hand_task.done():
        return
    _next_hand_task = asyncio.create_task(_deal_next_hand_after_gap())


async def _deal_next_hand_after_gap() -> None:
    await asyncio.sleep(max(MIN_HAND_GAP_SECONDS, ACTION_DELAY_SECONDS))
    await _wait_while_paused()
    if running and engine.can_start_hand():
        engine.start_hand()
        await broadcast_state()


def _schedule_showdown_reveal() -> None:
    """Once a contested showdown reveals hole cards, pay out and reopen
    the table after a delay so observers have time to see the hands."""
    global _showdown_reveal_task
    if engine.phase != "SHOWDOWN":
        return
    if _showdown_reveal_task is not None and not _showdown_reveal_task.done():
        return
    _showdown_reveal_task = asyncio.create_task(_finalize_showdown_after_delay())


async def _finalize_showdown_after_delay() -> None:
    await asyncio.sleep(SHOWDOWN_REVEAL_SECONDS)
    await _wait_while_paused()
    if engine.phase == "SHOWDOWN":
        engine.finalize_showdown()
        await broadcast_state()


async def broadcast_state() -> None:
    """Send every bot its own private cards and observers only the public table."""
    controls = _table_controls()
    for pid, websocket in list(connections.items()):
        payload = engine.snapshot(pid)
        payload.update(controls)
        if not await _send(websocket, payload):
            connections.pop(pid, None)
            engine.remove_player(pid)
    await broadcast_to_spectators(controls)
    _schedule_showdown_reveal()
    _schedule_next_hand()


async def broadcast_to_spectators(controls: Optional[dict] = None) -> None:
    """Refresh observers only. Used for instructor settings (speed, pause) so
    bots are not re-sent a turn they may already be acting on."""
    public = engine.snapshot()
    public.update(controls or _table_controls())
    for websocket in list(spectators):
        if not await _send(websocket, public):
            spectators.discard(websocket)


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    global ACTION_DELAY_SECONDS, SHOWDOWN_REVEAL_SECONDS, paused, running
    await websocket.accept()
    player_id: str | None = None
    is_observer = False
    try:
        while True:
            try:
                data = json.loads(await websocket.receive_text())
            except json.JSONDecodeError:
                await websocket.send_json({"type": "error", "message": "Invalid JSON payload"})
                continue
            if not isinstance(data, dict):
                await websocket.send_json({"type": "error", "message": "Message must be a JSON object"})
                continue
            msg_type = data.get("type")

            if msg_type == "observe":
                is_observer = True
                spectators.add(websocket)
                await broadcast_state()
                continue
            if msg_type == "join":
                requested_id = data.get("player_id")
                name = data.get("name")
                if not isinstance(requested_id, str) or not requested_id.strip() or not isinstance(name, str) or not name.strip():
                    await websocket.send_json({"type": "error", "message": "join requires non-empty name and player_id strings"})
                    continue
                if player_id is not None and requested_id != player_id:
                    await websocket.send_json({"type": "error", "message": "A connection may only control one player"})
                    continue
                try:
                    engine.add_player(requested_id.strip(), name.strip()[:24])
                except ValueError as error:
                    await websocket.send_json({"type": "error", "message": str(error)})
                    continue
                player_id = requested_id.strip()
                old_socket = connections.get(player_id)
                connections[player_id] = websocket
                if old_socket is not None and old_socket is not websocket:
                    try:
                        await old_socket.close(code=4001, reason="Replaced by reconnect")
                    except Exception:
                        pass
                await broadcast_state()
                continue
            if msg_type == "action":
                if player_id is None:
                    await websocket.send_json({"type": "error", "message": "Join the table first"})
                    continue
                amount = data.get("amount", 0)
                action = data.get("action")
                if not isinstance(action, str) or isinstance(amount, bool) or not isinstance(amount, int) or amount < 0:
                    await websocket.send_json({"type": "error", "message": "action must be text and amount must be a non-negative integer"})
                    continue
                result = engine.place_action(player_id, action, amount)
                await websocket.send_json({"type": "action_result", "payload": result})
                if result["ok"]:
                    if ACTION_DELAY_SECONDS:
                        await asyncio.sleep(ACTION_DELAY_SECONDS)
                    await _wait_while_paused()
                await broadcast_state()
                continue
            if msg_type == "start_hand":
                # The browser uses an observer connection as the instructor
                # console. It can start a ready table but never becomes a seat.
                if player_id is None and not is_observer:
                    await websocket.send_json({"type": "error", "message": "Join as a bot or observer first"})
                    continue
                if paused:
                    await websocket.send_json({"type": "error", "message": "The game is paused; press Continue first"})
                    continue
                if not engine.can_start_hand():
                    await websocket.send_json({"type": "error", "message": "Need at least two connected players with chips to start"})
                    continue
                engine.start_hand()
                await broadcast_state()
                continue
            if msg_type == "set_speed":
                if not is_observer:
                    await websocket.send_json({"type": "error", "message": "Only an instructor observer may change the game speed"})
                    continue
                action_delay = _clamp_seconds(data.get("action_delay", ACTION_DELAY_SECONDS), MAX_ACTION_DELAY_SECONDS)
                showdown_reveal = _clamp_seconds(data.get("showdown_reveal", SHOWDOWN_REVEAL_SECONDS), MAX_SHOWDOWN_REVEAL_SECONDS)
                if action_delay is None or showdown_reveal is None:
                    await websocket.send_json({"type": "error", "message": "set_speed values must be numbers of seconds"})
                    continue
                ACTION_DELAY_SECONDS, SHOWDOWN_REVEAL_SECONDS = action_delay, showdown_reveal
                await broadcast_to_spectators()
                continue
            if msg_type in ("pause", "resume"):
                if not is_observer:
                    await websocket.send_json({"type": "error", "message": "Only an instructor observer may pause or resume the game"})
                    continue
                # "resume" doubles as Start: it switches the table into
                # continuous play, dealing right away if the table is idle.
                paused = msg_type == "pause"
                if not paused:
                    running = True
                await broadcast_to_spectators()
                _schedule_next_hand()
                continue
            if msg_type == "ping":
                await websocket.send_json({"type": "pong"})
                continue
            await websocket.send_json({"type": "error", "message": "Unsupported message type"})
    except WebSocketDisconnect:
        pass
    finally:
        spectators.discard(websocket)
        if player_id and connections.get(player_id) is websocket:
            connections.pop(player_id, None)
            engine.remove_player(player_id)
            await broadcast_state()
