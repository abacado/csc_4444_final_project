import json
from pathlib import Path
from typing import Dict

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
SERIES_HANDS = 10
series_total: int | None = None
series_completed = 0
series_active = False


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
    series = None
    if series_total is not None:
        series = {
            "total_hands": series_total,
            "completed_hands": series_completed,
            "active": series_active,
        }
    return {"can_start": engine.can_start_hand(), "series": series}


def _advance_series_if_ready() -> None:
    """Deal the next scheduled hand only after the preceding pot is paid."""
    global series_active, series_completed
    if not series_active or engine.phase != "WAITING" or engine.last_action is None:
        return
    if engine.last_action.get("type") != "payout":
        return
    series_completed += 1
    if series_completed < series_total and engine.can_start_hand():
        engine.start_hand()
    else:
        series_active = False


async def broadcast_state() -> None:
    """Send every bot its own private cards and observers only the public table."""
    _advance_series_if_ready()
    controls = _table_controls()
    for pid, websocket in list(connections.items()):
        payload = engine.snapshot(pid)
        payload.update(controls)
        if not await _send(websocket, payload):
            connections.pop(pid, None)
            engine.remove_player(pid)
    public = engine.snapshot()
    public.update(controls)
    for websocket in list(spectators):
        if not await _send(websocket, public):
            spectators.discard(websocket)


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    global series_active, series_completed, series_total
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
                await broadcast_state()
                continue
            if msg_type == "start_hand":
                # The browser uses an observer connection as the instructor
                # console. It can start a ready table but never becomes a seat.
                if player_id is None and not is_observer:
                    await websocket.send_json({"type": "error", "message": "Join as a bot or observer first"})
                    continue
                if not engine.can_start_hand():
                    await websocket.send_json({"type": "error", "message": "Need at least two connected players with chips to start"})
                    continue
                series_total = None
                series_completed = 0
                series_active = False
                engine.start_hand()
                await broadcast_state()
                continue
            if msg_type == "start_series":
                if not is_observer:
                    await websocket.send_json({"type": "error", "message": "Only an instructor observer may start a series"})
                    continue
                if not engine.can_start_hand():
                    await websocket.send_json({"type": "error", "message": "Need at least two connected players with chips to start"})
                    continue
                series_total = SERIES_HANDS
                series_completed = 0
                series_active = True
                engine.start_hand()
                await broadcast_state()
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
