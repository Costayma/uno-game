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
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')

uno_rooms: Dict[str, UnoGame] = {}
matching_pools = {}  # 匹配池
response_waiting = {}  # 等待响应状态
restart_votes = {}  # 再来一局投票: room_id -> set of player_ids
user_sid_map = {}  # user_id -> sid

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
    """客户端标识自己的 user_id"""
    sid = request.sid
    user_id = data.get('user_id')
    if not user_id:
        return
    # 存储映射
    user_sid_map[user_id] = sid
    print(f"[IDENTIFY] 收到标识: user_id={user_id}, sid={sid}")
    print(f"[IDENTIFY] 当前房间数: {len(uno_rooms)}")
    found = False
    # 尝试查找该玩家是否已在某个游戏中
    for room_id, game in uno_rooms.items():
        player = next((p for p in game.players if p.player_id == user_id), None)
        if player:
            found = True
            # 更新玩家的 sid
            player.sid = sid
            print(f"[重连] 玩家 {user_id} 重连成功，房间 {room_id}，游戏状态 {game.state}")
            # 如果游戏已结束，发送 game_over
            if game.state == GameState.FINISHED:
                emit('game_over', {'winner': game.winner.nickname if game.winner else '未知'}, to=sid)
                # 可选：发送再来一局按钮状态
                return
            # 如果游戏进行中，发送当前状态
            state = game.get_game_state()
            emit('game_state', state, to=sid)
            hand = game.get_player_hand(player)
            emit('your_hand', {'hand': hand}, to=sid)
            # 如果是当前回合且等待响应，检查是否处于响应阶段
            if room_id in response_waiting and response_waiting[room_id]['player_sid'] == sid:
                # 通知前端进入响应阶段
                emit('response_required', {
                    'room_id': room_id,
                    'available_indices': response_waiting[room_id]['available_indices'],
                    'pending_draw': game.pending_draw
                }, to=sid)
            break
        if not found:
            print(f"[IDENTIFY] 新玩家 {user_id}，未找到已有游戏")
            # 可在此给前端发送一个“新玩家”标识，但不需要额外操作

@socketio.on('disconnect')
def handle_disconnect():
    sid = request.sid
    # 从 user_sid_map 中移除该 sid（但保留 user_id 映射可能更好，因为重连时还会使用）
    # 我们可以不删除，只打印
    print(f"[断开] 客户端 {sid} 已断开")
    # 如果希望清理超时玩家，可以启动定时器，但暂不实现

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
    join_room(room_id)
    emit('room_created', {'room_id': room_id}, to=sid)
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
        emit('player_joined', {
            'room_id': room_id,
            'player': player.to_dict()
        }, room=room_id)
        print(f"[UNO] {nickname} 加入房间 {room_id}")
        if len(game.players) >= 2:
            handle_start_game({'room_id': room_id})
    else:
        emit('error', {'message': '房间已满或玩家已存在'}, to=sid)

