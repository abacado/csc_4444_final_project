import asyncio
import json
import argparse
import random
import time
from itertools import combinations
from collections import Counter, defaultdict
import websockets

# Standard deck setup for the Monte Carlo rollouts
SUITS = ["hearts", "diamonds", "clubs", "spades"]
RANKS = list(range(2, 15))  # 2 through 14 (11=J, 12=Q, 13=K, 14=A)


def raw_chen_score(hole_cards):
    """Calculates Bill Chen's raw preflop score for two hole cards."""
    if len(hole_cards) < 2:
        return 0.0
    r1, r2 = hole_cards[0]["rank"], hole_cards[1]["rank"]
    s1, s2 = hole_cards[0]["suit"], hole_cards[1]["suit"]
    high, low = max(r1, r2), min(r1, r2)

    points = {14: 10, 13: 8, 12: 7, 11: 6}.get(high, high / 2.0)
    if high == low:
        return max(5.0, points * 2.0) + (1.0 if high >= 8 else 0.0)

    gap = high - low - 1
    return (
        points
        + (2.0 if s1 == s2 else 0.0)
        - (0, 1, 2, 4, 5)[min(gap, 4)]
        + (1.0 if gap <= 1 and high < 12 else 0.0)
    )


# Precompute exact preflop strength percentiles (0.0 to 1.0) across all 1,326 2-card combos
_FULL_DECK = [{"rank": r, "suit": s} for r in RANKS for s in SUITS]
_SORTED_SCORES = sorted(raw_chen_score(c) for c in combinations(_FULL_DECK, 2))
PREFLOP_PERCENTILE = {score: (i + 1) / len(_SORTED_SCORES) for i, score in enumerate(_SORTED_SCORES)}


def preflop_percentile(hole_cards):
    """Returns the exact percentile (0.0 to 1.0) of our 2-card starting hand."""
    if len(hole_cards) < 2:
        return 0.0
    return PREFLOP_PERCENTILE.get(raw_chen_score(hole_cards), 0.5)


class InMemoryBanditLearner:
    """
    Lightweight Q-learning bandit for picking bet sizes.
    We keep this entirely in memory so the script is 100% self-contained for grading.
    """
    def __init__(self, alpha=0.15):
        self.alpha = alpha
        self.q_table = defaultdict(lambda: {"raise_small": 0.05, "raise_pot": 0.05, "raise_overbet": 0.0})
        self._init_warm_start()
        self.trajectory = []
        self.hand_start_chips = None
        self.current_hand = -1

    def _init_warm_start(self):
        # Pre-seed the table with common-sense poker sizing so the bot plays well from Hand 1
        for phase in ("PREFLOP", "FLOP", "TURN", "RIVER"):
            for arch in ("TAG", "MANIAC", "STATION", "NIT"):
                self.q_table[f"{phase}|EQ4|STATION"] = {"raise_small": 0.0, "raise_pot": 0.15, "raise_overbet": 0.30}
                self.q_table[f"{phase}|EQ4|{arch}"] = {"raise_small": 0.05, "raise_pot": 0.20, "raise_overbet": 0.15}
                self.q_table[f"{phase}|EQ3|{arch}"] = {"raise_small": 0.15, "raise_pot": 0.10, "raise_overbet": -0.15}

    def get_state_key(self, phase, win_prob, opp_archetype):
        eq_bucket = min(4, int(win_prob * 5))
        return f"{phase}|EQ{eq_bucket}|{opp_archetype}"

    def record_step(self, state_key, arm_key):
        self.trajectory.append((state_key, arm_key))

    def end_hand_update(self, final_chips, big_blind):
        if not self.trajectory or self.hand_start_chips is None:
            self.trajectory.clear()
            return

        net_bb = (final_chips - self.hand_start_chips) / max(1.0, float(big_blind))
        reward = max(-6.0, min(6.0, net_bb))

        for s_key, a_key in self.trajectory:
            old_q = self.q_table[s_key][a_key]
            new_q = old_q + self.alpha * ((reward * 0.05) - old_q)
            self.q_table[s_key][a_key] = max(-0.35, min(0.35, new_q))

        self.trajectory.clear()

    def get_bonus(self, state_key, arm_key):
        return self.q_table[state_key].get(arm_key, 0.0)


