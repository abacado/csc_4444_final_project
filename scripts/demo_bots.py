"""Keep simple bots at a Bayou Poker table for manual UI testing.

By default the bots never start a hand. Open the browser and use the
instructor Start Hand button; each bot then calls a bet or checks when able.
Stop this program with Ctrl+C.
"""

import argparse
import asyncio
import json
import os

import websockets


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Connect passive Bayou Poker demo bots.")
    parser.add_argument("--url", default=os.environ.get("POKER_WS_URL", "ws://localhost:8000/ws"), help="Websocket endpoint")
    parser.add_argument("--count", type=int, default=3, help="Number of bots to seat (2-20)")
    parser.add_argument("--auto-start", action="store_true", help="Have the first bot start every ready hand")
    parser.add_argument("--delay", type=float, default=0.0, help="Seconds to wait before each bot action, so hands are watchable (default: 0)")
    args = parser.parse_args()
    if not 2 <= args.count <= 20:
        parser.error("--count must be between 2 and 20")
    return args


async def run_bot(player_id: str, name: str, url: str, auto_start: bool, delay: float) -> None:
    async with websockets.connect(url) as socket:
        await socket.send(json.dumps({"type": "join", "player_id": player_id, "name": name}))
        print(f"{name} connected")
        while True:
            message = json.loads(await socket.recv())
            if message["type"] == "error":
                print(f"{name} server error: {message['message']}")
                continue
            if message["type"] != "state":
                continue
            state = message["payload"]
            if auto_start and player_id == "demo-bot-1" and state["phase"] == "WAITING" and state["can_start"]:
                if delay:
                    await asyncio.sleep(delay)
                await socket.send(json.dumps({"type": "start_hand"}))
                print("Demo Bot 1 started a hand")
                continue
            if state.get("current_turn") != player_id:
                continue
            me = next(player for player in state["players"] if player["id"] == player_id)
            highest_bet = max(player["bet"] for player in state["players"])
            action = "call" if me["bet"] < highest_bet else "check"
            if delay:
                await asyncio.sleep(delay)
            await socket.send(json.dumps({"type": "action", "action": action}))
            print(f"{name}: {action}")


async def main() -> None:
    args = parse_args()
    print(f"Connecting {args.count} demo bots to {args.url}. Press Ctrl+C to stop.")
    if not args.auto_start:
        print("Open http://localhost:8000 and press Start Hand when the table is ready.")
    await asyncio.gather(*(
        run_bot(f"demo-bot-{number}", f"Demo Bot {number}", args.url, args.auto_start, args.delay)
        for number in range(1, args.count + 1)
    ))


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nDemo bots stopped.")
