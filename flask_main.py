import asyncio
import sys
import time
from typing import Dict
from flask import Flask, jsonify, request, render_template
from flask_socketio import SocketIO, emit, join_room, leave_room
from uno import UnoGame, Player, CardType, Color, GameState

# === Windows 事件循环兼容性 ===
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

app = Flask(__name__)
app.config['SECRET_KEY'] = 'uno_secret_key'
socketio = SocketIO(app, cors_allowed_origins="*")

uno_rooms: Dict[str, UnoGame] = {}
matching_pools = {}
response_waiting = {}
restart_votes = {}
user_sid_map = {}

# 🚨 新增：好友房准备状态 & 计分板
room_ready = {}  # room_id -> { user_id: bool }
room_scoreboard = {}  # room_id -> { "scores": {user_id: int}, "rounds": int }


# 🚨 优化版：定期清理闲置缓存房间和所有关联资源
def cleanup_old_rooms():
    import time
    while True:
        time.sleep(600)  # 每 10 分钟清理一次
        to_delete = []
        for room_id, game in list(uno_rooms.items()):
            # 如果游戏已经结束，并且没人发起“再来一局”的投票
            if game.state == GameState.FINISHED and room_id not in restart_votes:
                to_delete.append(room_id)

        # 执行彻底删除（连带清理附属数据）
        for room_id in to_delete:
            if room_id in uno_rooms:
                del uno_rooms[room_id]

                # 🚨 优化：清除与该房间绑定的所有周边数据，防内存泄漏
                if room_id in room_scoreboard:
                    del room_scoreboard[room_id]
                if room_id in room_ready:
                    del room_ready[room_id]
                if room_id in response_waiting:
                    del response_waiting[room_id]

                print(f"[系统自动清理] 已彻底清理闲置房间 {room_id} 及相关数据")


# ===== 计分工具函数 =====
def calculate_and_apply_scores(room_id):
    game = uno_rooms.get(room_id)
    if not game: return

    # 获取当前局最后的玩家列表和离场列表（在 uno.py 中通过 play_card 处理完毕）
    # 注意：此时 game.players 是剩下的玩家，game.winners 是离场/获胜玩家
    if room_id not in room_scoreboard:
        room_scoreboard[room_id] = {"scores": {}, "rounds": 0}
    stats = room_scoreboard[room_id]
    current_round = stats["rounds"] + 1

    # 🚨 分数计算逻辑核心
    # 如果是 2 人模式，正常结束
    if game.max_players == 2:
        players_data = [(p.player_id, p.nickname) for p in game.players]
        # 2人模式下，赢家是 winner
        if game.winner:
            winner_id = game.winner.player_id
            stats["scores"][winner_id] = stats["scores"].get(winner_id, 0) + 8
    else:
        # 多人模式：winners 列表中包含了前两名
        # 注意：按照 UNO 规则，游戏结束时 winners 里必有两人。
        scores_map = {}
        if game.winners:
            # 第一名（离场的人）得 8 分
            winner_1 = game.winners[0]
            scores_map[winner_1.player_id] = 8
            # 第二名（离场的人）得 5 分
            if len(game.winners) >= 2:
                winner_2 = game.winners[1]
                scores_map[winner_2.player_id] = 5

            # 剩下的人：找出手牌最少的 (即当前 game.players 中 hand 最少的人)
            if game.players:
                min_hand = min([len(p.hand) for p in game.players])
                for p in game.players:
                    if len(p.hand) == min_hand:
                        scores_map[p.player_id] = 3

            # 写入总榜
            for pid, score in scores_map.items():
                stats["scores"][pid] = stats["scores"].get(pid, 0) + score

    stats["rounds"] = current_round

    # 是否达到 4 局？
    if stats["rounds"] >= 4:
        emit('scoreboard_update', {'scores': stats["scores"], 'reset': True}, room=room_id)
        # 重置 4 局内的计分
        stats["scores"] = {}
        stats["rounds"] = 0
    else:
        emit('scoreboard_update', {'scores': stats["scores"], 'reset': False}, room=room_id)


