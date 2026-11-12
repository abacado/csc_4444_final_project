import asyncio
import json
import argparse
import random
from itertools import combinations
from collections import Counter, defaultdict
import websockets

#AI ITERATION 3: ADVERSARIAL & OPPONENT MODELING BOT
#Notes: Assumes opponents aren't completely random. Tracks how often 
#everyone folds/raises to build profiles, and calculates Expectimax EV 
#to occasionally bluff tight players or avoid calling station traps.

SUITS = ["hearts", "diamonds", "clubs", "spades"]
RANKS = list(range(2, 15))


class OpponentTracker:
    """
    Watches server updates across hands to memorize opponent tendencies.
    """
    def __init__(self, my_id):
        self.my_id = my_id
        self.stats = defaultdict(lambda: {
            "actions": 0, "folds": 0, "calls": 0,
            "checks": 0, "raises": 0, "faced_bet": 0,
            "folded_to_bet": 0, "showdown_strengths": []
        })
        self.current_hand_number = -1
        self.last_recorded_event = None
        self.hand_aggressors = set()

    def update(self, state):
        hand_num = state.get("hand_number", 0)
        phase = state.get("phase", "WAITING")

        if hand_num != self.current_hand_number:
            self.current_hand_number = hand_num
            self.last_recorded_event = None
            self.hand_aggressors.clear()

        last_act = state.get("last_action")
        if last_act and last_act != self.last_recorded_event:
            self.last_recorded_event = last_act
            act_type = last_act.get("type")
            pid = last_act.get("player")

            if pid and pid != self.my_id and act_type in ("fold", "check", "call", "raise"):
                s = self.stats[pid]
                s["actions"] += 1
                if act_type == "fold":
                    s["folds"] += 1
                    s["faced_bet"] += 1
                    s["folded_to_bet"] += 1
                elif act_type == "check":
                    s["checks"] += 1
                elif act_type == "call":
                    s["calls"] += 1
                    s["faced_bet"] += 1
                elif act_type == "raise":
                    s["raises"] += 1
                    s["faced_bet"] += 1
                    self.hand_aggressors.add(pid)

    def get_fold_prob(self, pid):
        #Uses Laplace smoothing so we assume 30% fold rate before we have data
        s = self.stats[pid]
        return (s["folded_to_bet"] + 3.0) / (s["faced_bet"] + 10.0)

    def get_aggression_rate(self, pid):
        s = self.stats[pid]
        return (s["raises"] + 1.5) / (s["actions"] + 10.0)


def preflop_chen_score(hole_cards):
    if len(hole_cards) < 2:
        return 0.0
    r1, r2 = hole_cards[0]["rank"], hole_cards[1]["rank"]
    s1, s2 = hole_cards[0]["suit"], hole_cards[1]["suit"]
    high, low = max(r1, r2), min(r1, r2)

    rank_points = {14: 10, 13: 8, 12: 7, 11: 6}
    points = rank_points.get(high, high / 2.0)

    if high == low:
        points = max(5.0, points * 2.0)
    else:
        if s1 == s2:
            points += 2.0
        gap = high - low - 1
        if gap == 1:
            points -= 1.0
        elif gap == 2:
            points -= 2.0
        elif gap == 3:
            points -= 4.0
        elif gap >= 4:
            points -= 5.0
        if gap <= 1 and high < 12:
            points += 1.0

    return max(0.0, min(1.0, points / 15.0))


def evaluate_5card_hand(cards):
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
    best = (-1, [])
    for combo in combinations(seven_cards, 5):
        score = evaluate_5card_hand(combo)
        if score > best:
            best = score
    return best


def range_weighted_monte_carlo(hole_cards, community_cards, active_opponents, tracker, num_simulations=260):
    """
    Monte Carlo that respects opponent betting history.
    If an opponent raised this hand, we re-draw their simulated cards
    if they are dealt trash, effectively testing our hand against a stronger range.
    """
    num_opponents = len(active_opponents)
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
    wins = 0.0

    for _ in range(num_simulations):
        deck_copy = remaining_deck[:]
        random.shuffle(deck_copy)

        sim_board = community_cards + deck_copy[:cards_needed_for_board]
        pool_idx = cards_needed_for_board
        my_best = evaluate_7card_hand(hole_cards + sim_board)

        beaten = False
        tied = False

        for opp in active_opponents:
            oid = opp["id"]
            opp_hole = deck_copy[pool_idx : pool_idx + 2]
            pool_idx += 2

            #If this player raised earlier in the hand, discard trash cards
            if oid in tracker.hand_aggressors and pool_idx + 2 <= len(deck_copy):
                if preflop_chen_score(opp_hole) < 0.38:
                    opp_hole = deck_copy[pool_idx : pool_idx + 2]
                    pool_idx += 2

            opp_best = evaluate_7card_hand(opp_hole + sim_board)
            if opp_best > my_best:
                beaten = True
                break
            elif opp_best == my_best:
                tied = True

        if not beaten:
            wins += 0.5 if tied else 1.0

    return wins / num_simulations


