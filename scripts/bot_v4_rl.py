import asyncio
import json
import argparse
import random
from itertools import combinations
from collections import Counter, defaultdict
import websockets

SUITS = ["hearts", "diamonds", "clubs", "spades"]
RANKS = list(range(2, 15))  # 2 through 14 (where 11=J, 12=Q, 13=K, 14=A)


class InMemoryBanditLearner:
    """
    Self-contained in-memory Reinforcement Learning module for bet-sizing selection.
    Requires no external files and adapts bet sizing (0.55x, 0.85x, 1.25x pot)
    based on which sizing extracts the most chips per opponent archetype.
    """
    def __init__(self, alpha=0.10):
        self.alpha = alpha
        self.q_table = defaultdict(lambda: {"raise_small": 0.05, "raise_pot": 0.05, "raise_overbet": 0.0})
        self._init_warm_start()
        self.trajectory = []
        self.hand_start_chips = None
        self.current_hand = -1

    def _init_warm_start(self):
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
    Tracks TRUE postflop & raised-pot tendencies (ignoring routine $20 preflop blind folds
    so we never mistake a calling station for a folder).
    """
    def __init__(self, my_id):
        self.my_id = my_id
        self.stats = defaultdict(lambda: {
            "actions": 0, "folds": 0, "calls": 0,
            "checks": 0, "raises": 0,
            "faced_real_bet": 0, "folded_to_real_bet": 0,
            "showdown_bluffs": 0
        })
        self.current_hand_number = -1
        self.last_recorded_event = None
        self.hand_aggressors = set()
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

        if phase == "SHOWDOWN" and self.recorded_showdown_hand != hand_num:
            self.recorded_showdown_hand = hand_num
            for p in players:
                pid = p.get("id")
                cards = p.get("hole_cards", [])
                if pid != self.my_id and len(cards) == 2 and isinstance(cards[0].get("rank"), int):
                    if pid in self.hand_aggressors and preflop_chen_score(cards) < 0.32:
                        self.stats[pid]["showdown_bluffs"] += 1

        last_act = state.get("last_action")
        if last_act and last_act != self.last_recorded_event:
            self.last_recorded_event = last_act
            act_type = last_act.get("type")
            pid = last_act.get("player")

            if pid and pid != self.my_id and act_type in ("fold", "check", "call", "raise"):
                s = self.stats[pid]
                s["actions"] += 1

                # Only count facing a bet if it's POSTFLOP or a preflop raise (> 1 BB)
                # This fixes the bug where routine 1 BB preflop folds made bots look like folders!
                is_real_bet = (phase != "PREFLOP") or (highest_bet > big_blind)

                if act_type == "fold":
                    s["folds"] += 1
                    if is_real_bet:
                        s["faced_real_bet"] += 1
                        s["folded_to_real_bet"] += 1
                elif act_type == "check":
                    s["checks"] += 1
                elif act_type == "call":
                    s["calls"] += 1
                    if is_real_bet:
                        s["faced_real_bet"] += 1
                elif act_type == "raise":
                    if is_real_bet:
                        s["faced_real_bet"] += 1
                    target = last_act.get("target", 0)
                    if target >= big_blind * 2.5 or target >= pot * 0.25:
                        s["raises"] += 1
                        self.hand_aggressors.add(pid)

    def get_real_fold_prob(self, pid):
        s = self.stats[pid]
        return (s["folded_to_real_bet"] + 2.0) / (s["faced_real_bet"] + 8.0)

    def get_aggression_rate(self, pid):
        s = self.stats[pid]
        return (s["raises"] + 1.5) / (s["actions"] + 10.0)

    def get_archetype(self, pid):
        s = self.stats[pid]
        if s["showdown_bluffs"] >= 1:
            return "MANIAC"
        if s["actions"] < 6:
            return "TAG"
        fold_p = self.get_real_fold_prob(pid)
        agg_r = self.get_aggression_rate(pid)
        if fold_p < 0.30 and agg_r < 0.22:
            return "STATION"
        if agg_r >= 0.26:
            return "MANIAC"
        if fold_p >= 0.52:
            return "NIT"
        return "TAG"


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


def evaluate_5card_fast(cards):
    r0, r1, r2, r3, r4 = sorted((c["rank"] for c in cards), reverse=True)
    s0, s1, s2, s3, s4 = (c["suit"] for c in cards)
    is_flush = (s0 == s1 == s2 == s3 == s4)

    if r0 > r1 > r2 > r3 > r4:
        is_straight = (r0 - r4 == 4) or (r0 == 14 and r1 == 5)
        if is_straight:
            sh = 5 if (r0 == 14 and r1 == 5) else r0
            return (8, sh, 0, 0, 0, 0) if is_flush else (4, sh, 0, 0, 0, 0)
        if is_flush:
            return (5, r0, r1, r2, r3, r4)
        return (0, r0, r1, r2, r3, r4)

    counts = Counter((r0, r1, r2, r3, r4))
    sorted_counts = sorted(counts.items(), key=lambda x: (x[1], x[0]), reverse=True)
    freq0 = sorted_counts[0][1]
    freq1 = sorted_counts[1][1]
    tb = tuple(rank for rank, _ in sorted_counts)

    if freq0 == 4:
        return (7,) + tb
    if freq0 == 3 and freq1 == 2:
        return (6,) + tb
    if is_flush:
        return (5, r0, r1, r2, r3, r4)
    if freq0 == 3:
        return (3,) + tb
    if freq0 == 2 and freq1 == 2:
        return (2,) + tb
    return (1,) + tb


def evaluate_7card_fast(seven_cards):
    best = (-1,)
    for combo in combinations(seven_cards, 5):
        score = evaluate_5card_fast(combo)
        if score > best:
            best = score
    return best


def monte_carlo_equity(hole_cards, community_cards, active_opponents, tracker, num_simulations=500):
    """
    Runs 500 fast rollouts. Only applies range filtering when an opponent is a
    tight/passive raiser (so we never underestimate our equity against frequent bluffers like v3).
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

    opp_thresholds = []
    for opp in active_opponents:
        oid = opp["id"]
        arch = tracker.get_archetype(oid)
        if oid in tracker.hand_aggressors and arch != "MANIAC":
            # Tight/normal raisers hold at least a 0.35 Chen hand when they raise
            opp_thresholds.append(0.35)
        else:
            opp_thresholds.append(0.0)

    for _ in range(num_simulations):
        deck_copy = remaining_deck[:]
        random.shuffle(deck_copy)

        sim_board = community_cards + deck_copy[:cards_needed_for_board]
        pool_idx = cards_needed_for_board
        my_best = evaluate_7card_fast(hole_cards + sim_board)

        beaten = False
        tied = False

        for threshold in opp_thresholds:
            opp_hole = deck_copy[pool_idx : pool_idx + 2]
            pool_idx += 2

            if threshold > 0.0 and pool_idx + 2 <= len(deck_copy):
                if preflop_chen_score(opp_hole) < threshold:
                    opp_hole = deck_copy[pool_idx : pool_idx + 2]
                    pool_idx += 2

            opp_best = evaluate_7card_fast(opp_hole + sim_board)
            if opp_best > my_best:
                beaten = True
                break
            elif opp_best == my_best:
                tied = True

        if not beaten:
            wins += 0.5 if tied else 1.0

    return wins / num_simulations


