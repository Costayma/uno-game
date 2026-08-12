"""
UNO 游戏逻辑模块
"""
import random
from enum import Enum
from typing import List, Optional

class Color(Enum):
    RED = "red"
    YELLOW = "yellow"
    GREEN = "green"
    BLUE = "blue"
    WILD = "wild"

class CardType(Enum):
    NUMBER = "number"
    SKIP = "skip"
    REVERSE = "reverse"
    DRAW_TWO = "draw_two"
    WILD = "wild"
    WILD_DRAW_FOUR = "wild_draw_four"

class Card:
    """UNO 卡牌"""
    def __init__(self, color: Color, card_type: CardType, value: Optional[int] = None):
        self.color = color
        self.card_type = card_type
        self.value = value

    def to_dict(self) -> dict:
        return {
            "color": self.color.value,
            "card_type": self.card_type.value,
            "value": self.value,
        }

    def __repr__(self):
        if self.card_type == CardType.NUMBER:
            return f"{self.color.value}_{self.value}"
        elif self.card_type == CardType.WILD:
            return "wild"
        elif self.card_type == CardType.WILD_DRAW_FOUR:
            return "wild_draw4"
        else:
            return f"{self.color.value}_{self.card_type.value}"

def create_deck() -> List[Card]:
    """创建一副完整的 UNO 牌（108张）"""
    deck = []
    colors = [Color.RED, Color.YELLOW, Color.GREEN, Color.BLUE]
    for color in colors:
        deck.append(Card(color, CardType.NUMBER, 0))
        for num in range(1, 10):
            deck.append(Card(color, CardType.NUMBER, num))
            deck.append(Card(color, CardType.NUMBER, num))
        for _ in range(2):
            deck.append(Card(color, CardType.SKIP))
            deck.append(Card(color, CardType.REVERSE))
            deck.append(Card(color, CardType.DRAW_TWO))
    for _ in range(4):
        deck.append(Card(Color.WILD, CardType.WILD))
        deck.append(Card(Color.WILD, CardType.WILD_DRAW_FOUR))
    return deck

class Player:
    """UNO 玩家"""
    def __init__(self, player_id: str, nickname: str, sid: str):
        self.player_id = player_id
        self.nickname = nickname
        self.sid = sid
        self.hand: List[Card] = []
        self.uno_called = False

    def to_dict(self) -> dict:
        return {
            "player_id": self.player_id,
            "nickname": self.nickname,
            "hand_count": len(self.hand),
            "uno_called": self.uno_called,
        }

class GameState(Enum):
    WAITING = "waiting"
    PLAYING = "playing"
    FINISHED = "finished"

