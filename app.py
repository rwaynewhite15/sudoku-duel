"""
Sudoku Duel - Web edition (Flask + Flask-SocketIO)
Supports named game rooms and AI opponents.
"""
from gevent import monkey
monkey.patch_all()

import argparse
import os
import random
import threading
import uuid
from flask import Flask, render_template, request
from flask_socketio import SocketIO, emit
from flask_socketio import join_room as sio_join_room, leave_room as sio_leave_room


def _can_place(board, r, c, v):
    for x in range(9):
        if board[r][x] == v or board[x][c] == v:
            return False
    br, bc = (r // 3) * 3, (c // 3) * 3
    for dr in range(3):
        for dc in range(3):
            if board[br + dr][bc + dc] == v:
                return False
    return True


def _solve(board, steps):
    steps[0] += 1
    if steps[0] > 100_000:
        return True  # fail open: assume solvable rather than block the event loop
    for r in range(9):
        for c in range(9):
            if board[r][c] == 0:
                for v in range(1, 10):
                    if _can_place(board, r, c, v):
                        board[r][c] = v
                        if _solve(board, steps):
                            return True
                        board[r][c] = 0
                return False
    return True


def _board_solvable(board):
    return _solve([row[:] for row in board], [0])


def _fill_board(board, steps):
    steps[0] += 1
    if steps[0] > 500_000:
        return False
    for r in range(9):
        for c in range(9):
            if board[r][c] == 0:
                nums = list(range(1, 10))
                random.shuffle(nums)
                for v in nums:
                    if _can_place(board, r, c, v):
                        board[r][c] = v
                        if _fill_board(board, steps):
                            return True
                        board[r][c] = 0
                return False
    return True


def _make_puzzle(prefilled):
    board = [[0] * 9 for _ in range(9)]
    _fill_board(board, [0])
    cells = [(r, c) for r in range(9) for c in range(9)]
    random.shuffle(cells)
    givens = set(cells[:min(prefilled, 81)])
    puzzle = [[0] * 9 for _ in range(9)]
    for r, c in givens:
        puzzle[r][c] = board[r][c]
    return puzzle, givens


class SudokuGame:
    def __init__(self, lives=3, prefilled=0):
        self.reset(lives, prefilled)

    def reset(self, lives, prefilled=0):
        self._givens = set()
        if prefilled > 0:
            self.board, self._givens = _make_puzzle(prefilled)
        else:
            self.board = [[0] * 9 for _ in range(9)]
        self.lives = {0: lives, 1: lives}
        self.current_player = 0
        self.game_over = False
        self.winner = None
        self.last_move = None
        self.starting_lives = lives

    def is_valid_move(self, row, col, val):
        if not (0 <= row < 9 and 0 <= col < 9 and 1 <= val <= 9):
            return False, "out of range"
        if self.board[row][col] != 0:
            return False, "cell already occupied"
        for c in range(9):
            if self.board[row][c] == val:
                return False, f"{val} already in row {row + 1}"
        for r in range(9):
            if self.board[r][col] == val:
                return False, f"{val} already in column {col + 1}"
        br, bc = (row // 3) * 3, (col // 3) * 3
        for r in range(br, br + 3):
            for c in range(bc, bc + 3):
                if self.board[r][c] == val:
                    return False, f"{val} already in 3x3 box"
        return True, "ok"

    def make_move(self, player, row, col, val):
        if self.game_over:
            return "game already over"
        if player != self.current_player:
            return "not your turn"
        valid, reason = self.is_valid_move(row, col, val)
        if valid:
            self.board[row][col] = val
            if not _board_solvable(self.board):
                self.board[row][col] = 0
                valid = False
                reason = "move makes the board unsolvable"
        self.last_move = {
            "player": player, "row": row, "col": col, "val": val,
            "valid": valid, "reason": reason,
        }
        if valid:
            if all(self.board[r][c] != 0 for r in range(9) for c in range(9)):
                self.game_over = True
                self.winner = player
            else:
                self.current_player = 1 - player
        else:
            self.lives[player] -= 1
            if self.lives[player] <= 0:
                self.game_over = True
                self.winner = 1 - player
            else:
                self.current_player = 1 - player
        return reason

    def state(self):
        return {
            "board": self.board,
            "given": [[(r, c) in self._givens for c in range(9)] for r in range(9)],
            "lives": self.lives,
            "current_player": self.current_player,
            "game_over": self.game_over,
            "winner": self.winner,
            "last_move": self.last_move,
            "starting_lives": self.starting_lives,
        }


def _get_ai_move(game, difficulty):
    """Return (row, col, val) for the AI based on difficulty, or None."""
    empty = [(r, c) for r in range(9) for c in range(9) if game.board[r][c] == 0]
    if not empty:
        return None

    if difficulty == 'easy':
        # 30% chance of a blunder: random cell + random number ignoring all rules
        if random.random() < 0.30:
            r, c = random.choice(empty)
            return r, c, random.randint(1, 9)
        # Otherwise a constraint-valid move (may still break solvability)
        random.shuffle(empty)
        for r, c in empty:
            opts = [v for v in range(1, 10) if _can_place(game.board, r, c, v)]
            if opts:
                return r, c, random.choice(opts)

    elif difficulty == 'medium':
        # Constraint-valid, no solvability check — will occasionally break solvability
        random.shuffle(empty)
        for r, c in empty:
            opts = [v for v in range(1, 10) if _can_place(game.board, r, c, v)]
            if opts:
                return r, c, random.choice(opts)

    elif difficulty == 'hard':
        # Constraint-valid + solvability check — never blunders
        random.shuffle(empty)
        for r, c in empty:
            opts = [v for v in range(1, 10) if _can_place(game.board, r, c, v)]
            random.shuffle(opts)
            for v in opts:
                tmp = [row[:] for row in game.board]
                tmp[r][c] = v
                if _board_solvable(tmp):
                    return r, c, v

    elif difficulty == 'expert':
        # MRV: pick the most constrained cell (fewest candidates) + solvability check
        best_r, best_c, best_opts = None, None, None
        best_count = 10
        for r, c in empty:
            opts = [v for v in range(1, 10) if _can_place(game.board, r, c, v)]
            if opts and len(opts) < best_count:
                best_count = len(opts)
                best_r, best_c, best_opts = r, c, opts
        if best_r is not None:
            random.shuffle(best_opts)
            for v in best_opts:
                tmp = [row[:] for row in game.board]
                tmp[best_r][best_c] = v
                if _board_solvable(tmp):
                    return best_r, best_c, v

    return None


class Room:
    def __init__(self, room_id, lives=3, ai_difficulty=None, prefilled=0):
        self.room_id = room_id
        self.game = SudokuGame(lives=lives, prefilled=prefilled)
        self.slots = [None, None]
        self.sid_to_player = {}
        self.ai_difficulty = ai_difficulty
        self.ai_player = 1 if ai_difficulty else None
        self.lock = threading.Lock()
        if ai_difficulty:
            self.slots[1] = '__AI__'

    def assign_slot(self, sid):
        for i in range(2):
            if self.slots[i] is None:
                self.slots[i] = sid
                self.sid_to_player[sid] = i
                return i
        return None

    def free_slot(self, sid):
        for i in range(2):
            if self.slots[i] == sid:
                self.slots[i] = None
        self.sid_to_player.pop(sid, None)

    def is_joinable(self):
        return any(s is None for s in self.slots)

    def is_ready(self):
        if self.ai_difficulty:
            return self.slots[0] is not None
        return all(s is not None for s in self.slots)

    def human_count(self):
        return sum(1 for s in self.slots if s and s != '__AI__')

    def full_state(self):
        return {
            **self.game.state(),
            "room_id": self.room_id,
            "room_ready": self.is_ready(),
            "ai_difficulty": self.ai_difficulty,
        }


app = Flask(__name__, template_folder=".")
app.config["SECRET_KEY"] = "sudoku-duel"
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="gevent")

rooms = {}
rooms_lock = threading.Lock()


@app.route("/")
def index():
    return render_template("index.html")


@socketio.on("connect")
def on_connect():
    emit("connected", {})


@socketio.on("disconnect")
def on_disconnect():
    sid = request.sid
    with rooms_lock:
        for rid, room in list(rooms.items()):
            if sid in room.sid_to_player:
                with room.lock:
                    room.free_slot(sid)
                sio_leave_room(rid)
                socketio.emit("state", room.full_state(), to=rid)
                if room.human_count() == 0:
                    del rooms[rid]
                break


@socketio.on("join_room_req")
def on_join_room(data):
    sid = request.sid
    ai_difficulty = data.get("ai_difficulty")
    lives = max(1, min(5, int(data.get("lives", 3))))
    prefilled = max(0, min(60, int(data.get("prefilled", 0))))

    if ai_difficulty:
        room_id = f"ai_{uuid.uuid4().hex[:8]}"
        room = Room(room_id, lives=lives, ai_difficulty=ai_difficulty, prefilled=prefilled)
        with rooms_lock:
            rooms[room_id] = room
    else:
        room_id = str(data.get("room_id", "")).strip()[:32]
        if not room_id:
            emit("room_error", {"message": "Room name cannot be empty."})
            return
        with rooms_lock:
            if room_id not in rooms:
                room = Room(room_id, lives=lives, prefilled=prefilled)
                rooms[room_id] = room
            else:
                room = rooms[room_id]
                if not room.is_joinable():
                    emit("room_error", {"message": "That room is full."})
                    return
                if room.ai_difficulty:
                    emit("room_error", {"message": "Cannot join an AI game room."})
                    return

    with room.lock:
        pid = room.assign_slot(sid)

    sio_join_room(room_id)
    emit("welcome", {"player": pid, "room_id": room_id, "ai_difficulty": room.ai_difficulty})
    socketio.emit("state", room.full_state(), to=room_id)

    if (room.ai_player is not None
            and room.game.current_player == room.ai_player
            and not room.game.game_over):
        _schedule_ai_move(room)


@socketio.on("leave_room_req")
def on_leave_room(data):
    sid = request.sid
    room_id = data.get("room_id")
    with rooms_lock:
        room = rooms.get(room_id)
    if not room or sid not in room.sid_to_player:
        return
    with room.lock:
        room.free_slot(sid)
    sio_leave_room(room_id)
    socketio.emit("state", room.full_state(), to=room_id)
    with rooms_lock:
        if room.human_count() == 0:
            rooms.pop(room_id, None)


@socketio.on("move")
def on_move(data):
    sid = request.sid
    room_id = data.get("room_id")
    with rooms_lock:
        room = rooms.get(room_id)
    if not room:
        return
    pid = room.sid_to_player.get(sid)
    if pid is None:
        return
    try:
        row = int(data["row"]); col = int(data["col"]); val = int(data["val"])
    except (KeyError, TypeError, ValueError):
        return
    with room.lock:
        room.game.make_move(pid, row, col, val)
    socketio.emit("state", room.full_state(), to=room_id)
    if (room.ai_player is not None
            and room.game.current_player == room.ai_player
            and not room.game.game_over):
        _schedule_ai_move(room)


@socketio.on("reset")
def on_reset(data):
    sid = request.sid
    room_id = data.get("room_id")
    with rooms_lock:
        room = rooms.get(room_id)
    if not room or sid not in room.sid_to_player:
        return
    try:
        lives = max(1, min(5, int(data.get("lives", 3))))
        prefilled = max(0, min(60, int(data.get("prefilled", 0))))
    except (TypeError, ValueError):
        lives = 3
        prefilled = 0
    with room.lock:
        room.game.reset(lives, prefilled)
    socketio.emit("state", room.full_state(), to=room_id)


def _schedule_ai_move(room):
    delay = random.uniform(0.7, 1.5)
    t = threading.Timer(delay, _do_ai_move, args=[room])
    t.daemon = True
    t.start()


def _do_ai_move(room):
    with room.lock:
        if room.game.game_over or room.game.current_player != room.ai_player:
            return
        move = _get_ai_move(room.game, room.ai_difficulty)
        if not move:
            return
        r, c, v = move
        room.game.make_move(room.ai_player, r, c, v)
    socketio.emit("state", room.full_state(), to=room.room_id)
    if room.game.current_player == room.ai_player and not room.game.game_over:
        _schedule_ai_move(room)


def main():
    p = argparse.ArgumentParser(description="Sudoku Duel web server")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=int(os.environ.get("PORT", 5000)))
    args = p.parse_args()
    print(f"[sudoku-duel] http://{args.host}:{args.port}")
    socketio.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