# ===== 广播匹配池状态 =====
def broadcast_pool_status(expected_players: int):
    if expected_players not in matching_pools:
        return
    pool = matching_pools[expected_players]
    current_count = len(pool)
    for player in pool:
        emit('match_pool_update', {
            'expected': expected_players,
            'current': current_count
        }, to=player['sid'])


# ===== HTTP 接口 =====
@app.route('/')
def root():
    return render_template('game_client.html')


@app.route('/rooms')
def list_rooms():
    return jsonify({
        "total_rooms": len(uno_rooms),
        "rooms": list(uno_rooms.keys())
    })


# ===== SocketIO 事件 =====
@socketio.on('connect')
def handle_connect():
    print(f"[连接] 客户端 {request.sid} 已连接")
    emit('connected', {'sid': request.sid})


@socketio.on('identify')
def handle_identify(data):
    sid = request.sid
    user_id = data.get('user_id')
    if not user_id:
        return
    user_sid_map[user_id] = sid
    print(f"[IDENTIFY] 收到标识: user_id={user_id}, sid={sid}")
    found = False
    for room_id, game in uno_rooms.items():
        player = next((p for p in game.players if p.player_id == user_id), None)
        if player:
            found = True
            player.sid = sid
            print(f"[重连] 玩家 {user_id} 重连成功，房间 {room_id}")
            if game.state == GameState.FINISHED:
                emit('game_over', {'winner': game.winner.nickname if game.winner else '未知'}, to=sid)
                return
            state = game.get_game_state()
            emit('game_state', state, to=sid)
            hand = game.get_player_hand(player)
            emit('your_hand', {'hand': hand}, to=sid)
            if room_id in response_waiting and response_waiting[room_id]['player_sid'] == sid:
                emit('response_required', {
                    'room_id': room_id,
                    'available_indices': response_waiting[room_id]['available_indices'],
                    'pending_draw': game.pending_draw
                }, to=sid)
            break
    if not found:
        print(f"[IDENTIFY] 新玩家 {user_id}，未找到已有游戏")


@socketio.on('disconnect')
def handle_disconnect():
    sid = request.sid
    print(f"[断开] 客户端 {sid} 已断开")


# ===== 开黑模式 =====
@socketio.on('uno_create_room')
def handle_create_room(data):
    sid = request.sid
    user_id = data.get('user_id')
    nickname = data.get('nickname', '玩家')
    if not user_id:
        emit('error', {'message': '缺少 user_id'}, to=sid)
        return
    room_id = f"uno_{user_id}_{int(time.time())}"
    game = UnoGame(room_id)
    player = Player(user_id, nickname, sid)
    game.add_player(player)
    uno_rooms[room_id] = game
    room_ready[room_id] = {user_id: True}  # 房主自己默认为“已准备”
    join_room(room_id)
    emit('room_created', {'room_id': room_id, 'host_id': user_id}, to=sid)
    print(f"[UNO] 房间 {room_id} 创建，玩家: {nickname}")


@socketio.on('uno_join_room')
def handle_join_room(data):
    sid = request.sid
    user_id = data.get('user_id')
    nickname = data.get('nickname', '玩家')
    room_id = data.get('room_id')
    if room_id not in uno_rooms:
        emit('error', {'message': '房间不存在'}, to=sid)
        return
    game = uno_rooms[room_id]
    player = Player(user_id, nickname, sid)
    if game.add_player(player):
        join_room(room_id)
        # 初始化准备状态
        if room_id not in room_ready:
            room_ready[room_id] = {}
        room_ready[room_id][user_id] = False

        emit('player_joined', {
            'room_id': room_id,
            'player': player.to_dict(),
            'is_host': False
        }, room=room_id)
        print(f"[UNO] {nickname} 加入房间 {room_id}")
        # 广播状态
        emit('room_player_ready_update', room_ready[room_id], room=room_id)
    else:
        emit('error', {'message': '房间已满或玩家已存在'}, to=sid)


