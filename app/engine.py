import random
from collections import Counter, OrderedDict
from dataclasses import dataclass, field
from enum import Enum
from itertools import combinations
from typing import Iterable, Optional


class Suit(Enum):
    HEARTS = "hearts"
    DIAMONDS = "diamonds"
    CLUBS = "clubs"
    SPADES = "spades"


@dataclass(frozen=True)
class Card:
    rank: int
    suit: Suit

    def __str__(self) -> str:
        rank_name = {11: "J", 12: "Q", 13: "K", 14: "A"}.get(self.rank, str(self.rank))
        return f"{rank_name}{self.suit.value[0].upper()}"

    def to_dict(self) -> dict:
        return {"rank": self.rank, "suit": self.suit.value, "label": str(self)}


@dataclass
class Player:
    player_id: str
    name: str
    stack: int
    hole_cards: list[Card] = field(default_factory=list)
    folded: bool = False
    all_in: bool = False
    bet: int = 0
    hand_contribution: int = 0
    connected: bool = True

    def reset_for_hand(self) -> None:
        self.hole_cards = []
        self.folded = self.stack == 0
        self.all_in = False
        self.bet = 0
        self.hand_contribution = 0


class PokerEngine:
    """Rules engine for one no-limit Texas Hold'em table.

    ``pot`` is never client supplied: it is the sum of every player's
    ``hand_contribution``. ``amount`` for a raise is the desired total bet
    for the current betting round, not an additional amount.
    """

    MAX_PLAYERS = 20

    def __init__(self, starting_stack: int = 1000, small_blind: int = 10, big_blind: int = 20):
        if min(starting_stack, small_blind, big_blind) <= 0 or small_blind > big_blind:
            raise ValueError("Stacks and blinds must be positive, with small blind <= big blind")
        self.starting_stack, self.small_blind, self.big_blind = starting_stack, small_blind, big_blind
        self.players: OrderedDict[str, Player] = OrderedDict()
        self.player_order: list[str] = []
        self.phase = "WAITING"
        self.community_cards: list[Card] = []
        self.deck: list[Card] = []
        self.pot = 0
        self.current_bet = 0
        self.minimum_raise = big_blind
        self.current_turn: Optional[str] = None
        self.dealer_index = -1
        self.round_acted: set[str] = set()
        # Players in this set have already had an opportunity since the last
        # full raise. A short all-in must not reopen raising for them.
        self.raise_locked: set[str] = set()
        self.last_action: Optional[dict] = None
        self.hand_number = 0
        # Set by _resolve_showdown() for a contested pot: the phase holds at
        # SHOWDOWN with hole cards revealed until finalize_showdown() applies
        # this payout and reopens the table, giving observers time to react.
        self.pending_payout: Optional[dict[str, int]] = None

    def add_player(self, player_id: str, name: str) -> bool:
        if not player_id or not name:
            raise ValueError("Player id and name are required")
        if player_id in self.players:
            self.players[player_id].connected = True
            self.players[player_id].name = name
            if player_id not in self.player_order:
                self.player_order.append(player_id)
            return False
        if len(self.players) >= self.MAX_PLAYERS:
            raise ValueError(f"Table is full; max players is {self.MAX_PLAYERS}")
        self.players[player_id] = Player(player_id, name, self.starting_stack)
        self.player_order.append(player_id)
        return True

    def remove_player(self, player_id: str) -> None:
        player = self.players.get(player_id)
        if player is None:
            return
        player.connected = False
        if self.phase != "WAITING" and not player.folded:
            player.folded = True
            self.round_acted.add(player_id)
            self._finish_if_ready()
            # A disconnect is a fold. If it was that bot's turn and play is
            # still live, advance immediately so the hand cannot deadlock.
            if self.phase != "WAITING" and self.current_turn == player_id:
                self.current_turn = self._next_actor(self.player_order.index(player_id))
                self._finish_if_ready()

    def _build_deck(self) -> list[Card]:
        deck = [Card(rank, suit) for suit in Suit for rank in range(2, 15)]
        random.shuffle(deck)
        return deck

    def _in_hand(self, player_id: str) -> bool:
        player = self.players[player_id]
        return bool(player.hole_cards) and not player.folded

    def _can_act(self, player_id: str) -> bool:
        player = self.players[player_id]
        return self._in_hand(player_id) and player.connected and not player.all_in

    def _next_matching(self, start_index: int, predicate) -> Optional[str]:
        if not self.player_order:
            return None
        for offset in range(1, len(self.player_order) + 1):
            candidate = self.player_order[(start_index + offset) % len(self.player_order)]
            if predicate(candidate):
                return candidate
        return None

    def _next_in_hand(self, start_index: int) -> Optional[str]:
        return self._next_matching(start_index, self._in_hand)

    def _next_actor(self, start_index: int) -> Optional[str]:
        return self._next_matching(start_index, self._can_act)

    def can_start_hand(self) -> bool:
        return self.phase == "WAITING" and sum(p.connected and p.stack > 0 for p in self.players.values()) >= 2

    def _commit(self, player_id: str, amount: int) -> int:
        player = self.players[player_id]
        contribution = min(amount, player.stack)
        player.stack -= contribution
        player.bet += contribution
        player.hand_contribution += contribution
        self.pot += contribution
        if player.stack == 0:
            player.all_in = True
        return contribution

    def start_hand(self) -> dict:
        if not self.can_start_hand():
            raise ValueError("At least two connected players with chips are required to start a hand")
        self.hand_number += 1
        self.phase, self.community_cards, self.pot = "PREFLOP", [], 0
        self.current_bet, self.minimum_raise, self.round_acted, self.raise_locked = 0, self.big_blind, set(), set()
        self.deck = self._build_deck()
        for player in self.players.values():
            player.reset_for_hand()
        next_dealer = self._next_matching(self.dealer_index, lambda pid: self.players[pid].connected and self.players[pid].stack > 0)
        self.dealer_index = self.player_order.index(next_dealer)
        participants = [pid for pid in self.player_order if self.players[pid].connected and self.players[pid].stack > 0]
        for _ in range(2):
            for pid in participants:
                self.players[pid].hole_cards.append(self.deck.pop())
        dealer = self.dealer
        if len(participants) == 2:
            small_blind, big_blind = dealer, self._next_in_hand(self.dealer_index)
        else:
            small_blind = self._next_in_hand(self.dealer_index)
            big_blind = self._next_in_hand(self.player_order.index(small_blind))
        self._commit(small_blind, self.small_blind)
        self._commit(big_blind, self.big_blind)
        # A short-stacked big blind can post less than the small blind, so
        # the bet to match is whichever blind actually put in more.
        self.current_bet = max(self.players[small_blind].bet, self.players[big_blind].bet)
        self.current_turn = self._next_actor(self.player_order.index(big_blind))
        self.last_action = {"type": "deal", "dealer": dealer, "small_blind": small_blind, "big_blind": big_blind}
        self._finish_if_ready()
        return self.snapshot()

    def _begin_round(self) -> None:
        self.round_acted.clear()
        self.raise_locked.clear()
        self.current_bet, self.minimum_raise = 0, self.big_blind
        for player in self.players.values():
            player.bet = 0

    def _burn_and_deal(self, count: int) -> None:
        self.deck.pop()
        self.community_cards.extend(self.deck.pop() for _ in range(count))

    def _advance_phase(self) -> None:
        if self.phase == "PREFLOP":
            self.phase, count = "FLOP", 3
        elif self.phase == "FLOP":
            self.phase, count = "TURN", 1
        elif self.phase == "TURN":
            self.phase, count = "RIVER", 1
        else:
            self.phase = "SHOWDOWN"
            self.current_turn = None
            self._resolve_showdown()
            return
        self._begin_round()
        self._burn_and_deal(count)
        self.current_turn = self._next_actor(self.dealer_index)

    def _round_complete(self) -> bool:
        contenders = [pid for pid in self.player_order if self._in_hand(pid)]
        if len(contenders) <= 1:
            return True
        return all(self.players[pid].all_in or (pid in self.round_acted and self.players[pid].bet == self.current_bet) for pid in contenders)

    def _finish_if_ready(self) -> None:
        while self.phase not in ("WAITING", "SHOWDOWN") and self._round_complete():
            if len([pid for pid in self.player_order if self._in_hand(pid)]) <= 1:
                self._resolve_showdown()
                return
            self._advance_phase()
            if self.phase in ("WAITING", "SHOWDOWN"):
                return

    def place_action(self, player_id: str, action: str, amount: int = 0) -> dict:
        if self.phase == "WAITING":
            return {"ok": False, "error": "No hand is in progress"}
        if self.phase == "SHOWDOWN":
            return {"ok": False, "error": "Hand is at showdown; waiting for payout"}
        if player_id != self.current_turn:
            return {"ok": False, "error": "It is not this player's turn"}
        if action not in {"fold", "check", "call", "raise"} or not isinstance(amount, int) or amount < 0:
            return {"ok": False, "error": "Invalid action payload"}
        player = self.players[player_id]
        if not self._can_act(player_id):
            return {"ok": False, "error": "Player can no longer act"}
        if action == "fold":
            player.folded = True
            self.last_action = {"type": "fold", "player": player_id}
        elif action == "check":
            if player.bet != self.current_bet:
                return {"ok": False, "error": "Cannot check while facing a bet"}
            self.last_action = {"type": "check", "player": player_id}
        elif action == "call":
            paid = self._commit(player_id, self.current_bet - player.bet)
            self.last_action = {"type": "call", "player": player_id, "amount": paid}
        else:
            if amount <= self.current_bet:
                return {"ok": False, "error": "Raise target must exceed the current bet"}
            if amount > player.bet + player.stack:
                return {"ok": False, "error": "Raise exceeds available stack"}
            raise_size, is_all_in = amount - self.current_bet, amount == player.bet + player.stack
            if raise_size < self.minimum_raise and not is_all_in:
                return {"ok": False, "error": f"Raise must increase the bet by at least {self.minimum_raise}"}
            # A player who has already acted cannot re-raise after a short
            # all-in. Bots that have not acted since the last full raise may.
            if self.current_bet > 0 and player_id in self.raise_locked:
                return {"ok": False, "error": "A short all-in did not reopen raising"}
            paid = self._commit(player_id, amount - player.bet)
            self.current_bet = player.bet
            if raise_size >= self.minimum_raise:
                self.minimum_raise = raise_size
                self.round_acted = {player_id}
                self.raise_locked = {player_id}
            self.last_action = {"type": "raise", "player": player_id, "amount": paid, "target": amount}
        self.round_acted.add(player_id)
        self.raise_locked.add(player_id)
        phase_before_completion = self.phase
        self._finish_if_ready()
        if self.phase != "WAITING" and self.phase == phase_before_completion:
            self.current_turn = self._next_actor(self.player_order.index(player_id))
            self._finish_if_ready()
        return {"ok": True, "phase": self.phase, "pot": self.pot, "current_turn": self.current_turn, "action": self.last_action, "state": self.snapshot(player_id)}

    def _resolve_showdown(self) -> None:
        contenders = [pid for pid in self.player_order if self._in_hand(pid)]
        if len(contenders) <= 1:
            if contenders:
                winner = contenders[0]
                self.players[winner].stack += self.pot
                self.last_action = {"type": "payout", "winners": [winner], "pot": self.pot}
            self.pot, self.current_turn, self.phase = 0, None, "WAITING"
            return
        scores = {pid: self.evaluate_best_hand(self.players[pid].hole_cards + self.community_cards) for pid in contenders}
        payouts: dict[str, int] = {}
        previous = 0
        for level in sorted({p.hand_contribution for p in self.players.values() if p.hand_contribution}):
            contributors = [pid for pid, p in self.players.items() if p.hand_contribution >= level]
            layer = (level - previous) * len(contributors)
            eligible = [pid for pid in contenders if self.players[pid].hand_contribution >= level]
            if eligible:
                best = max(scores[pid] for pid in eligible)
                winners = [pid for pid in eligible if scores[pid] == best]
                share, remainder = divmod(layer, len(winners))
                for pid in winners:
                    payouts[pid] = payouts.get(pid, 0) + share
                ordered = [pid for pid in self.player_order[self.dealer_index + 1:] + self.player_order[:self.dealer_index + 1] if pid in winners]
                for pid in ordered[:remainder]:
                    payouts[pid] += 1
            previous = level
        self.last_action = {"type": "payout", "winners": list(payouts), "payouts": payouts, "pot": self.pot}
        # A contested pot holds at SHOWDOWN with hole cards revealed and
        # stacks untouched; finalize_showdown() applies payouts once the
        # server-side reveal delay elapses.
        self.pending_payout = payouts
        self.current_turn, self.phase = None, "SHOWDOWN"

    def finalize_showdown(self) -> None:
        if self.phase != "SHOWDOWN" or self.pending_payout is None:
            return
        for pid, amount in self.pending_payout.items():
            self.players[pid].stack += amount
        self.pending_payout = None
        self.pot, self.current_turn, self.phase = 0, None, "WAITING"

    @property
    def dealer(self) -> Optional[str]:
        return self.player_order[self.dealer_index] if self.player_order and self.dealer_index >= 0 else None

    def snapshot(self, player_id: Optional[str] = None) -> dict:
        players = []
        for pid, p in self.players.items():
            reveal = player_id == pid or (self.phase == "SHOWDOWN" and not p.folded)
            cards = [c.to_dict() for c in p.hole_cards] if reveal else [{"rank": "?", "suit": "?", "label": "??"} for _ in p.hole_cards]
            players.append({"id": p.player_id, "name": p.name, "stack": p.stack, "bet": p.bet, "folded": p.folded, "all_in": p.all_in, "connected": p.connected, "hole_cards": cards})
        return {"phase": self.phase, "pot": self.pot, "community_cards": [c.to_dict() for c in self.community_cards], "players": players, "current_turn": self.current_turn, "dealer": self.dealer, "hand_number": self.hand_number, "last_action": self.last_action, "small_blind": self.small_blind, "big_blind": self.big_blind}

    @staticmethod
    def _straight_high(ranks: list[int]) -> Optional[int]:
        unique = set(ranks)
        for high in range(14, 4, -1):
            if set(range(high - 4, high + 1)).issubset(unique):
                return high
        return 5 if {14, 2, 3, 4, 5}.issubset(unique) else None

    @staticmethod
    def _evaluate_five(cards: tuple[Card, ...]) -> tuple[int, tuple[int, ...]]:
        ranks = sorted((c.rank for c in cards), reverse=True)
        counts = Counter(ranks)
        flush, straight = len({c.suit for c in cards}) == 1, PokerEngine._straight_high(ranks)
        if flush and straight:
            return 8, (straight,)
        groups = sorted(((count, rank) for rank, count in counts.items()), reverse=True)
        if groups[0][0] == 4:
            return 7, (groups[0][1], groups[1][1])
        if groups[0][0] == 3 and groups[1][0] == 2:
            return 6, (groups[0][1], groups[1][1])
        if flush:
            return 5, tuple(ranks)
        if straight:
            return 4, (straight,)
        if groups[0][0] == 3:
            return 3, (groups[0][1], *sorted((r for r in ranks if r != groups[0][1]), reverse=True))
        if groups[0][0] == groups[1][0] == 2:
            return 2, (groups[0][1], groups[1][1], groups[2][1])
        if groups[0][0] == 2:
            pair = groups[0][1]
            return 1, (pair, *sorted((r for r in ranks if r != pair), reverse=True))
        return 0, tuple(ranks)

    @staticmethod
    def evaluate_best_hand(cards: Iterable[Card]) -> tuple[int, list[int]]:
        card_list = list(cards)
        if len(card_list) < 5:
            return 0, sorted((c.rank for c in card_list), reverse=True)
        rank, breakers = max(PokerEngine._evaluate_five(combo) for combo in combinations(card_list, 5))
        return rank, list(breakers)
