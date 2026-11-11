import asyncio
import json
import argparse
import random
from itertools import combinations
from collections import Counter
import websockets

#AI ITERATION 2: MONTE CARLO EXPECTED VALUE (EV) AGENT
#Concepts: Rational Utility Agent + Monte Carlo Rollouts

SUITS = ["hearts", "diamonds", "clubs", "spades"]
RANKS = list(range(2, 15))  # 2 through 14 (Ace)


def evaluate_5card_hand(cards):
    """
    Evaluates a 5-card poker hand and returns (category, tie_breakers).
    8: Straight Flush, 7: Quads, 6: Full House, 5: Flush,
    4: Straight, 3: Trips, 2: Two Pair, 1: One Pair, 0: High Card
    """
    ranks = sorted([c["rank"] for c in cards], reverse=True)
    suits = [c["suit"] for c in cards]
    is_flush = (suits[0] == suits[1] == suits[2] == suits[3] == suits[4])

    unique_ranks = sorted(set(ranks), reverse=True)
    is_straight = False
    straight_high = 0
    if len(unique_ranks) == 5:
        if unique_ranks[0] - unique_ranks[4] == 4:
            is_straight = True
            straight_high = unique_ranks[0]
        elif unique_ranks == [14, 5, 4, 3, 2]:
            is_straight = True
            straight_high = 5

    if is_straight and is_flush:
        return (8, [straight_high])

    counts = Counter(ranks)
    sorted_counts = sorted(counts.items(), key=lambda x: (x[1], x[0]), reverse=True)
    freq_pattern = [count for rank, count in sorted_counts]
    tie_breakers = [rank for rank, count in sorted_counts]

    if freq_pattern == [4, 1]:
        return (7, tie_breakers)
    if freq_pattern == [3, 2]:
        return (6, tie_breakers)
    if is_flush:
        return (5, ranks)
    if is_straight:
        return (4, [straight_high])
    if freq_pattern == [3, 1, 1]:
        return (3, tie_breakers)
    if freq_pattern == [2, 2, 1]:
        return (2, tie_breakers)
    if freq_pattern == [2, 1, 1, 1]:
        return (1, tie_breakers)
    return (0, ranks)


def evaluate_7card_hand(seven_cards):
    """Finds the best 5-card hand out of 7 cards."""
    best = (-1, [])
    for combo in combinations(seven_cards, 5):
        score = evaluate_5card_hand(combo)
        if score > best:
            best = score
    return best


def monte_carlo_win_probability(hole_cards, community_cards, num_opponents, num_simulations=250):
    """
    Runs Monte Carlo rollouts to estimate the probability of winning at showdown
    against `num_opponents` active players.
    """
    if num_opponents <= 0:
        return 1.0

    known_cards = {(c["rank"], c["suit"]) for c in (hole_cards + community_cards)}
    remaining_deck = [
        {"rank": r, "suit": s}
        for r in RANKS
        for s in SUITS
        if (r, s) not in known_cards
    ]

    cards_needed_for_board = 5 - len(community_cards)
    total_cards_to_sample = cards_needed_for_board + (2 * num_opponents)

    wins = 0.0
    for _ in range(num_simulations):
        sampled = random.sample(remaining_deck, total_cards_to_sample)
        sim_board = community_cards + sampled[:cards_needed_for_board]
        my_best = evaluate_7card_hand(hole_cards + sim_board)

        #Evaluate each simulated opponent's 7-card hand
        opp_cards_pool = sampled[cards_needed_for_board:]
        beaten = False
        tied = False
        for i in range(num_opponents):
            opp_hole = opp_cards_pool[2 * i : 2 * i + 2]
            opp_best = evaluate_7card_hand(opp_hole + sim_board)
            if opp_best > my_best:
                beaten = True
                break
            elif opp_best == my_best:
                tied = True

        if not beaten:
            wins += 0.5 if tied else 1.0

    return wins / num_simulations