@socketio.on('player_ready')
def handle_player_ready(data):
    """玩家点击准备"""
    room_id = data.get('room_id')
    user_id = data.get('user_id')
    if room_id not in room_ready:
        return
    room_ready[room_id][user_id] = True
    # 广播所有准备状态
    emit('room_player_ready_update', room_ready[room_id], room=room_id)


@socketio.on('uno_start_game')
def handle_start_game(data):
    sid = request.sid
    room_id = data.get('room_id')
    if room_id not in uno_rooms:
        emit('error', {'message': '房间不存在'}, to=sid)
        return
    game = uno_rooms[room_id]

    # 校验准备状态（开黑模式下）
    if room_id in room_ready:
        host_id = game.players[0].player_id if game.players else None
        # 所有人都必须准备，而且只要有一人没准备就不行
        all_ready = all(room_ready[room_id].values())
        if not all_ready:
            emit('error', {'message': '有好友还未准备，请等待'}, to=sid)
            return

    if game.start_game():
        # 重置准备状态
        if room_id in room_ready:
            room_ready[room_id] = {}
        state = game.get_game_state()
        emit('game_started', state, room=room_id)
        for player in game.players:
            hand = game.get_player_hand(player)
            emit('your_hand', {'hand': hand}, to=player.sid)
        print(f"[UNO] 房间 {room_id} 游戏开始")
    else:
        emit('error', {'message': '至少需要2名玩家才能开始'}, to=sid)


# ===== 匹配模式 =====
@socketio.on('match_start')
def handle_match_start(data):
    sid = request.sid
    user_id = data.get('user_id')
    nickname = data.get('nickname', '玩家')
    expected_players = data.get('expected_players', 2)
    if not user_id: return
    for exp, pool in matching_pools.items():
        for p in pool:
            if p['user_id'] == user_id:
                emit('match_error', {'message': '您已在匹配队列中'}, to=sid)
                return
    for room in uno_rooms.values():
        if any(p.player_id == user_id for p in room.players):
            emit('match_error', {'message': '您已在游戏中，不能重复匹配'}, to=sid)
            return
    if expected_players not in matching_pools:
        matching_pools[expected_players] = []
    pool = matching_pools[expected_players]
    pool.append({'sid': sid, 'user_id': user_id, 'nickname': nickname})
    emit('match_status', {'status': 'matching', 'expected': expected_players, 'pool_size': len(pool)}, to=sid)
    print(f"[匹配] {nickname} 加入 {expected_players}人场 (当前池子人数: {len(pool)}/{expected_players})")
    broadcast_pool_status(expected_players)

    if len(pool) >= expected_players:
        matched_players = [pool.pop(0) for _ in range(expected_players)]
        room_id = f"match_{int(time.time())}"
        game = UnoGame(room_id, max_players=expected_players)
        players = []
        ok = True
        current_sids = {}
        for p in matched_players:
            current_sid = user_sid_map.get(p['user_id'])
            if not current_sid:
                print(f"[匹配] ❌ 玩家 {p['user_id']} 已断开连接，匹配失败！")
                ok = False
                break
            current_sids[p['user_id']] = current_sid
        if ok:
            for p in matched_players:
                current_sid = current_sids[p['user_id']]
                player = Player(p['user_id'], p['nickname'], current_sid)
                if not game.add_player(player):
                    ok = False
                    break
                join_room(room_id, sid=current_sid)
                players.append(player)
        if not ok:
            for p in matched_players:
                pool.append(p)
            emit('match_error', {'message': '匹配失败，玩家已断开连接或状态异常，请重试'}, to=sid)
            broadcast_pool_status(expected_players)
            return
        uno_rooms[room_id] = game
        if game.start_game():
            state = game.get_game_state()
            emit('game_started', state, room=room_id)
            for player in game.players:
                hand = game.get_player_hand(player)
                emit('your_hand', {'hand': hand}, to=player.sid)
            print(f"[匹配] ✅ {expected_players}人场匹配成功！房间 {room_id}")
        else:
            del uno_rooms[room_id]
            for p in matched_players: leave_room(room_id, sid=p['sid'])
            emit('match_error', {'message': '开局失败，请重试'}, to=sid)
            print(f"[DEBUG] 游戏启动失败，已清理房间 {room_id}")