def choose_action(state, my_id, tracker, bandit):
    """
    Combines Bot v2's rock-solid value discipline (never spewing chips on weak raises)
    with 500-rollout speed, true postflop opponent profiling, and Bandit bet-sizing.
    """
    players = state["players"]
    me = next(p for p in players if p["id"] == my_id)
    highest_bet = max(p["bet"] for p in players)
    to_call = highest_bet - me["bet"]
    pot = state.get("pot", 0)
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

    num_opponents = len(active_opponents)
    total_active_players = num_opponents + 1

    # 1. Get exact 500-rollout win probability
    win_prob = monte_carlo_equity(
        hole_cards, community_cards, active_opponents, tracker, num_simulations=500
    )

    if state.get("dealer") == my_id:
        win_prob = min(1.0, win_prob + 0.02)

    # 2. Baseline equity and Pot Odds (Bot v2's proven foundation)
    fair_equity = 1.0 / total_active_players
    pot_odds = to_call / (pot + to_call) if (pot + to_call) > 0 else 0.0

    # 3. Profile active opponents using TRUE postflop/raised fold rates
    collective_real_fold = 1.0
    has_calling_station = False
    has_maniac = False
    primary_arch = "TAG"

    for opp in active_opponents:
        oid = opp["id"]
        collective_real_fold *= tracker.get_real_fold_prob(oid)
        arch = tracker.get_archetype(oid)
        if arch == "STATION":
            has_calling_station = True
            primary_arch = "STATION"
        elif arch == "MANIAC":
            has_maniac = True
            if primary_arch != "STATION":
                primary_arch = "MANIAC"
        elif arch == "NIT" and primary_arch == "TAG":
            primary_arch = "NIT"

    state_key = bandit.get_state_key(phase, win_prob, primary_arch)
    max_raise_target = me["bet"] + me["stack"]
    min_safe_raise = highest_bet + max(big_blind, to_call)

    # 4. VALUE RAISING & SELECTIVE NIT STEALING
    # Require Bot v2's strong value threshold (fair_equity + 0.20) so we never value-own ourselves!
    # Only allow a semi-bluff steal when heads-up against a proven NIT (real fold prob >= 0.55).
    is_value_raise = (win_prob >= fair_equity + 0.20)
    is_nit_steal = (
        num_opponents == 1
        and not has_calling_station
        and collective_real_fold >= 0.55
        and win_prob >= 0.42
        and to_call == 0
    )

    if (is_value_raise or is_nit_steal) and max_raise_target > highest_bet:
        if highest_bet < big_blind * 8 or win_prob >= 0.76:
            # Choose optimal bet size using Bandit RL + opponent profile
            sizing_candidates = [
                ("raise_small", max(min_safe_raise, highest_bet + int(max(pot * 0.55, 2.5 * big_blind)))),
                ("raise_pot", max(min_safe_raise, highest_bet + int(max(pot * 0.80, 3.0 * big_blind)))),
            ]
            if win_prob >= fair_equity + 0.36 or (has_calling_station and win_prob >= 0.72):
                sizing_candidates.append(
                    ("raise_overbet", max(min_safe_raise, highest_bet + int(max(pot * 1.15, 4.0 * big_blind))))
                )

            best_arm = "raise_small"
            best_target = sizing_candidates[0][1]
            best_score = -999.0

            for arm_key, raw_target in sizing_candidates:
                target = min(raw_target, max_raise_target)
                # Base preference: larger sizes when equity is huge, smaller sizes for thin value/steals
                base_pref = 0.20 if (arm_key == "raise_pot" and win_prob >= fair_equity + 0.32) else 0.10
                if arm_key == "raise_overbet" and win_prob >= 0.80:
                    base_pref = 0.25
                score = base_pref + bandit.get_bonus(state_key, arm_key)
                if score > best_score:
                    best_score = score
                    best_arm = arm_key
                    best_target = target

            bandit.record_step(state_key, best_arm)
            return "raise", best_target

    if to_call == 0:
        return "check", 0

    # 5. CALLING LOGIC (Punishes Bot v3's bluffs while respecting tight raisers)
    # Against Maniacs / Bot v3 bluffers, call with a tiny 0.01 buffer over pot odds.
    # Against standard/tight opponents, use Bot v2's proven 0.03 buffer.
    call_margin = 0.01 if has_maniac else 0.03

    if win_prob >= (pot_odds + call_margin):
        # Protect our stack against massive all-in shoves (> 10 BB) unless we have solid equity,
        # but lower the requirement slightly if the shover is a known Maniac/bluffer
        shove_margin = 0.08 if has_maniac else 0.12
        if to_call > big_blind * 10 and win_prob < fair_equity + shove_margin:
            return "fold", 0
        return "call", 0

    # Cheap preflop blind defense
    if to_call <= big_blind and win_prob >= (fair_equity - 0.08):
        return "call", 0

    return "fold", 0


async def run_bot(uri, my_id, my_name):
    tracker = OpponentTracker(my_id)
    bandit = InMemoryBanditLearner()

    async with websockets.connect(uri) as websocket:
        await websocket.send(json.dumps({
            "type": "join",
            "name": my_name,
            "player_id": my_id
        }))
        print(f"[{my_name}] Connected to {uri} as '{my_id}' (Disciplined EV + Anti-Bluff Ready)")
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
                        stacks_summary = ", ".join(
                            f"{pl['name']}: ${pl['stack']}" for pl in state["players"]
                        )
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