@socketio.on('uno_start_game')
def handle_start_game(data):
    sid = request.sid
    room_id = data.get('room_id')
    if room_id not in uno_rooms:
        emit('error', {'message': '房间不存在'}, to=sid)
        return
    game = uno_rooms[room_id]
    if game.start_game():
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

    # ---- 参数校验 ----
    if not user_id:
        emit('error', {'message': '缺少 user_id'}, to=sid)
        return
    if not isinstance(expected_players, int) or expected_players < 2 or expected_players > 10:
        emit('match_error', {'message': '期望人数必须在2-10之间'}, to=sid)
        return

    # ---- 检查是否已在匹配队列（用 user_id） ----
    for exp, pool in matching_pools.items():
        for p in pool:
            if p['user_id'] == user_id:
                emit('match_error', {'message': '您已在匹配队列中'}, to=sid)
                return

    # ---- 检查是否已在游戏中 ----
    for room in uno_rooms.values():
        if any(p.player_id == user_id for p in room.players):
            emit('match_error', {'message': '您已在游戏中，不能重复匹配'}, to=sid)
            return

    # ---- 获取或创建匹配池 ----
    if expected_players not in matching_pools:
        matching_pools[expected_players] = []
    pool = matching_pools[expected_players]   # 此时 pool 一定存在

    # ---- 加入匹配池 ----
    pool.append({
        'sid': sid,
        'user_id': user_id,
        'nickname': nickname
    })
    emit('match_status', {'status': 'matching', 'expected': expected_players, 'pool_size': len(pool)}, to=sid)
    print(f"[匹配] {nickname} 加入 {expected_players}人场 (当前池子人数: {len(pool)}/{expected_players})")
    broadcast_pool_status(expected_players)

    # ---- 检查是否凑满 ----
    if len(pool) >= expected_players:
        matched_players = [pool.pop(0) for _ in range(expected_players)]
        room_id = f"match_{int(time.time())}"
        game = UnoGame(room_id, max_players=expected_players)
        players = []
        ok = True
        for p in matched_players:
            # 获取最新的 sid
            current_sid = user_sid_map.get(p['user_id'], p['sid'])
            player = Player(p['user_id'], p['nickname'], current_sid)
            if not game.add_player(player):
                ok = False
                break
            join_room(room_id, sid=current_sid)
            players.append(player)

        if not ok:
            # 回退
            for p in matched_players:
                pool.append(p)
            emit('match_error', {'message': '玩家添加失败，请重试'}, to=sid)
            broadcast_pool_status(expected_players)
            return

        uno_rooms[room_id] = game
        print(f"[DEBUG] 开始游戏，玩家数量: {len(game.players)}")

        if game.start_game():
            state = game.get_game_state()
            emit('game_started', state, room=room_id)
            for player in game.players:
                hand = game.get_player_hand(player)
                emit('your_hand', {'hand': hand}, to=player.sid)
            nicknames = ', '.join(p['nickname'] for p in matched_players)
            print(f"[匹配] ✅ {expected_players}人场匹配成功！房间 {room_id}，玩家: {nicknames}")
        else:
            # 开局失败，清理房间并将玩家放回池子
            del uno_rooms[room_id]
            for p in matched_players:
                leave_room(room_id, sid=p['sid'])
                pool.append(p)
            emit('match_error', {'message': '开局失败，请重试'}, to=sid)
            broadcast_pool_status(expected_players)
            print(f"[DEBUG] 游戏启动失败，已清理房间 {room_id}")

@socketio.on('match_cancel')
def handle_match_cancel(data):
    sid = request.sid
    user_id = data.get('user_id')
    if not user_id:
        emit('match_error', {'message': '缺少 user_id'}, to=sid)
        return

    for expected, pool in list(matching_pools.items()):
        for i, p in enumerate(pool):
            if p['user_id'] == user_id:
                del pool[i]
                emit('match_status', {'status': 'canceled', 'expected': expected, 'pool_size': len(pool)}, to=sid)
                broadcast_pool_status(expected)
                if not pool:
                    del matching_pools[expected]
                print(f"[匹配] 玩家 {user_id} 取消 {expected}人场匹配 (剩余人数: {len(pool)})")
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
        emit('error', {'message': '房间不存在'}, to=sid)
        return

    game = uno_rooms[room_id]
    player = next((p for p in game.players if p.player_id == user_id), None)
    if not player:
        emit('error', {'message': '你不在这个房间中'}, to=sid)
        return

    if room_id in response_waiting:
        emit('error', {'message': '当前正在等待其他玩家响应 +2/+4'}, to=sid)
        return

    result = game.play_card(player, card_index, chosen_color)
    played_card = result.get('card')

    if 'card' in result and hasattr(result['card'], 'to_dict'):
        result['card'] = result['card'].to_dict()

    emit('card_played', result, room=room_id)

    if result.get('game_over'):
        winner = result.get('winner')
        emit('game_over', {'winner': winner, 'message': f'游戏结束，胜者：{winner}'}, room=room_id)
        return

    # 广播最新状态
    state = game.get_game_state()
    emit('game_state', state, room=room_id)

    # 如果是 +2 或 +4，进入响应阶段
    if played_card and played_card.card_type in (CardType.DRAW_TWO, CardType.WILD_DRAW_FOUR):
        initiate_response_phase(room_id)
    else:
        # 更新所有玩家的手牌
        for p in game.players:
            emit('your_hand', {'hand': game.get_player_hand(p)}, to=p.sid)

        # 如果当前玩家手牌为1，提示喊UNO（但由前端控制按钮，后端只负责状态）
        # 这里无需额外操作