@socketio.on('match_cancel')
def handle_match_cancel(data):
    sid = request.sid
    user_id = data.get('user_id')
    if not user_id: return
    for expected, pool in list(matching_pools.items()):
        for i, p in enumerate(pool):
            if p['user_id'] == user_id:
                del pool[i]
                emit('match_status', {'status': 'canceled'}, to=sid)
                broadcast_pool_status(expected)
                if not pool: del matching_pools[expected]
                return
    emit('match_error', {'message': '您不在匹配队列中'}, to=sid)


# ===== 游戏操作 =====
@socketio.on('uno_play_card')
def handle_play_card(data):
    sid = request.sid
    room_id = data.get('room_id')
    user_id = data.get('user_id')
    card_index = data.get('card_index')
    chosen_color = data.get('chosen_color')

    if room_id not in uno_rooms:
        emit('private_log', {'message': '房间不存在'}, to=sid)
        return
    game = uno_rooms[room_id]
    player = next((p for p in game.players if p.player_id == user_id), None)
    if not player:
        emit('private_log', {'message': '你不在这个房间中'}, to=sid)
        return
    if room_id in response_waiting:
        emit('private_log', {'message': '当前正在等待其他玩家响应 +2/+4'}, to=sid)
        return

    result = game.play_card(player, card_index, chosen_color)
    played_card = result.get('card')

    # 🚨 修复：如果出牌失败，必须把牌还给客户端！
    if not result.get('success'):
        # 1. 仅提示当事人（不广播）
        emit('private_log', {'message': result.get('message')}, to=sid)
        # 2. 重新发送该玩家的手牌数据，让前端恢复刚刚消失的这张牌！
        hand = game.get_player_hand(player)
        emit('your_hand', {'hand': hand}, to=sid)
        return  # 注意这里必须结束函数执行，不往下走

    # 只有成功才会走到下面（广播给全房间）
    if 'card' in result and hasattr(result['card'], 'to_dict'):
        result['card'] = result['card'].to_dict()
    emit('card_played', result, room=room_id)

    # 🚨 多人淘汰模式：若有玩家离场，通知前端移除他
    if result.get('is_exit_mode') and result.get('exited_players'):
        emit('player_left', {'exited_players': result['exited_players']}, room=room_id)

    if result.get('auto_draw'):
        emit('system_toast', {'message': result['message']}, to=player.sid)

    if result.get('game_over'):
        winner = result.get('winner')
        emit('game_over', {'winner': winner, 'message': f'游戏结束，胜者：{winner}'}, room=room_id)
        # 🚨 对局结束触发计分
        calculate_and_apply_scores(room_id)
        return

    state = game.get_game_state()
    emit('game_state', state, room=room_id)

    if played_card and played_card.card_type in (CardType.DRAW_TWO, CardType.WILD_DRAW_FOUR):
        initiate_response_phase(room_id)
    else:
        for p in game.players:
            emit('your_hand', {'hand': game.get_player_hand(p)}, to=p.sid)

    if result.get('forgot_uno'):
        if room_id not in response_waiting:
            response_waiting[room_id] = {}
        response_waiting[room_id]['uno_forgot_timer'] = True
        emit('uno_forgot_warning', {'room_id': room_id}, to=player.sid)

        # 🚨 核心修复：把当前玩家的ID传给后台线程，用于3秒后锁定“已喊UNO”状态
        forgotten_player_id = player.player_id

        def advance_turn_after_3s(forgotten_player_id):
            import time
            time.sleep(3)
            if room_id in uno_rooms and game.state == GameState.PLAYING:
                if room_id in response_waiting and response_waiting[room_id].get('uno_forgot_timer'):
                    del response_waiting[room_id]

                    # 1. 逃过一劫，强制该玩家进入“已喊UNO”状态，让其他玩家抓按钮消失
                    for p in game.players:
                        if p.player_id == forgotten_player_id:
                            p.uno_called = True
                            break

                    # 2. 正常切换到下家
                    game._next_turn()
                    next_state = game.get_game_state()
                    socketio.emit('game_state', next_state, room=room_id, namespace='/')

                    # 3. 如果打出的牌是 +2 或 +4，必须在后台线程里面手动处理响应（绝不调用外部函数）
                    if played_card and played_card.card_type in (CardType.DRAW_TWO, CardType.WILD_DRAW_FOUR):
                        current_p = game.get_current_player()
                        if current_p:
                            top_card = game.discard_pile[-1] if game.discard_pile else None
                            if top_card:
                                if top_card.card_type == CardType.DRAW_TWO:
                                    response_types = [CardType.DRAW_TWO, CardType.WILD_DRAW_FOUR]
                                elif top_card.card_type == CardType.WILD_DRAW_FOUR:
                                    response_types = [CardType.WILD_DRAW_FOUR]
                                else:
                                    return
                                available_indices = []
                                for i, c in enumerate(current_p.hand):
                                    if c.card_type in response_types:
                                        available_indices.append(i)
                                if available_indices:
                                    response_waiting[room_id] = {
                                        'player_sid': current_p.sid,
                                        'available_indices': available_indices,
                                        'game': game
                                    }
                                    socketio.emit('response_required', {
                                        'room_id': room_id,
                                        'available_indices': available_indices,
                                        'pending_draw': game.pending_draw
                                    }, to=current_p.sid, namespace='/')
                                    print(f"[响应] 玩家 {current_p.nickname} 有响应牌，等待选择")
                                else:
                                    # 下家无响应牌，直接罚下家的牌（完美解决+2自摸）
                                    count = game.pending_draw
                                    game.pending_draw = 0
                                    drawn_cards = [game._draw_card() for _ in range(count)]
                                    current_p.hand.extend(drawn_cards)
                                    game._next_turn()
                                    next_state = game.get_game_state()
                                    socketio.emit('game_state', next_state, room=room_id, namespace='/')
                                    for p in game.players:
                                        hand = game.get_player_hand(p)
                                        socketio.emit('your_hand', {'hand': hand}, to=p.sid, namespace='/')
                                    socketio.emit('card_drawn', {
                                        'message': f'{current_p.nickname} 未出响应牌，罚摸 {count} 张并跳过回合'},
                                                  room=room_id, namespace='/')

        # 🚨 将玩家ID传入后台线程
        socketio.start_background_task(lambda: advance_turn_after_3s(forgotten_player_id))
    else:
        # 正常流程（没有忘记喊UNO）
        if played_card and played_card.card_type in (CardType.DRAW_TWO, CardType.WILD_DRAW_FOUR):
            initiate_response_phase(room_id)
        else:
            for p in game.players:
                emit('your_hand', {'hand': game.get_player_hand(p)}, to=p.sid)