class OpponentTracker:
    """
    Tracks how each player bets across hands.
    Ignores routine 1 BB preflop folds so tight bots aren't mistaken for postflop folders.
    """
    def __init__(self, my_id):
        self.my_id = my_id
        self.stats = defaultdict(lambda: {
            "actions": 0, "raises": 0,
            "faced_real_bet": 0, "folded_to_real_bet": 0,
            "showdown_bluffs": 0
        })
        self.current_hand_number = -1
        self.last_recorded_event = None
        self.hand_aggressors = {}  # maps player_id -> (preflop_strength, raised_postflop)
        self.recorded_showdown_hand = -1

    def update(self, state):
        hand_num = state.get("hand_number", 0)
        phase = state.get("phase", "WAITING")
        big_blind = max(1, state.get("big_blind", 20))
        pot = state.get("pot", 0)
        players = state.get("players", [])
        highest_bet = max((pl.get("bet", 0) for pl in players), default=0)

        if hand_num != self.current_hand_number:
            self.current_hand_number = hand_num
            self.last_recorded_event = None
            self.hand_aggressors.clear()

        # At showdown, check if any raiser turned over weak cards (bottom 45% preflop hand)
        if phase == "SHOWDOWN" and self.recorded_showdown_hand != hand_num:
            self.recorded_showdown_hand = hand_num
            for p in players:
                pid = p.get("id")
                cards = p.get("hole_cards", [])
                if pid != self.my_id and len(cards) == 2 and isinstance(cards[0].get("rank"), int):
                    if pid in self.hand_aggressors and preflop_percentile(cards) < 0.45:
                        self.stats[pid]["showdown_bluffs"] += 1

        last_act = state.get("last_action")
        if last_act and last_act != self.last_recorded_event:
            self.last_recorded_event = last_act
            act_type = last_act.get("type")
            pid = last_act.get("player")

            if pid and pid != self.my_id and act_type in ("fold", "check", "call", "raise"):
                s = self.stats[pid]
                s["actions"] += 1
                is_real_bet = (phase != "PREFLOP") or (highest_bet > big_blind)

                if act_type == "fold" and is_real_bet:
                    s["faced_real_bet"] += 1
                    s["folded_to_real_bet"] += 1
                elif act_type == "call" and is_real_bet:
                    s["faced_real_bet"] += 1
                elif act_type == "raise":
                    if is_real_bet:
                        s["faced_real_bet"] += 1
                    target = last_act.get("target") or last_act.get("amount") or highest_bet
                    # Catch 2.0x BB opens while ignoring 1 BB min-clicks
                    if target >= big_blind * 2.0 or target >= pot * 0.25:
                        s["raises"] += 1
                        agg_rate = (s["raises"] + 2.0) / (s["actions"] + 12.0)
                        pf_floor = max(0.45, min(0.75, 0.85 - 1.2 * agg_rate))
                        old_pf, old_post = self.hand_aggressors.get(pid, (0.0, False))
                        self.hand_aggressors[pid] = (
                            max(old_pf, pf_floor),
                            old_post or (phase in ("FLOP", "TURN", "RIVER"))
                        )

    def get_real_fold_prob(self, pid):
        s = self.stats[pid]
        return (s["folded_to_real_bet"] + 2.0) / (s["faced_real_bet"] + 8.0)

    def get_archetype(self, pid):
        s = self.stats[pid]
        if s["showdown_bluffs"] >= 1:
            return "MANIAC"
        if s["actions"] < 6:
            return "TAG"

        fold_p = self.get_real_fold_prob(pid)
        agg_r = (s["raises"] + 1.5) / (s["actions"] + 10.0)

        if fold_p < 0.30 and agg_r < 0.22:
            return "STATION"
        if agg_r >= 0.28:
            return "MANIAC"
        if fold_p >= 0.50:
            return "NIT"
        return "TAG"

    def summarize_opponents(self, active_opponents):
        collective_fold = 1.0
        has_station = False
        has_maniac = False
        primary_arch = "TAG"

        for opp in active_opponents:
            oid = opp["id"]
            collective_fold *= self.get_real_fold_prob(oid)
            arch = self.get_archetype(oid)
            if arch == "STATION":
                has_station = True
                primary_arch = "STATION"
            elif arch == "MANIAC":
                has_maniac = True
                if primary_arch != "STATION":
                    primary_arch = "MANIAC"
            elif arch == "NIT" and primary_arch == "TAG":
                primary_arch = "NIT"

        return collective_fold, has_station, has_maniac, primary_arch


# --- HAND EVALUATION & BOARD HELPERS ---

def get_board_wetness(community_cards):
    """Scores board from 0.0 (dry) to 1.0 (wet/draw-heavy)."""
    if len(community_cards) < 3:
        return 0.0
    ranks = sorted([c["rank"] for c in community_cards], reverse=True)
    max_suit = max(Counter(c["suit"] for c in community_cards).values())
    unique = sorted(set(ranks))

    straight_draw = any(unique[i + 2] - unique[i] <= 4 for i in range(len(unique) - 2))
    wetness = (0.45 if max_suit >= 3 else (0.15 if max_suit == 2 else 0.0))
    if straight_draw:
        wetness += 0.35
    return min(1.0, wetness)


