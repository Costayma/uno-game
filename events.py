"""UNO 游戏的 WebSocket 事件处理"""
import time

def register_uno_events(sio):
    """注册所有 UNO 相关的 SocketIO 事件"""
    from uno.uno import UnoGame, Player, Color

    # 存储 UNO 房间（用函数属性存储）
    if not hasattr(register_uno_events, 'rooms'):
        register_uno_events.rooms = {}

    @sio.event
    async def uno_create_room(sid, data):
        user_id = data.get("user_id")
        nickname = data.get("nickname", "玩家")
        room_id = f"uno_{user_id}_{int(time.time())}"
        game = UnoGame(room_id)
        player = Player(user_id, nickname, sid)
        game.add_player(player)
        register_uno_events.rooms[room_id] = game
        await sio.emit("room_created", {"room_id": room_id}, to=sid)
        print(f"[UNO] 房间 {room_id} 创建，玩家: {nickname}")

    @sio.event
    async def uno_join_room(sid, data):
        user_id = data.get("user_id")
        nickname = data.get("nickname", "玩家")
        room_id = data.get("room_id")
        if room_id not in register_uno_events.rooms:
            await sio.emit("error", {"message": "房间不存在"}, to=sid)
            return
        game = register_uno_events.rooms[room_id]
        player = Player(user_id, nickname, sid)
        if game.add_player(player):
            await sio.emit("player_joined", {
                "room_id": room_id,
                "player": player.to_dict()
            }, room=room_id)
            print(f"[UNO] {nickname} 加入房间 {room_id}")
        else:
            await sio.emit("error", {"message": "房间已满"}, to=sid)

    @sio.event
    async def uno_start_game(sid, data):
        room_id = data.get("room_id")
        if room_id not in register_uno_events.rooms:
            await sio.emit("error", {"message": "房间不存在"}, to=sid)
            return
        game = register_uno_events.rooms[room_id]
        if game.start_game():
            state = game.get_game_state()
            await sio.emit("game_started", state, room=room_id)
            for player in game.players:
                hand = game.get_player_hand(player)
                await sio.emit("your_hand", {"hand": hand}, to=player.sid)
            print(f"[UNO] 房间 {room_id} 游戏开始")
        else:
            await sio.emit("error", {"message": "至少需要2人才能开始"}, to=sid)

    @sio.event
    async def uno_play_card(sid, data):
        room_id = data.get("room_id")
        user_id = data.get("user_id")
        card_index = data.get("card_index")
        chosen_color = data.get("chosen_color")
        if room_id not in register_uno_events.rooms:
            return
        game = register_uno_events.rooms[room_id]
        player = next((p for p in game.players if p.player_id == user_id), None)
        if not player:
            return
        result = game.play_card(player, card_index, chosen_color)
        await sio.emit("card_played", result, room=room_id)
        if result.get("game_over"):
            await sio.emit("game_over", result, room=room_id)
        else:
            state = game.get_game_state()
            await sio.emit("game_state", state, room=room_id)
            current = game.get_current_player()
            if current:
                hand = game.get_player_hand(current)
                await sio.emit("your_hand", {"hand": hand}, to=current.sid)

    @sio.event
    async def uno_draw_card(sid, data):
        room_id = data.get("room_id")
        user_id = data.get("user_id")
        if room_id not in register_uno_events.rooms:
            return
        game = register_uno_events.rooms[room_id]
        player = next((p for p in game.players if p.player_id == user_id), None)
        if not player:
            return
        result = game.draw_cards(player)
        await sio.emit("card_drawn", result, room=room_id)
        state = game.get_game_state()
        await sio.emit("game_state", state, room=room_id)
        current = game.get_current_player()
        if current:
            hand = game.get_player_hand(current)
            await sio.emit("your_hand", {"hand": hand}, to=current.sid)

    @sio.event
    async def uno_call_uno(sid, data):
        room_id = data.get("room_id")
        user_id = data.get("user_id")
        if room_id not in register_uno_events.rooms:
            return
        game = register_uno_events.rooms[room_id]
        player = next((p for p in game.players if p.player_id == user_id), None)
        if not player:
            return
        result = game.call_uno(player)
        await sio.emit("uno_called", result, room=room_id)