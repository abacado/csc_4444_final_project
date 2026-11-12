import asyncio
import json
import subprocess
import time
import sys
from datetime import datetime
import websockets

MAX_HANDS_PER_GAME = 100
NUM_BOTS = 4  # Fair 1v1v1v1 showdown: Bot v1 vs Bot v2 vs Bot v3 vs Bot v4
LOG_FILE = "tournament_results.txt"

BOT_NAMES = [
    "Bot v1 (Heuristic)",
    "Bot v2 (Monte Carlo)",
    "Bot v3 (Adversarial)",
    "Bot v4 (RL + MCTS)",
]


def format_scoreboard(win_counts, total_games):
    """Creates a clean ASCII table showing Wins, Losses, and Win % for every bot."""
    lines = [
        f"\n================ LEADERBOARD AFTER {total_games} GAMES ================",
        f"{'Bot Name':<24} | {'Wins':>5} | {'Losses':>6} | {'Win Rate':>9}",
        "-" * 55,
    ]
    # Sort bots by most wins
    sorted_bots = sorted(BOT_NAMES, key=lambda b: win_counts.get(b, 0), reverse=True)
    for name in sorted_bots:
        wins = win_counts.get(name, 0)
        losses = total_games - wins
        win_rate = (wins / total_games * 100.0) if total_games > 0 else 0.0
        lines.append(f"{name:<24} | {wins:>5} | {losses:>6} | {win_rate:>8.1f}%")
    lines.append("=" * 55)
    return "\n".join(lines)


def log_to_file(text):
    """Appends tournament results to tournament_results.txt so you never lose data."""
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(text + "\n")


async def monitor_and_control_tournament(game_num, uri):
    for attempt in range(25):
        try:
            async with websockets.connect(uri) as ws:
                await ws.send(json.dumps({"type": "observe"}))
                await ws.send(json.dumps({
                    "type": "set_speed",
                    "action_delay": 0.0,
                    "showdown_reveal": 0.0
                }))

                started = False
                last_printed_hand = -1

                while True:
                    try:
                        raw_msg = await asyncio.wait_for(ws.recv(), timeout=1.5)
                    except asyncio.TimeoutError:
                        await ws.send(json.dumps({"type": "observe"}))
                        await ws.send(json.dumps({"type": "resume"}))
                        await ws.send(json.dumps({"type": "start_hand"}))
                        continue

                    msg = json.loads(raw_msg)
                    if msg.get("type") != "state":
                        continue

                    state = msg["payload"]
                    players = state.get("players", [])
                    phase = state.get("phase", "WAITING")
                    hand_num = state.get("hand_number", 0)

                    if not started and len(players) >= NUM_BOTS:
                        await ws.send(json.dumps({"type": "resume"}))
                        await ws.send(json.dumps({"type": "start_hand"}))
                        started = True

                    players_with_stack = [p for p in players if p.get("stack", 0) > 0]
                    alive_players = [
                        p for p in players
                        if p.get("stack", 0) > 0 or p.get("bet", 0) > 0 or p.get("all_in", False)
                    ]

                    # Overwrite the same terminal line (\r) so hands don't flood your scrollback!
                    if started and hand_num > last_printed_hand:
                        last_printed_hand = hand_num
                        stacks_str = " | ".join(
                            f"{p['name']}: ${p['stack'] + p.get('bet', 0)}" for p in players
                        )
                        status_line = f"\r[Game #{game_num:02d} - Hand {hand_num:03d}] ({len(alive_players)} alive) -> {stacks_str}    "
                        sys.stdout.write(status_line)
                        sys.stdout.flush()

                    if started and hand_num > 0 and phase == "WAITING":
                        if len(players_with_stack) <= 1 or hand_num >= MAX_HANDS_PER_GAME:
                            leader = max(players, key=lambda p: p.get("stack", 0))
                            winner = leader["name"]
                            reason = (
                                f"in {hand_num} hands"
                                if len(players_with_stack) <= 1
                                else f"by Chip Lead (${leader['stack']}) at Hand {hand_num} cap"
                            )
                            sys.stdout.write("\n")  # Move to a fresh line when the game ends
                            return winner, reason

        except asyncio.CancelledError:
            raise
        except Exception:
            await asyncio.sleep(0.3)

    raise RuntimeError("Could not connect to server after multiple attempts.")


def force_kill_process_tree(proc):
    if proc is None:
        return
    try:
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
        else:
            proc.kill()
    except Exception:
        pass


def cleanup_processes(server, bots):
    for b in bots:
        force_kill_process_tree(b)
    force_kill_process_tree(server)


def main():
    py = sys.executable
    game_num = 0
    win_counts = {name: 0 for name in BOT_NAMES}

    # Write a session header to tournament_results.txt
    log_to_file(f"\n--- NEW BENCHMARK SESSION STARTED AT {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ---")

    try:
        while True:
            game_num += 1
            port = 8100 + (game_num % 50)
            ws_url = f"ws://localhost:{port}/ws"

            fast_server_cmd = (
                "import uvicorn, app.server; "
                "app.server.MIN_HAND_GAP_SECONDS = 0.01; "
                "app.server.SHOWDOWN_REVEAL_SECONDS = 0.0; "
                f"uvicorn.run(app.server.app, host='127.0.0.1', port={port}, log_level='error')"
            )

            server = subprocess.Popen(
                [py, "-c", fast_server_cmd],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
            time.sleep(1.2)

            bots = [
                subprocess.Popen([py, "-u", "scripts/bot_v1.py", "--id", "v1", "--name", "Bot v1 (Heuristic)", "--url", ws_url], stdout=subprocess.DEVNULL),
                subprocess.Popen([py, "-u", "scripts/bot_v2.py", "--id", "v2", "--name", "Bot v2 (Monte Carlo)", "--url", ws_url], stdout=subprocess.DEVNULL),
                subprocess.Popen([py, "-u", "scripts/bot_v3.py", "--id", "v3", "--name", "Bot v3 (Adversarial)", "--url", ws_url], stdout=subprocess.DEVNULL),
                subprocess.Popen([py, "-u", "scripts/bot_v4_rl.py", "--id", "v4", "--name", "Bot v4 (RL + MCTS)", "--url", ws_url], stdout=subprocess.DEVNULL),
            ]

            try:
                start_time = time.time()
                winner, reason = asyncio.run(monitor_and_control_tournament(game_num, ws_url))
                elapsed = time.time() - start_time
                win_counts[winner] = win_counts.get(winner, 0) + 1

                summary_line = f"[Game #{game_num:02d} Done in {elapsed:4.1f}s] Winner: {winner} ({reason})"
                board_str = format_scoreboard(win_counts, game_num)

                print(summary_line)
                print(board_str)

                # Save both the game result and the latest leaderboard to tournament_results.txt
                log_to_file(summary_line)
                log_to_file(board_str)

            except KeyboardInterrupt:
                print("\n[Auto-Trainer] Stopping training loop cleanly...")
                cleanup_processes(server, bots)
                break
            except Exception as e:
                print(f"\n[Auto-Trainer] Connection interrupted ({e}), restarting...")
            finally:
                cleanup_processes(server, bots)
                time.sleep(0.4)

    except KeyboardInterrupt:
        pass
    finally:
        if game_num > 0:
            print(format_scoreboard(win_counts, game_num))
            print(f"\nFull history saved to: {LOG_FILE}")


if __name__ == "__main__":
    main()