def evaluate_5card_fast(cards):
    """Returns a comparable tuple (category, tiebreakers...) for 5 cards."""
    r0, r1, r2, r3, r4 = sorted((c["rank"] for c in cards), reverse=True)
    s0, s1, s2, s3, s4 = (c["suit"] for c in cards)
    is_flush = (s0 == s1 == s2 == s3 == s4)

    if r0 > r1 > r2 > r3 > r4:
        is_straight = (r0 - r4 == 4) or (r0 == 14 and r1 == 5)
        if is_straight:
            sh = 5 if (r0 == 14 and r1 == 5) else r0
            return (8 if is_flush else 4, sh)
        return (5 if is_flush else 0, r0, r1, r2, r3, r4)

    counts = Counter((r0, r1, r2, r3, r4))
    sc = sorted(counts.items(), key=lambda x: (x[1], x[0]), reverse=True)
    f0, f1 = sc[0][1], sc[1][1]
    tb = tuple(r for r, _ in sc)

    if f0 == 4:
        return (7,) + tb
    if f0 == 3 and f1 == 2:
        return (6,) + tb
    if is_flush:
        return (5, r0, r1, r2, r3, r4)
    if f0 == 3:
        return (3,) + tb
    if f0 == 2 and f1 == 2:
        return (2,) + tb
    return (1,) + tb


def evaluate_7card_fast(seven_cards):
    return max(evaluate_5card_fast(combo) for combo in combinations(seven_cards, 5))


def is_late_position(state, my_id):
    """True if we are on the dealer button or Cutoff."""
    if state.get("dealer") == my_id:
        return True
    active = [p["id"] for p in state["players"] if p.get("stack", 0) > 0 or p.get("bet", 0) > 0]
    if my_id not in active or len(active) <= 2:
        return False
    dealer_idx = active.index(state["dealer"]) if state.get("dealer") in active else 0
    rel_seat = (active.index(my_id) - dealer_idx - 1) % len(active)
    return (rel_seat / (len(active) - 1)) >= 0.65


# --- MONTE CARLO SIMULATION & DECISION ENGINE ---

def monte_carlo_equity(hole_cards, community_cards, active_opponents, tracker, num_simulations=400):
    """
    Runs rollouts to estimate win probability.
    Weights opponent hands by both their preflop raise percentile and postflop board hits.
    """
    if not active_opponents:
        return 1.0

    known = {(c["rank"], c["suit"]) for c in (hole_cards + community_cards)}
    deck = [{"rank": r, "suit": s} for r in RANKS for s in SUITS if (r, s) not in known]
    needed_board = 5 - len(community_cards)
    has_board = len(community_cards) >= 3

    opp_ranges = []
    for opp in active_opponents:
        oid = opp["id"]
        if oid in tracker.hand_aggressors and tracker.get_archetype(oid) != "MANIAC":
            opp_ranges.append(tracker.hand_aggressors[oid])
        else:
            opp_ranges.append((0.0, False))

    wins = 0.0
    completed = 0
    start_time = time.perf_counter()

    for _ in range(num_simulations):
        if completed >= 60 and (time.perf_counter() - start_time) > 3.5:
            break
        completed += 1

        sim_deck = deck[:]
        random.shuffle(sim_deck)
        sim_board = community_cards + sim_deck[:needed_board]
        idx = needed_board
        my_best = evaluate_7card_fast(hole_cards + sim_board)

        beaten, tied = False, False
        for pf_floor, raised_postflop in opp_ranges:
            opp_hole = sim_deck[idx : idx + 2]
            idx += 2

            # If this player raised preflop, reject weak starting hands
            if pf_floor > 0.0 and idx + 2 <= len(sim_deck):
                if preflop_percentile(opp_hole) < pf_floor:
                    opp_hole = sim_deck[idx : idx + 2]
                    idx += 2

            # If they raised on the flop/turn/river, pick the better of 2 hands on the current board
            if has_board and raised_postflop and idx + 2 <= len(sim_deck) and random.random() < 0.70:
                alt_hole = sim_deck[idx : idx + 2]
                idx += 2
                if evaluate_7card_fast(alt_hole + community_cards) > evaluate_7card_fast(opp_hole + community_cards):
                    opp_hole = alt_hole

            opp_best = evaluate_7card_fast(opp_hole + sim_board)
            if opp_best > my_best:
                beaten = True
                break
            elif opp_best == my_best:
                tied = True

        if not beaten:
            wins += 0.5 if tied else 1.0

    return wins / max(1, completed)