def choose_action(state, my_id, tracker):
    players = state["players"]
    me = next(p for p in players if p["id"] == my_id)
    highest_bet = max(p["bet"] for p in players)
    to_call = highest_bet - me["bet"]
    pot = state.get("pot", 0)
    big_blind = state.get("big_blind", 20)
    phase = state.get("phase", "PREFLOP")

    hole_cards = me.get("hole_cards", [])
    community_cards = state.get("community_cards", [])

    if not hole_cards or not isinstance(hole_cards[0].get("rank"), int):
        return ("check", 0) if to_call == 0 else ("call", 0)

    active_opponents = [
        p for p in players
        if p["id"] != my_id and not p.get("folded", False) and (p["stack"] > 0 or p["bet"] > 0 or p.get("all_in", False))
    ]
    if not active_opponents:
        return ("check", 0) if to_call == 0 else ("call", 0)

    #1. Run simulation to get our raw win rate
    win_prob = range_weighted_monte_carlo(
        hole_cards, community_cards, active_opponents, tracker, num_simulations=260
    )

    #Boost equity slightly if we act last
    if state.get("dealer") == my_id:
        win_prob = min(1.0, win_prob + 0.025)

    #2. Check the tracker to see if the remaining opponents fold often
    collective_fold_prob = 1.0
    for opp in active_opponents:
        oid = opp["id"]
        collective_fold_prob *= tracker.get_fold_prob(oid)

    #People who put a big bet in rarely fold
    if highest_bet > big_blind * 2:
        collective_fold_prob *= 0.45

    #3. Calculate Expected Value (EV) for calling
    ev_call = (win_prob * (pot + to_call)) - to_call

    max_raise_target = me["bet"] + me["stack"]
    min_safe_raise = highest_bet + max(big_blind, to_call)

    best_raise_ev = -float("inf")
    best_raise_target = min_safe_raise

    #4. Check EV for Raising (allows bluffs if fold probability is very high)
    if max_raise_target > highest_bet and (highest_bet < big_blind * 8 or win_prob >= 0.72):
        candidate_targets = [
            max(min_safe_raise, highest_bet + int(max(pot * 0.60, 2.5 * big_blind))),
            max(min_safe_raise, highest_bet + int(max(pot * 1.00, 3.5 * big_blind)))
        ]

        for raw_target in candidate_targets:
            target = min(raw_target, max_raise_target)
            added_chips = target - me["bet"]
            opp_call_amount = target - highest_bet

            size_leverage = min(1.30, 1.0 + (opp_call_amount / max(1, pot)) * 0.25)
            p_fold = min(0.75, collective_fold_prob * size_leverage)

            #Only bluff if our hand isn't completely dead
            effective_p_fold = p_fold if win_prob >= 0.34 else 0.0

            future_pot_if_called = pot + added_chips + opp_call_amount
            ev_if_called = (win_prob * future_pot_if_called) - added_chips

            ev_raise = (effective_p_fold * pot) + ((1.0 - effective_p_fold) * ev_if_called)

            if ev_raise > best_raise_ev:
                best_raise_ev = ev_raise
                best_raise_target = target

    #5. Make the decision
    fair_equity = 1.0 / (len(active_opponents) + 1)

    #Raise if EV(raise) is better than calling and we meet our minimum standards
    if best_raise_ev > ev_call and best_raise_ev > big_blind * 0.35:
        if win_prob >= fair_equity + 0.14 or (collective_fold_prob >= 0.42 and win_prob >= 0.38 and to_call <= big_blind * 2):
            return "raise", best_raise_target

    if to_call == 0:
        return "check", 0

    #Call if EV is positive, with a risk buffer against huge shoves
    risk_buffer = big_blind * 0.20 if to_call <= big_blind * 4 else to_call * 0.08
    if ev_call >= risk_buffer:
        return "call", 0

    if phase == "PREFLOP" and to_call <= big_blind and win_prob >= fair_equity - 0.06:
        return "call", 0

    return "fold", 0


async def run_bot(uri, my_id, my_name):
    tracker = OpponentTracker(my_id)

    async with websockets.connect(uri) as websocket:
        await websocket.send(json.dumps({
            "type": "join",
            "name": my_name,
            "player_id": my_id
        }))
        print(f"[{my_name}] Connected to {uri} as '{my_id}'")
        last_sent = None

        while True:
            message = json.loads(await websocket.recv())
            msg_type = message.get("type")

            if msg_type == "state":
                state = message["payload"]
                tracker.update(state)

                if state["current_turn"] == my_id:
                    action, amount = choose_action(state, my_id, tracker)
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
    parser = argparse.ArgumentParser(description="Iteration 3: Adversarial & Opponent-Modeling Poker Bot")
    parser.add_argument("--id", default="bot-v3", help="Unique player_id for the bot")
    parser.add_argument("--name", default="Bot v3 (Adversarial)", help="Display name at table")
    parser.add_argument("--url", default="ws://localhost:8000/ws", help="Server websocket URL")
    args = parser.parse_args()

    asyncio.run(run_bot(args.url, args.id, args.name))