@socketio.on('uno_draw_card')
def handle_draw_card(data):
    sid = request.sid
    room_id = data.get('room_id')
    user_id = data.get('user_id')
    if room_id not in uno_rooms:
        return
    game = uno_rooms[room_id]
    player = next((p for p in game.players if p.player_id == user_id), None)
    if not player:
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
        for p in game.players:
            emit('your_hand', {'hand': game.get_player_hand(p)}, to=p.sid)

@socketio.on('uno_call_uno')
def handle_call_uno(data):
    sid = request.sid
    room_id = data.get('room_id')
    user_id = data.get('user_id')
    if room_id not in uno_rooms:
        return
    game = uno_rooms[room_id]
    player = next((p for p in game.players if p.player_id == user_id), None)
    if not player:
        return
    result = game.call_uno(player)
    emit('uno_called', result, room=room_id)
    # 更新玩家状态
    state = game.get_game_state()
    emit('game_state', state, room=room_id)

@socketio.on('catch_uno')
def handle_catch_uno(data):
    """抓未喊UNO"""
    sid = request.sid
    room_id = data.get('room_id')
    target_user_id = data.get('target_user_id')
    if room_id not in uno_rooms:
        emit('error', {'message': '房间不存在'}, to=sid)
        return
    game = uno_rooms[room_id]
    caller = next((p for p in game.players if p.sid == sid), None)
    if not caller:
        return
    target = next((p for p in game.players if p.player_id == target_user_id), None)
    if not target:
        emit('error', {'message': '目标玩家不存在'}, to=sid)
        return
    result = game.catch_uno(target, caller)
    emit('uno_caught', result, room=room_id)
    # 广播新状态
    state = game.get_game_state()
    emit('game_state', state, room=room_id)
    for p in game.players:
        emit('your_hand', {'hand': game.get_player_hand(p)}, to=p.sid)

# ===== 响应阶段逻辑 =====
def initiate_response_phase(room_id):
    """在打出 +2 或 +4 后调用，检查下家是否有响应牌，并通知前端"""
    game = uno_rooms.get(room_id)
    if not game:
        return
    if game.state != GameState.PLAYING:
        return

    current_player = game.get_current_player()
    if not current_player:
        return

    # ===== 根据弃牌堆顶牌决定响应类型（而不是 pending_draw） =====
    top_card = game.discard_pile[-1] if game.discard_pile else None
    if not top_card:
        return

    if top_card.card_type == CardType.DRAW_TWO:
        # +2 允许被 +2 或 +4 响应
        response_types = [CardType.DRAW_TWO, CardType.WILD_DRAW_FOUR]
    elif top_card.card_type == CardType.WILD_DRAW_FOUR:
        # +4 只能被 +4 响应
        response_types = [CardType.WILD_DRAW_FOUR]
    else:
        # 如果不是功能牌，无需响应
        return

    # 收集手牌中可响应的牌索引
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
            'pending_draw': game.pending_draw  # 仅用于提示罚摸数量，不影响判断
        }, to=current_player.sid)
        print(f"[响应] 玩家 {current_player.nickname} 有响应牌，等待选择")
    else:
        # 没有可响应的牌，直接罚摸并跳过
        apply_penalty_and_skip(room_id)

def apply_penalty_and_skip(room_id):
    game = uno_rooms.get(room_id)
    if not game:
        return
    current = game.get_current_player()
    if not current:
        return

    count = game.pending_draw
    game.pending_draw = 0
    drawn_cards = [game._draw_card() for _ in range(count)]
    current.hand.extend(drawn_cards)
    game._next_turn()
    state = game.get_game_state()
    emit('game_state', state, room=room_id)
    for p in game.players:
        emit('your_hand', {'hand': game.get_player_hand(p)}, to=p.sid)
    emit('card_drawn', {'message': f'{current.nickname} 未出响应牌，罚摸 {count} 张并跳过回合'}, room=room_id)
    print(f"[响应] {current.nickname} 未出响应牌，罚摸 {count} 张")