def choose_action(state, my_id):
    """
    Rational Expected-Utility Decision Function:
    1. Runs Monte Carlo rollouts to estimate win probability P(win).
    2. Computes Pot Odds and Expected Value (EV).
    3. Selects action that maximizes expected chip utility.
    """
    players = state["players"]
    me = next(p for p in players if p["id"] == my_id)
    highest_bet = max(p["bet"] for p in players)
    to_call = highest_bet - me["bet"]
    pot = state.get("pot", 0)
    big_blind = state.get("big_blind", 20)

    hole_cards = me.get("hole_cards", [])
    community_cards = state.get("community_cards", [])

    if not hole_cards or not isinstance(hole_cards[0].get("rank"), int):
        return ("check", 0) if to_call == 0 else ("call", 0)

    #Count active opponents still in the hand (not folded, with chips or all-in)
    active_opponents = [
        p for p in players
        if p["id"] != my_id and not p.get("folded", False) and (p["stack"] > 0 or p["bet"] > 0 or p.get("all_in", False))
    ]
    num_opponents = max(1, len(active_opponents))
    total_active_players = num_opponents + 1

    #Estimate Win Probability via Monte Carlo Simulation
    win_prob = monte_carlo_win_probability(
        hole_cards, community_cards, num_opponents, num_simulations=250
    )

    #Fair share of the pot (e.g., 0.50 heads-up, 0.33 in 3-way pot)
    fair_equity = 1.0 / total_active_players

    #Compute Pot Odds
    pot_odds = to_call / (pot + to_call) if (pot + to_call) > 0 else 0.0

    max_raise_target = me["bet"] + me["stack"]
    min_safe_raise = highest_bet + max(big_blind, to_call)

    #Utility-Maximizing Action Selection
    #Strong equity advantage: Value Raise proportional to our edge
    if win_prob >= fair_equity + 0.22 and max_raise_target > highest_bet:
        if win_prob >= fair_equity + 0.38:
            # Monster equity: size up to ~75% of the pot
            desired_target = max(min_safe_raise, highest_bet + int(max(pot * 0.75, 3 * big_blind)))
        else:
            # Solid equity advantage: size to ~50% of the pot (or 2.5x BB)
            desired_target = max(min_safe_raise, highest_bet + int(max(pot * 0.50, 2.5 * big_blind)))

        raise_target = min(desired_target, max_raise_target)

        #Avoid infinitely re-raising without near-nut equity
        if highest_bet < big_blind * 8 or win_prob >= 0.78:
            return "raise", raise_target

    #Positive Expected Value (+EV) Call: Win probability exceeds Pot Odds
    #Add a small 3% safety margin for postflop reverse implied odds
    if to_call == 0:
        return "check", 0

    if win_prob >= (pot_odds + 0.03):
        # Protect stack against huge shoves if our absolute equity isn't strong
        if to_call > big_blind * 10 and win_prob < fair_equity + 0.12:
            return "fold", 0
        return "call", 0

    #Cheap speculative call preflop/flop if bet is tiny (<= 1 big blind)
    if to_call <= big_blind and win_prob >= (fair_equity - 0.08):
        return "call", 0

    return "fold", 0


async def run_bot(uri, my_id, my_name):
    async with websockets.connect(uri) as websocket:
        await websocket.send(json.dumps({
            "type": "join",
            "name": my_name,
            "player_id": my_id
        }))
        print(f"[{my_name}] Connected to {uri} as '{my_id}'")
        last_sent = None
        busted_players = set()

        while True:
            message = json.loads(await websocket.recv())
            msg_type = message.get("type")

            if msg_type == "state":
                state = message["payload"]

                # Log player eliminations and current chip counts
                for p in state["players"]:
                    if p["stack"] == 0 and not p.get("all_in", False) and p["id"] not in busted_players:
                        busted_players.add(p["id"])
                        stacks_summary = ", ".join(
                            f"{pl['name']}: ${pl['stack']}" for pl in state["players"]
                        )
                        print(f"[Hand {state['hand_number']}] {p['name']} ({p['id']}) was eliminated! | Stacks: {stacks_summary}")

                if state["current_turn"] == my_id:
                    action, amount = choose_action(state, my_id)
                    last_sent = action
                    payload = {"type": "action", "action": action}
                    if action == "raise":
                        payload["amount"] = amount
                    await websocket.send(json.dumps(payload))

            elif msg_type == "action_result":
                result = message["payload"]
                if not result["ok"]:
                    print(f"[{my_name}] Action Rejected ({last_sent}): {result['error']}")
                    if last_sent != "call":
                        last_sent = "call"
                        await websocket.send(json.dumps({"type": "action", "action": "call"}))

            elif msg_type == "error":
                print(f"[{my_name}] Server error:", message["message"])


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Iteration 2: Monte Carlo EV Poker Bot")
    parser.add_argument("--id", default="bot-v2", help="Unique player_id for the bot")
    parser.add_argument("--name", default="Bot v2 (Monte Carlo)", help="Display name at table")
    parser.add_argument("--url", default="ws://localhost:8000/ws", help="Server websocket URL")
    args = parser.parse_args()

    asyncio.run(run_bot(args.url, args.id, args.name))