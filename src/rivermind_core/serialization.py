from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

from rivermind_core.models import (
    Action,
    ActionType,
    BettingRound,
    GameType,
    HandHistory,
    Player,
)


def hand_to_json(hand: HandHistory) -> str:
    payload = {
        "site": hand.site,
        "hand_id": hand.hand_id,
        "game_type": hand.game_type.value,
        "game_name": hand.game_name,
        "currency": hand.currency,
        "table_name": hand.table_name,
        "max_seats": hand.max_seats,
        "button_seat": hand.button_seat,
        "small_blind": str(hand.small_blind),
        "big_blind": str(hand.big_blind),
        "played_at_raw": hand.played_at_raw,
        "players": [
            {
                "seat": player.seat,
                "name": player.name,
                "starting_stack": str(player.starting_stack),
                "is_hero": player.is_hero,
                "hole_cards": list(player.hole_cards),
            }
            for player in hand.players
        ],
        "actions": [
            {
                "sequence": action.sequence,
                "street": action.street.value,
                "player": action.player,
                "action_type": action.action_type.value,
                "amount": _decimal_to_json(action.amount),
                "to_amount": _decimal_to_json(action.to_amount),
                "is_all_in": action.is_all_in,
                "raw_text": action.raw_text,
            }
            for action in hand.actions
        ],
        "board": list(hand.board),
        "total_pot": _decimal_to_json(hand.total_pot),
        "rake": _decimal_to_json(hand.rake),
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def hand_from_json(payload_json: str, *, raw_text: str = "") -> HandHistory:
    payload: dict[str, Any] = json.loads(payload_json)
    return HandHistory(
        site=payload["site"],
        hand_id=payload["hand_id"],
        game_type=GameType(payload["game_type"]),
        game_name=payload["game_name"],
        currency=payload["currency"],
        table_name=payload["table_name"],
        max_seats=payload["max_seats"],
        button_seat=payload["button_seat"],
        small_blind=Decimal(payload["small_blind"]),
        big_blind=Decimal(payload["big_blind"]),
        played_at_raw=payload["played_at_raw"],
        players=tuple(
            Player(
                seat=item["seat"],
                name=item["name"],
                starting_stack=Decimal(item["starting_stack"]),
                is_hero=item["is_hero"],
                hole_cards=tuple(item["hole_cards"]),
            )
            for item in payload["players"]
        ),
        actions=tuple(
            Action(
                sequence=item["sequence"],
                street=BettingRound(item["street"]),
                player=item["player"],
                action_type=ActionType(item["action_type"]),
                amount=_decimal_from_json(item["amount"]),
                to_amount=_decimal_from_json(item["to_amount"]),
                is_all_in=item["is_all_in"],
                raw_text=item["raw_text"],
            )
            for item in payload["actions"]
        ),
        board=tuple(payload["board"]),
        total_pot=_decimal_from_json(payload["total_pot"]),
        rake=_decimal_from_json(payload["rake"]),
        raw_text=raw_text,
    )


def _decimal_to_json(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None


def _decimal_from_json(value: str | None) -> Decimal | None:
    return Decimal(value) if value is not None else None