def pick_raise_amount(phase, win_prob, fair_equity, pot, big_blind, highest_bet,
                      min_safe_raise, max_target, board_wetness, has_station, state_key, bandit):
    """Uses the Bandit learner + board wetness to pick between small, pot, and overbet sizing."""
    candidates = [
        ("raise_small", max(min_safe_raise, highest_bet + int(max(pot * 0.55, 2.2 * big_blind)))),
        ("raise_pot", max(min_safe_raise, highest_bet + int(max(pot * 0.78, 3.0 * big_blind)))),
    ]
    if win_prob >= fair_equity + 0.34 or (has_station and win_prob >= 0.70):
        candidates.append(
            ("raise_overbet", max(min_safe_raise, highest_bet + int(max(pot * 1.15, 4.0 * big_blind))))
        )

    best_arm = "raise_small"
    best_target = min(candidates[0][1], max_target)
    best_score = -999.0

    for arm_key, raw_target in candidates:
        target = min(raw_target, max_target)
        base_pref = 0.20 if (arm_key == "raise_pot" and win_prob >= fair_equity + 0.28) else 0.10
        if arm_key == "raise_overbet" and win_prob >= 0.78:
            base_pref = 0.25

        if phase in ("FLOP", "TURN"):
            if board_wetness >= 0.50 and arm_key == "raise_pot":
                base_pref += 0.08
            elif board_wetness <= 0.20 and arm_key == "raise_small":
                base_pref += 0.08

        score = base_pref + bandit.get_bonus(state_key, arm_key)
        if score > best_score:
            best_score = score
            best_arm = arm_key
            best_target = target

    bandit.record_step(state_key, best_arm)
    return best_target


def choose_action(state, my_id, tracker, bandit):
    """
    Main decision logic:
    - Preflop: Uses exact 1,326-combo hand percentiles + position.
    - Postflop: Uses 450-rollout range-weighted Monte Carlo, tighter heads-up pressure, and Bandit sizing.
    """
    players = state["players"]
    me = next(p for p in players if p["id"] == my_id)
    highest_bet = max(p["bet"] for p in players)
    to_call = max(0, highest_bet - me["bet"])
    price = min(me["stack"], to_call)
    pot = max(1, state.get("pot", 0))
    big_blind = max(1, state.get("big_blind", 20))
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

    heads_up = (len(active_opponents) == 1)
    late_pos = is_late_position(state, my_id)

    # Calculate true pot odds (subtracting any uncallable all-in excess)
    covered_bet = me["bet"] + price
    uncalled_excess = sum(max(0, p.get("bet", 0) - covered_bet) for p in active_opponents)
    eligible_pot = max(1, pot - uncalled_excess)
    pot_odds = price / (eligible_pot + price) if price > 0 else 0.0

    max_my_target = me["bet"] + me["stack"]
    max_opp_target = max((p.get("bet", 0) + p.get("stack", 0) for p in active_opponents), default=max_my_target)
    min_safe_raise = highest_bet + max(big_blind, to_call)
    max_target = max(min_safe_raise, min(max_my_target, max_opp_target))
    can_raise = (max_my_target > highest_bet) and (max_opp_target > highest_bet)

    collective_fold, has_station, has_maniac, primary_arch = tracker.summarize_opponents(active_opponents)

    # --- 1. PREFLOP STRATEGY ---
    if phase == "PREFLOP":
        pct = preflop_percentile(hole_cards)

        # Unopened pot (no one has raised above the Big Blind yet)
        if highest_bet <= big_blind:
            open_thresh = (0.40 if heads_up else 0.74) - (0.12 if late_pos else 0.0)
            limpers = sum(1 for p in active_opponents if p.get("bet", 0) == big_blind)
            if can_raise and pct >= open_thresh:
                open_target = int(big_blind * (2.25 + 0.70 * limpers))
                return "raise", max(min_safe_raise, min(max_target, open_target))
            if price == 0:
                return "check", 0
            if pct >= (0.52 if late_pos else 0.60) and pot_odds <= 0.24:
                return "call", 0
            return "fold", 0

        # Facing a preflop raise: run Monte Carlo against the raiser's narrowed range
        win_prob = monte_carlo_equity(hole_cards, community_cards, active_opponents, tracker, num_simulations=320)
        # 3-bet with elite hands (JJ+, AK)
        if can_raise and pct >= 0.965 and win_prob >= max(pot_odds + 0.16, 0.48):
            target_3bet = int(max(highest_bet * 2.75, min_safe_raise))
            return "raise", max(min_safe_raise, min(max_target, target_3bet))

        # Tighter call defense against wide opens so we don't call OOP with weak hands that fold postflop
        pf_margin = 0.02 if (price >= me["stack"] or has_maniac) else (0.06 if late_pos else 0.085)
        min_call_pct = 0.56 if heads_up else 0.76
        if price > 0 and (win_prob < pot_odds + pf_margin or (price >= 3.5 * big_blind and pct < min_call_pct)):
            return "fold", 0
        return ("call", 0) if price > 0 else ("check", 0)

    # --- 2. POSTFLOP STRATEGY (FLOP, TURN, RIVER) ---
    win_prob = monte_carlo_equity(hole_cards, community_cards, active_opponents, tracker, num_simulations=450)
    if late_pos:
        win_prob = min(1.0, win_prob + 0.015)

    fair_equity = 1.0 / (len(active_opponents) + 1)
    state_key = bandit.get_state_key(phase, win_prob, primary_arch)
    board_wetness = get_board_wetness(community_cards)
    made_category = evaluate_7card_fast(hole_cards + community_cards)[0]

    # Lower heads-up value margin to fair_equity + 0.05 to contest short-handed pots more actively
    value_threshold = max(0.45, fair_equity + (0.05 if heads_up else 0.17))

    # If facing a postflop bet:
    if price > 0:
        call_margin = 0.015 if (phase == "RIVER" or price >= me["stack"] or has_maniac) else 0.055
        if win_prob < pot_odds + call_margin:
            return "fold", 0

        if can_raise and made_category >= 1 and win_prob >= max(value_threshold + 0.15, 0.64):
            target = pick_raise_amount(
                phase, win_prob, fair_equity, eligible_pot + price, big_blind,
                highest_bet, min_safe_raise, max_target, board_wetness,
                has_station, state_key, bandit
            )
            return "raise", target
        return "call", 0

    # If checked to us (price == 0):
    if can_raise and made_category >= 1 and win_prob >= value_threshold:
        target = pick_raise_amount(
            phase, win_prob, fair_equity, eligible_pot, big_blind,
            highest_bet, min_safe_raise, max_target, board_wetness,
            has_station, state_key, bandit
        )
        return "raise", target

    # More active heads-up semi-bluff on Flop/Turn against folders
    if can_raise and heads_up and phase in ("FLOP", "TURN") and not has_station:
        if 0.27 <= win_prob < value_threshold and collective_fold >= 0.42 and random.random() < 0.50:
            bluff_target = max(min_safe_raise, min(max_target, int(eligible_pot * 0.48)))
            return "raise", bluff_target

    return "check", 0