@socketio.on('uno_draw_card')
def handle_draw_card(data):
    sid = request.sid
    room_id = data.get('room_id')
    user_id = data.get('user_id')
    if room_id not in uno_rooms: return
    game = uno_rooms[room_id]
    player = next((p for p in game.players if p.player_id == user_id), None)
    if not player:
        emit('private_log', {'message': '找不到该玩家'}, to=sid)
        return
    if room_id in response_waiting:
        emit('private_log', {'message': '当前处于等待响应阶段，不能强制抽牌！'}, to=sid)
        return

    if game.pending_draw > 0:
        count = game.pending_draw
        game.pending_draw = 0
        drawn_cards = [game._draw_card() for _ in range(count)]
        player.hand.extend(drawn_cards)
        game._next_turn()
        state = game.get_game_state()
        emit('game_state', state, room=room_id)
        for p in game.players:
            emit('your_hand', {'hand': game.get_player_hand(p)}, to=p.sid)
        emit('card_drawn', {'message': f'{player.nickname} 抽了 {count} 张牌'}, room=room_id)
    else:
        result = game.draw_cards(player)
        emit('card_drawn', result, room=room_id)
        state = game.get_game_state()
        emit('game_state', state, room=room_id)
        for p in game.players: emit('your_hand', {'hand': game.get_player_hand(p)}, to=p.sid)


