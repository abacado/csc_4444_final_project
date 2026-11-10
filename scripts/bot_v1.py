import asyncio
import json
import argparse
from itertools import combinations
from collections import Counter
import websockets

# AI ITERATION 1: HEURISTIC SIMPLE REFLEX AGENT
# Concepts: Simple Reflex Agent (Lecture 16) + Heuristic h(n) (Lecture 17)

def evaluate_5card_hand(cards):
    """
    Evaluates a 5-card poker hand and returns a category score (0 to 8)
    and tie-breaker ranks.
    Categories:
    8: Straight Flush, 7: Four of a Kind, 6: Full House, 5: Flush,
    4: Straight, 3: Three of a Kind, 2: Two Pair, 1: One Pair, 0: High Card
    """
    ranks = sorted([c["rank"] for c in cards], reverse=True)
    suits = [c["suit"] for c in cards]
    is_flush = len(set(suits)) == 1

    # Check for straight (including Ace-low A-2-3-4-5)
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

    counts = Counter(ranks)
    # Sort by frequency (descending), then by card rank (descending)
    sorted_counts = sorted(counts.items(), key=lambda x: (x[1], x[0]), reverse=True)
    freq_pattern = [ count for rank, count in sorted_counts ]
    tie_breakers = [ rank for rank, count in sorted_counts ]

    if is_straight and is_flush:
        return (8, [straight_high])
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


def best_postflop_heuristic(hole_cards, community_cards):
    """
    Heuristic function h(s) for postflop states (Flop, Turn, River).
    Finds the best 5-card combination out of available cards and maps it
    to a normalized heuristic score between 0.0 and 1.0.
    """
    all_cards = hole_cards + community_cards
    best_eval = (-1, [])
    for combo in combinations(all_cards, 5):
        score = evaluate_5card_hand(combo)
        if score > best_eval:
            best_eval = score

    category, tie_breakers = best_eval
    top_rank_bonus = (tie_breakers[0] / 14.0) * 0.10  # Small bonus for high pair/kicker

    # Map category to heuristic value h(s)
    base_scores = {
        0: 0.15,  # High Card
        1: 0.45,  # One Pair
        2: 0.70,  # Two Pair
        3: 0.80,  # Three of a Kind
        4: 0.88,  # Straight
        5: 0.92,  # Flush
        6: 0.96,  # Full House
        7: 0.99,  # Four of a Kind
        8: 1.00   # Straight Flush
    }
    return min(1.0, base_scores.get(category, 0.15) + top_rank_bonus)


def preflop_chen_heuristic(hole_cards):
    """
    Heuristic function h(s) for preflop states based on Bill Chen's Formula
    (The Mathematics of Poker, 2006).
    Returns a normalized score between 0.0 and 1.0.
    """
    if len(hole_cards) < 2:
        return 0.0

    r1, r2 = hole_cards[0]["rank"], hole_cards[1]["rank"]
    s1, s2 = hole_cards[0]["suit"], hole_cards[1]["suit"]
    high, low = max(r1, r2), min(r1, r2)

    # 1. Score highest card
    rank_points = {14: 10, 13: 8, 12: 7, 11: 6}
    points = rank_points.get(high, high / 2.0)

    # 2. If pair, double the points (minimum 5)
    if high == low:
        points = max(5.0, points * 2.0)
    else:
        # 3. Suited bonus
        if s1 == s2:
            points += 2.0
        # 4. Connectedness (gap penalty)
        gap = high - low - 1
        if gap == 1:
            points -= 1.0
        elif gap == 2:
            points -= 2.0
        elif gap == 3:
            points -= 4.0
        elif gap >= 4:
            points -= 5.0

        # Straight bonus for small gaps with cards below Queen
        if gap <= 1 and high < 12:
            points += 1.0

    # Normalize Chen score (max is 20 for AA; typical playable hand is >= 6)
    return max(0.0, min(1.0, points / 15.0))


def choose_action(state, my_id):
    """
    Simple Reflex Decision Function:
    Perceives current state -> Computes Heuristic h(s) -> Applies Condition-Action Rules.
    """
    me = next(p for p in state["players"] if p["id"] == my_id)
    highest_bet = max(p["bet"] for p in state["players"])
    to_call = highest_bet - me["bet"]
    big_blind = state.get("big_blind", 20)
    phase = state.get("phase", "PREFLOP")

    hole_cards = me.get("hole_cards", [])
    community_cards = state.get("community_cards", [])

    # Ensure our hole cards are valid integers (not '??')
    if not hole_cards or not isinstance(hole_cards[0].get("rank"), int):
        return ("check", 0) if to_call == 0 else ("call", 0)

    # Compute Heuristic h(s)
    if phase == "PREFLOP" or len(community_cards) < 3:
        h_score = preflop_chen_heuristic(hole_cards)
    else:
        h_score = best_postflop_heuristic(hole_cards, community_cards)

    # Condition-Action Rules (Lecture 16 Slide 7)
    max_raise_target = me["bet"] + me["stack"]
    min_safe_raise = highest_bet + max(big_blind, to_call)

    # Rule 1: Very strong hand -> Raise for value
    if h_score >= 0.68 and max_raise_target > highest_bet:
        # Size raise to 2.5x the big blind or minimum safe raise
        desired_target = max(min_safe_raise, highest_bet + int(2.5 * big_blind))
        raise_target = min(desired_target, max_raise_target)
        # Only raise if we haven't already inflated the pot massively on a single street
        if highest_bet < big_blind * 6 or h_score >= 0.82:
            return "raise", raise_target

    # Rule 2: Playable hand -> Call reasonable bets or Check
    if h_score >= 0.42:
        # Protect stack against massive all-in shoves if we only have a marginal 1-pair hand
        if to_call > big_blind * 8 and h_score < 0.60:
            return ("check", 0) if to_call == 0 else ("fold", 0)
        return ("check", 0) if to_call == 0 else ("call", 0)

    # Rule 3: Weak hand -> Check if free, Call tiny bets with borderline cards, else Fold
    if to_call == 0:
        return "check", 0
    if to_call <= big_blind and h_score >= 0.35:
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
        busted_players = set()  # <--- NEW: Keeps track of who has already busted

        while True:
            message = json.loads(await websocket.recv())
            msg_type = message.get("type")

            if msg_type == "state":
                state = message["payload"]

                # <--- NEW: Check if any player hit $0 chips this hand
                for p in state["players"]:
                    if p["stack"] == 0 and not p.get("all_in", False) and p["id"] not in busted_players:
                        busted_players.add(p["id"])
                        print(f"[Hand {state['hand_number']}] {p['name']} ({p['id']}) was eliminated!")

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
    parser = argparse.ArgumentParser(description="Iteration 1: Heuristic Reflex Poker Bot")
    parser.add_argument("--id", default="bot-v1", help="Unique player_id for the bot")
    parser.add_argument("--name", default="Bot v1 (Heuristic)", help="Display name at table")
    parser.add_argument("--url", default="ws://localhost:8000/ws", help="Server websocket URL")
    args = parser.parse_args()

    asyncio.run(run_bot(args.url, args.id, args.name))