async def run_bot(uri, my_id, my_name):
    tracker = OpponentTracker(my_id)
    bandit = InMemoryBanditLearner()

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
                tracker.update(state)

                hand_num = state.get("hand_number", 0)
                big_blind = state.get("big_blind", 20)
                me = next((p for p in state["players"] if p["id"] == my_id), None)

                if me:
                    current_total = me["stack"] + me["bet"]
                    if hand_num != bandit.current_hand:
                        if bandit.current_hand != -1 and bandit.trajectory:
                            bandit.end_hand_update(current_total, big_blind)
                        bandit.current_hand = hand_num
                        bandit.hand_start_chips = current_total
                    elif state.get("phase") == "WAITING" and bandit.trajectory:
                        bandit.end_hand_update(current_total, big_blind)

                for p in state["players"]:
                    if p["stack"] == 0 and not p.get("all_in", False) and p["id"] not in busted_players:
                        busted_players.add(p["id"])
                        stacks_summary = ", ".join(f"{pl['name']}: ${pl['stack']}" for pl in state["players"])
                        print(f"[Hand {state['hand_number']}] {p['name']} ({p['id']}) was eliminated! | Stacks: {stacks_summary}")

                if state["current_turn"] == my_id:
                    action, amount = choose_action(state, my_id, tracker, bandit)
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
    parser = argparse.ArgumentParser(description="Iteration 4: Self-Contained Tournament Bot")
    parser.add_argument("--id", default="bot-v4-rl", help="Unique player_id for the bot")
    parser.add_argument("--name", default="Bot v4 (RL + MCTS)", help="Display name at table")
    parser.add_argument("--url", default="ws://localhost:8000/ws", help="Server websocket URL")
    parser.add_argument("--qfile", default="rl_q_table.json", help="Ignored (kept for CLI compatibility)")
    args = parser.parse_args()

    asyncio.run(run_bot(args.url, args.id, args.name))