@socketio.on('uno_call_uno')
def handle_call_uno(data):
    sid = request.sid
    room_id = data.get('room_id')
    user_id = data.get('user_id')
    if room_id not in uno_rooms:
        emit('private_log', {'message': '房间不存在'}, to=sid)
        return
    game = uno_rooms[room_id]
    player = next((p for p in game.players if p.player_id == user_id), None)
    if not player:
        emit('private_log', {'message': '你不是本房间玩家'}, to=sid)
        return
    result = game.call_uno(player)
    if not result.get('success'):
        emit('private_log', {'message': result.get('message')}, to=sid)
        return
    emit('uno_called', result, room=room_id)
    state = game.get_game_state()
    emit('game_state', state, room=room_id)


@socketio.on('catch_uno')
def handle_catch_uno(data):
    sid = request.sid
    room_id = data.get('room_id')
    target_user_id = data.get('target_user_id')

    # 抓人期间3秒倒计时逻辑（保留原样，不用动）
    if room_id in response_waiting and response_waiting[room_id].get('uno_forgot_timer'):
        del response_waiting[room_id]
        game = uno_rooms[room_id]
        if game.state == GameState.PLAYING:
            for p in game.players:
                if p.player_id == target_user_id:
                    p.uno_called = True
                    break
            game._next_turn()
            state = game.get_game_state()
            emit('game_state', state, room=room_id)

    # 🚨 错误拦截：只发给个人
    if room_id not in uno_rooms:
        emit('private_log', {'message': '房间不存在'}, to=sid)
        return
    game = uno_rooms[room_id]
    caller = next((p for p in game.players if p.sid == sid), None)
    if not caller:
        emit('private_log', {'message': '你不在这个房间'}, to=sid)
        return
    target = next((p for p in game.players if p.player_id == target_user_id), None)
    if not target:
        emit('private_log', {'message': '目标玩家不存在'}, to=sid)
        return
    result = game.catch_uno(target, caller)
    if not result.get('success'):
        emit('private_log', {'message': result.get('message')}, to=sid)
        return
    emit('uno_caught', result, room=room_id)
    state = game.get_game_state()
    emit('game_state', state, room=room_id)
    for p in game.players: emit('your_hand', {'hand': game.get_player_hand(p)}, to=p.sid)

# ===== 响应阶段逻辑 =====
def initiate_response_phase(room_id):
    game = uno_rooms.get(room_id)
    if not game: return
    current_player = game.get_current_player()
    if not current_player: return
    top_card = game.discard_pile[-1] if game.discard_pile else None
    if not top_card: return
    if top_card.card_type == CardType.DRAW_TWO:
        response_types = [CardType.DRAW_TWO, CardType.WILD_DRAW_FOUR]
    elif top_card.card_type == CardType.WILD_DRAW_FOUR:
        response_types = [CardType.WILD_DRAW_FOUR]
    else:
        return
    available_indices = []
    for i, card in enumerate(current_player.hand):
        if card.card_type in response_types:
            available_indices.append(i)
    if available_indices:
        response_waiting[room_id] = {
            'player_sid': current_player.sid,
            'available_indices': available_indices,
            'game': game
        }
        emit('response_required', {
            'room_id': room_id,
            'available_indices': available_indices,
            'pending_draw': game.pending_draw
        }, to=current_player.sid)
        print(f"[响应] 玩家 {current_player.nickname} 有响应牌，等待选择")
    else:
        apply_penalty_and_skip(room_id)


def apply_penalty_and_skip(room_id):
    game = uno_rooms.get(room_id)
    if not game: return
    current = game.get_current_player()
    if not current: return
    count = game.pending_draw
    game.pending_draw = 0
    drawn_cards = [game._draw_card() for _ in range(count)]
    current.hand.extend(drawn_cards)
    game._next_turn()
    state = game.get_game_state()
    emit('game_state', state, room=room_id)
    for p in game.players: emit('your_hand', {'hand': game.get_player_hand(p)}, to=p.sid)
    emit('card_drawn', {'message': f'{current.nickname} 未出响应牌，罚摸 {count} 张并跳过回合'}, room=room_id)