class UnoGame:
    """UNO 游戏房间"""
    def __init__(self, room_id: str, max_players: int = 4):
        self.room_id = room_id
        self.max_players = max_players
        self.state = GameState.WAITING
        self.players: List[Player] = []
        self.winners: List[Player] = [] # 🚨 新增：记录离场的玩家
        self.deck: List[Card] = []
        self.discard_pile: List[Card] = []
        self.current_player_index = 0
        self.direction = 1
        self.current_color = None
        self.winner: Optional[Player] = None
        self.pending_draw = 0
        self.skip_next = False
        self.uno_timer = {}

    def add_player(self, player: Player) -> bool:
        if len(self.players) >= self.max_players:
            return False
        if any(p.player_id == player.player_id for p in self.players):
            return False
        self.players.append(player)
        return True

    def remove_player(self, player_id: str) -> Optional[Player]:
        for i, p in enumerate(self.players):
            if p.player_id == player_id:
                removed = self.players.pop(i)
                if self.state == GameState.PLAYING:
                    if i < self.current_player_index:
                        self.current_player_index -= 1
                    if self.current_player_index >= len(self.players):
                        self.current_player_index = 0
                return removed
        return None

    def start_game(self) -> bool:
        if len(self.players) < 2:
            print("[DEBUG] 玩家不足2人")
            return False
        self.winners = [] # 🚨 重置胜者列表
        self._reset_game_state()
        print("[DEBUG] 游戏重置成功")
        return True

    def reset_game(self, winner_player_id=None):
        if self.state == GameState.FINISHED:
            self._reset_game_state(winner_player_id)
            return True
        return False

    def _reset_game_state(self, winner_player_id=None):
        self.deck = create_deck()
        random.shuffle(self.deck)
        for player in self.players:
            player.hand = []
            player.uno_called = False
        for _ in range(7):
            for player in self.players:
                player.hand.append(self._draw_card())
        first_card = self._draw_card()
        while first_card.color == Color.WILD:
            self.deck.append(first_card)
            random.shuffle(self.deck)
            first_card = self._draw_card()
        self.discard_pile = [first_card]
        self.current_color = first_card.color
        self.current_player_index = 0
        if winner_player_id:
            for i, p in enumerate(self.players):
                if p.player_id == winner_player_id:
                    self.current_player_index = i
                    break
        self.state = GameState.PLAYING
        self.pending_draw = 0
        self.skip_next = False
        self.winner = None
        self.winners = []
        self._apply_first_card_effect(first_card)

    def _apply_first_card_effect(self, card: Card):
        if card.card_type == CardType.SKIP:
            self.skip_next = True
        elif card.card_type == CardType.REVERSE:
            self.direction *= -1
        elif card.card_type == CardType.DRAW_TWO:
            self.pending_draw = 2

    def _draw_card(self) -> Card:
        if not self.deck:
            if len(self.discard_pile) <= 1:
                return Card(Color.RED, CardType.NUMBER, 0)
            top_card = self.discard_pile.pop()
            self.deck = self.discard_pile
            self.discard_pile = [top_card]
            random.shuffle(self.deck)
        return self.deck.pop()

    def get_current_player(self) -> Optional[Player]:
        if not self.players:
            return None
        return self.players[self.current_player_index]

    def can_play_card(self, player: Player, card: Card) -> bool:
        if player not in self.players or card not in player.hand:
            return False
        if self.pending_draw > 0:
            if card.card_type not in (CardType.DRAW_TWO, CardType.WILD_DRAW_FOUR):
                return False
            return True
        top_card = self.discard_pile[-1]
        if card.color == self.current_color:
            return True
        if card.card_type == top_card.card_type:
            if card.card_type == CardType.NUMBER and card.value == top_card.value:
                return True
            if card.card_type != CardType.NUMBER:
                return True
        if card.color == Color.WILD:
            return True
        return False

    def play_card(self, player: Player, card_index: int, chosen_color: Optional[str] = None) -> dict:
        if self.state != GameState.PLAYING:
            return {"success": False, "message": "游戏未开始或已结束", "game_over": False, "winner": None}
        current = self.get_current_player()
        if current.player_id != player.player_id:
            return {"success": False, "message": "还没轮到你", "game_over": False, "winner": None}
        if card_index < 0 or card_index >= len(player.hand):
            return {"success": False, "message": "无效的牌索引", "game_over": False, "winner": None}
        card = player.hand[card_index]
        if not self.can_play_card(player, card):
            return {"success": False, "message": "这张牌不能出", "game_over": False, "winner": None}
        if card.color == Color.WILD and not chosen_color:
            return {"success": False, "message": "万能牌需要选择颜色", "game_over": False, "winner": None}

        player.hand.pop(card_index)
        self.discard_pile.append(card)
        if card.color == Color.WILD:
            self.current_color = Color(chosen_color)
        else:
            self.current_color = card.color

        # 🚨 核心改写：多人淘汰模式与胜利判定
        if len(player.hand) == 0:
            if len(self.players) == 2:
                # 2 人模式：直接结束游戏
                self.state = GameState.FINISHED
                self.winner = player
                return {
                    "success": True,
                    "message": f"{player.nickname} 获胜！",
                    "game_over": True,
                    "winner": player.nickname,
                    "card": card,
                    "is_exit_mode": False
                }
            else:
                # 3人以上模式：记录离场，继续游戏
                self.winners.append(player)
                self.players.remove(player)
                if self.current_player_index >= len(self.players):
                    self.current_player_index = 0

                if len(self.winners) >= 2:
                    # 如果已经有 2 人获胜，游戏结束
                    self.state = GameState.FINISHED
                    self.winner = player
                    return {
                        "success": True,
                        "message": f"淘汰赛结束！胜者：{player.nickname}",
                        "game_over": True,
                        "winner": player.nickname,
                        "card": card,
                        "is_exit_mode": True,
                        "exited_players": [w.to_dict() for w in self.winners]
                    }
                else:
                    # 仅一人离场，游戏继续
                    self._next_turn()
                    return {
                        "success": True,
                        "message": f"淘汰赛进行中，{player.nickname} 暂时离场！",
                        "game_over": False,
                        "winner": None,
                        "card": card,
                        "is_exit_mode": True,
                        "exited_players": [w.to_dict() for w in self.winners]
                    }

        self._apply_card_effect(card)

        # ✅ 【情况一】最后一张牌是功能牌：绝对优先补牌，补完立刻换下家
        if len(player.hand) == 1:
            if player.hand[0].card_type != CardType.NUMBER:
                new_card = self._draw_card()
                player.hand.append(new_card)
                player.uno_called = False
                self._next_turn()  # 🚨 补完牌，立刻把回合交给下家
                return {
                    "success": True,
                    "message": f"检测到您最后一张牌是功能牌，自动为您补摸一张牌！",
                    "game_over": False,
                    "winner": None,
                    "card": card,
                    "auto_draw": True
                }

        # ✅ 【情况二】最后一张牌是数字牌，且玩家忘了喊UNO：触发3秒等待
        if len(player.hand) == 1 and not player.uno_called:
            # 🚨 注意：这里不要写 _next_turn()，换人的动作交给后端 3秒后台任务处理
            return {
                "success": True,
                "message": f"{player.nickname} 忘了喊 UNO！",
                "game_over": False,
                "winner": None,
                "card": card,
                "forgot_uno": True,
                "auto_draw": False
            }

        # 其他正常情况
        self._next_turn()
        return {
            "success": True,
            "message": f"{player.nickname} 打出了 {card}",
            "game_over": False,
            "winner": None,
            "card": card,
            "auto_draw": False
        }

    def _apply_card_effect(self, card: Card):
        if card.card_type == CardType.SKIP:
            self.skip_next = True
        elif card.card_type == CardType.REVERSE:
            if len(self.players) == 2:
                self.skip_next = True
            else:
                self.direction *= -1
        elif card.card_type == CardType.DRAW_TWO:
            self.pending_draw += 2
        elif card.card_type == CardType.WILD_DRAW_FOUR:
            self.pending_draw += 4

    def _next_turn(self):
        if self.skip_next:
            self.current_player_index = (self.current_player_index + self.direction) % len(self.players)
            self.skip_next = False
        self.current_player_index = (self.current_player_index + self.direction) % len(self.players)

    def draw_cards(self, player: Player, count: Optional[int] = None) -> dict:
        if self.state != GameState.PLAYING:
            return {"success": False, "message": "游戏未开始或已结束"}
        current = self.get_current_player()
        if current.player_id != player.player_id:
            return {"success": False, "message": "还没轮到你"}
        if count is None:
            count = self.pending_draw if self.pending_draw > 0 else 1
        drawn = []
        for _ in range(count):
            drawn.append(self._draw_card())
        player.hand.extend(drawn)
        if len(player.hand) > 1:
            player.uno_called = False
        self.pending_draw = 0
        self._next_turn()
        return {"success": True, "message": f"{player.nickname} 抽了 {count} 张牌", "drawn_cards": [c.to_dict() for c in drawn]}

    def call_uno(self, player: Player) -> dict:
        if player not in self.players:
            return {"success": False, "message": "你不是本房间玩家"}
        if len(player.hand) != 2:
            return {"success": False, "message": "只有手牌为2张时才能喊 UNO"}
        player.uno_called = True
        return {"success": True, "message": f"{player.nickname} 喊了 UNO！"}

    def catch_uno(self, target_player: Player, caller_player: Player) -> dict:
        if target_player not in self.players or caller_player not in self.players:
            return {"success": False, "message": "玩家不存在"}
        if target_player == caller_player:
            return {"success": False, "message": "不能抓自己"}
        if len(target_player.hand) != 1:
            return {"success": False, "message": "该玩家手牌不是1张，不能抓"}
        if target_player.uno_called:
            return {"success": False, "message": "该玩家已经喊过UNO"}
        for _ in range(2):
            target_player.hand.append(self._draw_card())
        target_player.uno_called = True
        return {"success": True, "message": f"{caller_player.nickname} 抓了 {target_player.nickname} 未喊UNO，罚摸2张"}

    def get_game_state(self) -> dict:
        current = self.get_current_player()
        return {
            "room_id": self.room_id,
            "state": self.state.value,
            "players": [p.to_dict() for p in self.players],
            "current_player": current.to_dict() if current else None,
            "current_color": self.current_color.value if self.current_color else None,
            "top_card": self.discard_pile[-1].to_dict() if self.discard_pile else None,
            "direction": "clockwise" if self.direction == 1 else "counter-clockwise",
            "deck_count": len(self.deck),
            "pending_draw": self.pending_draw,
            "winner": self.winner.nickname if self.winner else None,
        }

    def get_player_hand(self, player: Player) -> List[dict]:
        if player not in self.players:
            return []
        return [card.to_dict() for card in player.hand]