@socketio.on('player_response')
def handle_player_response(data):
    sid = request.sid
    room_id = data.get('room_id')
    action = data.get('action')
    card_index = data.get('card_index')

    if room_id not in response_waiting:
        emit('error', {'message': '当前没有等待响应的阶段'}, to=sid)
        return

    info = response_waiting[room_id]
    if info['player_sid'] != sid:
        emit('error', {'message': '不是你的响应回合'}, to=sid)
        return

    game = info['game']
    player = next((p for p in game.players if p.sid == sid), None)
    if not player:
        del response_waiting[room_id]
        return

    if action == 'play' and card_index is not None:
        if card_index not in info['available_indices']:
            emit('error', {'message': '选择的牌不是响应牌'}, to=sid)
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
        emit('card_played', {'success': True, 'message': f'{player.nickname} 打出 {card}'}, room=room_id)
        game._next_turn()
        state = game.get_game_state()
        emit('game_state', state, room=room_id)
        for p in game.players:
            emit('your_hand', {'hand': game.get_player_hand(p)}, to=p.sid)
        top_card = game.discard_pile[-1] if game.discard_pile else None
        if top_card and top_card.card_type in (CardType.DRAW_TWO, CardType.WILD_DRAW_FOUR):
            initiate_response_phase(room_id)
    elif action == 'pass':
        del response_waiting[room_id]
        apply_penalty_and_skip(room_id)
    else:
        emit('error', {'message': '无效的操作'}, to=sid)

# ===== 再来一局 =====
@socketio.on('restart_invite')
def handle_restart_invite(data):
    """玩家发起再来一局邀请"""
    sid = request.sid
    room_id = data.get('room_id')
    if room_id not in uno_rooms:
        emit('error', {'message': '房间不存在'}, to=sid)
        return
    game = uno_rooms[room_id]
    if game.state != GameState.FINISHED:
        emit('error', {'message': '游戏尚未结束'}, to=sid)
        return
    # 记录投票
    if room_id not in restart_votes:
        restart_votes[room_id] = set()
    restart_votes[room_id].add(sid)
    # 广播邀请
    emit('restart_invite', {'inviter_sid': sid}, room=room_id)
    print(f"[重启] 玩家 {sid} 发起再来一局邀请")

@socketio.on('restart_accept')
def handle_restart_accept(data):
    """玩家接受再来一局邀请"""
    sid = request.sid
    room_id = data.get('room_id')
    if room_id not in uno_rooms:
        emit('error', {'message': '房间不存在'}, to=sid)
        return
    game = uno_rooms[room_id]
    if game.state != GameState.FINISHED:
        emit('error', {'message': '游戏尚未结束'}, to=sid)
        return
    if room_id not in restart_votes:
        restart_votes[room_id] = set()
    restart_votes[room_id].add(sid)
    # 检查是否所有玩家都接受了
    all_players_sids = {p.sid for p in game.players}
    if restart_votes[room_id] == all_players_sids:
        # 所有人都同意，重置游戏
        if game.reset_game():
            state = game.get_game_state()
            emit('game_started', state, room=room_id)
            for player in game.players:
                hand = game.get_player_hand(player)
                emit('your_hand', {'hand': hand}, to=player.sid)
            print(f"[重启] 房间 {room_id} 游戏重置")
            # 清除投票记录
            del restart_votes[room_id]
        else:
            emit('error', {'message': '重置游戏失败'}, to=sid)
    else:
        # 通知已接受
        emit('restart_accept_ok', {'accepted': True}, to=sid)

# ===== 启动 =====
if __name__ == '__main__':
    print("=" * 50)
    print("  UNO 游戏服务器启动中...")
    print("  地址: http://127.0.0.1:8000")
    print("=" * 50)
    socketio.run(app, host='0.0.0.0', port=8000, debug=False, allow_unsafe_werkzeug=True)