@socketio.on('player_response')
def handle_player_response(data):
    sid = request.sid
    room_id = data.get('room_id')
    action = data.get('action')
    card_index = data.get('card_index')

    if room_id not in response_waiting:
        emit('private_log', {'message': '当前没有等待响应的阶段'}, to=sid)
        return
    info = response_waiting[room_id]
    if info['player_sid'] != sid:
        emit('private_log', {'message': '不是你的响应回合'}, to=sid)
        return

    game = info['game']
    player = next((p for p in game.players if p.sid == sid), None)
    if not player:
        del response_waiting[room_id]
        return

    if action == 'play' and card_index is not None:
        if card_index not in info['available_indices']:
            emit('private_log', {'message': '选择的牌不是响应牌'}, to=sid)
            return

        card = player.hand[card_index]
        player.hand.pop(card_index)
        game.discard_pile.append(card)
        game._apply_card_effect(card)
        chosen_color = data.get('chosen_color')
        if card.color == Color.WILD and chosen_color:
            game.current_color = Color(chosen_color)
        elif card.color != Color.WILD:
            game.current_color = card.color
        del response_waiting[room_id]

        # 🚨 完整修复：响应成功后必须调用下一回合，否则死循环
        game._next_turn()

        emit('card_played', {'success': True, 'message': f'{player.nickname} 打出 {card}'}, room=room_id)
        state = game.get_game_state()
        emit('game_state', state, room=room_id)
        for p in game.players: emit('your_hand', {'hand': game.get_player_hand(p)}, to=p.sid)
        top_card = game.discard_pile[-1] if game.discard_pile else None
        if top_card and top_card.card_type in (CardType.DRAW_TWO, CardType.WILD_DRAW_FOUR):
            initiate_response_phase(room_id)


    elif action == 'pass':

        del response_waiting[room_id]

        apply_penalty_and_skip(room_id)

    else:

        emit('private_log', {'message': '无效的操作'}, to=sid)


# ===== 再来一局 =====
@socketio.on('restart_invite')
def handle_restart_invite(data):
    sid = request.sid
    room_id = data.get('room_id')
    if room_id not in uno_rooms: return
    game = uno_rooms[room_id]
    if game.state != GameState.FINISHED: return
    if room_id not in restart_votes:
        restart_votes[room_id] = set()
    restart_votes[room_id].add(sid)
    emit('restart_invite', {'inviter_sid': sid}, room=room_id)


@socketio.on('restart_accept')
def handle_restart_accept(data):
    sid = request.sid
    room_id = data.get('room_id')
    if room_id not in uno_rooms: return
    game = uno_rooms[room_id]
    if game.state != GameState.FINISHED: return
    if room_id not in restart_votes:
        restart_votes[room_id] = set()
    restart_votes[room_id].add(sid)
    all_players_sids = {p.sid for p in game.players}
    if restart_votes[room_id] == all_players_sids:
        winner_id = game.winner.player_id if game.winner else None
        if game.reset_game(winner_id):
            state = game.get_game_state()
            emit('game_started', state, room=room_id)
            for player in game.players: emit('your_hand', {'hand': game.get_player_hand(player)}, to=player.sid)
            del restart_votes[room_id]
        else:
            emit('error', {'message': '重置游戏失败'}, to=sid)
    else:
        emit('restart_accept_ok', {'accepted': True}, to=sid)


if __name__ == '__main__':
    print("=" * 50)
    print("  UNO 游戏服务器启动中...")
    print("  地址: http://127.0.0.1:8000")
    print("=" * 50)
    # 🚨 启动后台清理线程
    socketio.start_background_task(cleanup_old_rooms)
    socketio.run(app, host='0.0.0.0', port=8000, debug=False, allow_unsafe_werkzeug=True)