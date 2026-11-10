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