"""
Sudoku Duel - Web edition (Flask + Flask-SocketIO)
Supports named game rooms and AI opponents.
"""
from gevent import monkey, get_hub as _get_hub
monkey.patch_all()

import argparse
import os
import random
import threading
import time
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


def _count_solutions(board, limit=2):
    """Count solutions up to limit, with step cap to avoid blocking."""
    steps = [0]
    count = [0]

    def _rec(b):
        if count[0] >= limit:
            return
        steps[0] += 1
        if steps[0] > 50_000:
            return
        for r in range(9):
            for c in range(9):
                if b[r][c] == 0:
                    for v in range(1, 10):
                        if _can_place(b, r, c, v):
                            b[r][c] = v
                            _rec(b)
                            b[r][c] = 0
                    return
        count[0] += 1

    _rec([row[:] for row in board])
    return count[0]


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


# ── Precomputed Sudoku unit/peer structure ────────────────────────────────────
_UNITS: list[list[tuple[int, int]]] = (
    [[(r, c) for c in range(9)] for r in range(9)] +
    [[(r, c) for r in range(9)] for c in range(9)] +
    [[(br * 3 + dr, bc * 3 + dc) for dr in range(3) for dc in range(3)]
     for br in range(3) for bc in range(3)]
)
_BOXES: list[list[tuple[int, int]]] = _UNITS[18:]
_CELL_UNITS: dict[tuple[int, int], list[list[tuple[int, int]]]] = {
    (r, c): [u for u in _UNITS if (r, c) in u]
    for r in range(9) for c in range(9)
}
_PEERS: dict[tuple[int, int], frozenset[tuple[int, int]]] = {
    cell: frozenset(x for u in _CELL_UNITS[cell] for x in u if x != cell)
    for cell in _CELL_UNITS
}


def _score_puzzle(puzzle: list[list[int]]) -> int:
    """Return the highest technique level needed to solve the puzzle.
    0 = naked singles only  → Easy
    1 = hidden singles      → Medium
    2 = pairs/pointing/stuck → Hard
    """
    board = [row[:] for row in puzzle]
    cands: dict[tuple[int, int], set[int]] = {
        (r, c): {v for v in range(1, 10) if _can_place(board, r, c, v)}
        for r in range(9) for c in range(9) if board[r][c] == 0
    }

    def _place(cell: tuple[int, int], val: int) -> None:
        r, c = cell
        board[r][c] = val
        del cands[cell]
        for peer in _PEERS[cell]:
            if peer in cands:
                cands[peer].discard(val)

    max_level = 0
    changed = True
    while changed and cands:
        changed = False

        # Naked single
        for cell in list(cands):
            if len(cands[cell]) == 1:
                _place(cell, next(iter(cands[cell])))
                changed = True
        if changed:
            continue

        # Hidden single
        for unit in _UNITS:
            for val in range(1, 10):
                hits = [cell for cell in unit if cell in cands and val in cands[cell]]
                if len(hits) == 1:
                    _place(hits[0], val)
                    max_level = max(max_level, 1)
                    changed = True
                    break
            if changed:
                break
        if changed:
            continue

        # Naked pair (elimination)
        for unit in _UNITS:
            twos = [cell for cell in unit if cell in cands and len(cands[cell]) == 2]
            for i, c1 in enumerate(twos):
                for c2 in twos[i + 1:]:
                    if cands[c1] == cands[c2]:
                        pair_vals = cands[c1]
                        for cell in unit:
                            if cell in cands and cell != c1 and cell != c2:
                                before = len(cands[cell])
                                cands[cell] -= pair_vals
                                if len(cands[cell]) < before:
                                    max_level = max(max_level, 2)
                                    changed = True
        if changed:
            continue

        # Pointing pairs/triples (box → line)
        for box in _BOXES:
            box_set = set(box)
            for val in range(1, 10):
                hits = [cell for cell in box if cell in cands and val in cands[cell]]
                if len(hits) < 2:
                    continue
                rows = {cell[0] for cell in hits}
                cols = {cell[1] for cell in hits}
                if len(rows) == 1:
                    row = next(iter(rows))
                    for cell in [(row, c) for c in range(9)]:
                        if cell not in box_set and cell in cands and val in cands[cell]:
                            cands[cell].discard(val)
                            max_level = max(max_level, 2)
                            changed = True
                elif len(cols) == 1:
                    col = next(iter(cols))
                    for cell in [(r, col) for r in range(9)]:
                        if cell not in box_set and cell in cands and val in cands[cell]:
                            cands[cell].discard(val)
                            max_level = max(max_level, 2)
                            changed = True
        if changed:
            continue

        # Box-line reduction (line → box)
        for line in range(9):
            for val in range(1, 10):
                row_hits = [(line, c) for c in range(9)
                            if (line, c) in cands and val in cands[(line, c)]]
                if len(row_hits) >= 2:
                    boxes = {(r // 3, c // 3) for r, c in row_hits}
                    if len(boxes) == 1:
                        br, bc = next(iter(boxes))
                        for r in range(br * 3, br * 3 + 3):
                            for c in range(bc * 3, bc * 3 + 3):
                                if ((r, c) not in row_hits and (r, c) in cands
                                        and val in cands[(r, c)]):
                                    cands[(r, c)].discard(val)
                                    max_level = max(max_level, 2)
                                    changed = True
                col_hits = [(r, line) for r in range(9)
                            if (r, line) in cands and val in cands[(r, line)]]
                if len(col_hits) >= 2:
                    boxes = {(r // 3, c // 3) for r, c in col_hits}
                    if len(boxes) == 1:
                        br, bc = next(iter(boxes))
                        for r in range(br * 3, br * 3 + 3):
                            for c in range(bc * 3, bc * 3 + 3):
                                if ((r, c) not in col_hits and (r, c) in cands
                                        and val in cands[(r, c)]):
                                    cands[(r, c)].discard(val)
                                    max_level = max(max_level, 2)
                                    changed = True
        if changed:
            continue

        max_level = max(max_level, 2)  # stuck — needs harder techniques
        break

    if cands:
        max_level = max(max_level, 2)
    return max_level


def _make_puzzle(difficulty: str) -> tuple[list[list[int]], set[tuple[int, int]]]:
    """Generate a uniquely-solvable puzzle matching the target technique difficulty."""
    target = {'easy': 0, 'medium': 1, 'hard': 2}[difficulty]
    clue_target = {'easy': 44, 'medium': 32, 'hard': 26}[difficulty]
    deadline = time.time() + 18
    best_puzzle: list[list[int]] | None = None
    best_dist = 999

    while time.time() < deadline:
        board = [[0] * 9 for _ in range(9)]
        if not _fill_board(board, [0]):
            continue
        puzzle = [row[:] for row in board]
        cells = [(r, c) for r in range(9) for c in range(9)]
        random.shuffle(cells)
        count = 81
        for r, c in cells:
            if count <= clue_target or time.time() > deadline:
                break
            saved = puzzle[r][c]
            puzzle[r][c] = 0
            if _count_solutions(puzzle) == 1:
                count -= 1
            else:
                puzzle[r][c] = saved
        score = _score_puzzle(puzzle)
        dist = abs(score - target)
        if dist < best_dist:
            best_dist = dist
            best_puzzle = [row[:] for row in puzzle]
        if score == target:
            break
        # Adapt clue target: too easy → fewer clues, too hard → more clues
        if score < target:
            clue_target = max(22, clue_target - 4)
        else:
            clue_target = min(50, clue_target + 4)

    puzzle = best_puzzle if best_puzzle is not None else puzzle
    givens = {(r, c) for r in range(9) for c in range(9) if puzzle[r][c] != 0}
    return puzzle, givens


class SudokuGame:
    def __init__(self, lives=3, puzzle_difficulty: str = 'medium', mode: str = 'duel'):
        self.mode = mode
        self.reset(lives, puzzle_difficulty)

    def reset(self, lives, puzzle_difficulty: str = 'medium'):
        self._givens = set()
        template, self._givens = _get_hub().threadpool.apply(_make_puzzle, (puzzle_difficulty,))
        self.board = template
        if self.mode == 'race':
            # Each player works their own copy of the same starting board
            self.boards = {0: [row[:] for row in template], 1: [row[:] for row in template]}
            self.race_wrong: dict[int, dict[tuple[int, int], set[int]]] = {0: {}, 1: {}}
            self.wrong_guesses: dict[tuple[int, int], set[int]] = {}
        else:
            self.boards: dict[int, list[list[int]]] = {}
            self.race_wrong = {}
            self.wrong_guesses = {}
        self.lives = {0: lives, 1: lives}
        self.current_player = 0
        self.game_over = False
        self.winner = None
        self.last_move = None
        self.starting_lives = lives

    def _is_valid_on(self, board, row, col, val):
        if not (0 <= row < 9 and 0 <= col < 9 and 1 <= val <= 9):
            return False, "out of range"
        if board[row][col] != 0:
            return False, "cell already occupied"
        for c in range(9):
            if board[row][c] == val:
                return False, f"{val} already in row {row + 1}"
        for r in range(9):
            if board[r][col] == val:
                return False, f"{val} already in column {col + 1}"
        br, bc = (row // 3) * 3, (col // 3) * 3
        for r in range(br, br + 3):
            for c in range(bc, bc + 3):
                if board[r][c] == val:
                    return False, f"{val} already in 3x3 box"
        return True, "ok"

    def is_valid_move(self, row, col, val):
        return self._is_valid_on(self.board, row, col, val)

    def make_move(self, player, row, col, val):
        if self.game_over:
            return "game already over"
        if self.mode == 'race':
            return self._race_move(player, row, col, val)
        if player != self.current_player:
            return "not your turn"
        valid, reason = self._is_valid_on(self.board, row, col, val)
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
            self.wrong_guesses.pop((row, col), None)
            if all(self.board[r][c] != 0 for r in range(9) for c in range(9)):
                self.game_over = True
                self.winner = player
            else:
                self.current_player = 1 - player
        else:
            self.wrong_guesses.setdefault((row, col), set()).add(val)
            if self.starting_lives > 0:
                self.lives[player] -= 1
                if self.lives[player] <= 0:
                    self.game_over = True
                    self.winner = 1 - player
                    return reason
            self.current_player = 1 - player
        return reason

    def _race_move(self, player, row, col, val):
        board = self.boards[player]
        valid, reason = self._is_valid_on(board, row, col, val)
        if valid:
            board[row][col] = val
            if not _board_solvable(board):
                board[row][col] = 0
                valid = False
                reason = "move makes the board unsolvable"
        self.last_move = {
            "player": player, "row": row, "col": col, "val": val,
            "valid": valid, "reason": reason,
        }
        if valid:
            self.race_wrong[player].pop((row, col), None)
            if all(board[r][c] != 0 for r in range(9) for c in range(9)):
                self.game_over = True
                self.winner = player
        else:
            self.race_wrong[player].setdefault((row, col), set()).add(val)
            if self.starting_lives > 0:
                self.lives[player] -= 1
                if self.lives[player] <= 0:
                    self.game_over = True
                    self.winner = 1 - player
        return reason

    def _race_progress(self, player):
        board = self.boards[player]
        return sum(1 for r in range(9) for c in range(9)
                   if board[r][c] != 0 and (r, c) not in self._givens)

    def state(self):
        given = [[(r, c) in self._givens for c in range(9)] for r in range(9)]
        base = {
            "mode": self.mode,
            "given": given,
            "lives": self.lives,
            "game_over": self.game_over,
            "winner": self.winner,
            "last_move": self.last_move,
            "starting_lives": self.starting_lives,
        }
        if self.mode == 'race':
            total = 81 - len(self._givens)
            return {
                **base,
                "boards": {str(p): self.boards[p] for p in (0, 1)},
                "current_player": -1,
                "wrong_guesses": {
                    str(p): {f"{r},{c}": sorted(v)
                             for (r, c), v in self.race_wrong[p].items()}
                    for p in (0, 1)
                },
                "progress": {str(p): self._race_progress(p) for p in (0, 1)},
                "total_cells": total,
            }
        return {
            **base,
            "board": self.board,
            "current_player": self.current_player,
            "wrong_guesses": {f"{r},{c}": sorted(vals)
                              for (r, c), vals in self.wrong_guesses.items()},
        }


def _get_ai_move(game, difficulty, ai_player=1):
    """Return (row, col, val) for the AI based on difficulty, or None."""
    board = game.boards.get(ai_player, game.board) if game.mode == 'race' else game.board
    empty = [(r, c) for r in range(9) for c in range(9) if board[r][c] == 0]
    if not empty:
        return None

    if difficulty == 'easy':
        # 30% chance of a blunder: random cell + random number ignoring all rules
        if random.random() < 0.30:
            r, c = random.choice(empty)
            return r, c, random.randint(1, 9)
        random.shuffle(empty)
        for r, c in empty:
            opts = [v for v in range(1, 10) if _can_place(board, r, c, v)]
            if opts:
                return r, c, random.choice(opts)

    elif difficulty == 'medium':
        random.shuffle(empty)
        for r, c in empty:
            opts = [v for v in range(1, 10) if _can_place(board, r, c, v)]
            if opts:
                return r, c, random.choice(opts)

    elif difficulty == 'hard':
        random.shuffle(empty)
        for r, c in empty:
            opts = [v for v in range(1, 10) if _can_place(board, r, c, v)]
            random.shuffle(opts)
            for v in opts:
                tmp = [row[:] for row in board]
                tmp[r][c] = v
                if _board_solvable(tmp):
                    return r, c, v

    elif difficulty == 'expert':
        best: tuple[int, int, list[int]] | None = None
        best_count = 10
        for r, c in empty:
            opts = [v for v in range(1, 10) if _can_place(board, r, c, v)]
            if opts and len(opts) < best_count:
                best_count = len(opts)
                best = (r, c, opts)
        if best is not None:
            best_r, best_c, best_opts = best
            random.shuffle(best_opts)
            for v in best_opts:
                tmp = [row[:] for row in board]
                tmp[best_r][best_c] = v
                if _board_solvable(tmp):
                    return best_r, best_c, v

    return None


class Room:
    def __init__(self, room_id, lives=3, ai_difficulty=None, puzzle_difficulty='medium',
                 mode='duel', turn_seconds=0):
        self.room_id = room_id
        self.mode = mode
        self.game = SudokuGame(lives=lives, puzzle_difficulty=puzzle_difficulty, mode=mode)
        self.slots: list[str | None] = [None, None]
        self.sid_to_player = {}
        self.ai_difficulty = ai_difficulty
        self.ai_player = 1 if ai_difficulty else None
        self.lock = threading.Lock()
        self.turn_seconds = turn_seconds
        self._turn_timer = None
        self._turn_start = None
        self._turn_token = None
        if ai_difficulty:
            self.slots[1] = '__AI__'

    def _start_turn_timer(self):
        if self._turn_timer:
            self._turn_timer.cancel()
        self._turn_token = random.random()
        self._turn_start = time.time()
        token = self._turn_token
        t = threading.Timer(self.turn_seconds, _handle_timeout, args=[self, token])
        t.daemon = True
        t.start()
        self._turn_timer = t

    def _cancel_turn_timer(self):
        if self._turn_timer:
            self._turn_timer.cancel()
            self._turn_timer = None
        self._turn_start = None
        self._turn_token = None

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
        deadline = None
        if self.turn_seconds > 0 and self._turn_start and not self.game.game_over:
            deadline = self._turn_start + self.turn_seconds
        return {
            **self.game.state(),
            "room_id": self.room_id,
            "room_ready": self.is_ready(),
            "ai_difficulty": self.ai_difficulty,
            "turn_seconds": self.turn_seconds,
            "turn_deadline": deadline,
        }


app = Flask(__name__, template_folder=".")
app.config["SECRET_KEY"] = "sudoku-duel"
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="gevent")

rooms = {}
rooms_lock = threading.Lock()


def _maybe_start_timer(room):
    """Start turn timer if room is ready, game is active, and it's a human's turn."""
    if room.game.mode == 'race' or room.turn_seconds <= 0 or room.game.game_over or not room.is_ready():
        room._cancel_turn_timer()
        return
    if room.ai_player is not None and room.game.current_player == room.ai_player:
        room._cancel_turn_timer()
        return
    room._start_turn_timer()


def _handle_timeout(room, token):
    with room.lock:
        if room.game.game_over or room._turn_token != token:
            return
        player = room.game.current_player
        room.game.lives[player] -= 1
        room.game.last_move = {
            "player": player, "row": None, "col": None, "val": None,
            "valid": False, "reason": "time's up",
        }
        if room.game.lives[player] <= 0:
            room.game.game_over = True
            room.game.winner = 1 - player
        else:
            room.game.current_player = 1 - player
    _maybe_start_timer(room)
    socketio.emit("state", room.full_state(), to=room.room_id)
    if (room.ai_player is not None and not room.game.game_over
            and room.game.mode != 'race'
            and room.game.current_player == room.ai_player):
        _schedule_ai_move(room)


@app.route("/")
def index():
    return render_template("index.html")


@socketio.on("connect")
def on_connect():
    emit("connected", {})


@socketio.on("disconnect")
def on_disconnect():
    sid = request.sid  # type: ignore[attr-defined]
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
    sid = request.sid  # type: ignore[attr-defined]
    ai_difficulty = data.get("ai_difficulty")
    _l = int(data.get("lives", 3))
    lives = 0 if _l == 0 else max(1, min(5, _l))
    pd = data.get("puzzle_difficulty", "medium")
    puzzle_difficulty = pd if pd in ("easy", "medium", "hard") else "medium"
    gm = data.get("mode", "duel")
    mode = gm if gm in ("duel", "race") else "duel"
    turn_seconds = max(0, min(300, int(data.get("turn_seconds", 0))))

    if ai_difficulty:
        room_id = f"ai_{uuid.uuid4().hex[:8]}"
        room = Room(room_id, lives=lives, ai_difficulty=ai_difficulty,
                    puzzle_difficulty=puzzle_difficulty, mode=mode, turn_seconds=turn_seconds)
        with rooms_lock:
            rooms[room_id] = room
    else:
        room_id = str(data.get("room_id", "")).strip()[:32]
        if not room_id:
            emit("room_error", {"message": "Room name cannot be empty."})
            return
        with rooms_lock:
            if room_id not in rooms:
                room = Room(room_id, lives=lives, puzzle_difficulty=puzzle_difficulty,
                            mode=mode, turn_seconds=turn_seconds)
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
    emit("welcome", {"player": pid, "room_id": room_id, "ai_difficulty": room.ai_difficulty,
                     "turn_seconds": room.turn_seconds})
    socketio.emit("state", room.full_state(), to=room_id)
    _maybe_start_timer(room)

    if (room.ai_player is not None and not room.game.game_over
            and (room.game.mode == 'race'
                 or room.game.current_player == room.ai_player)):
        _schedule_ai_move(room)


@socketio.on("leave_room_req")
def on_leave_room(data):
    sid = request.sid  # type: ignore[attr-defined]
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
    sid = request.sid  # type: ignore[attr-defined]
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
    _maybe_start_timer(room)
    socketio.emit("state", room.full_state(), to=room_id)
    # In race mode the AI timer is a self-sustaining chain started at join/reset;
    # do NOT reschedule here or every player move stacks another parallel timer.
    if (room.ai_player is not None and not room.game.game_over
            and room.game.mode != 'race'
            and room.game.current_player == room.ai_player):
        _schedule_ai_move(room)


@socketio.on("reset")
def on_reset(data):
    sid = request.sid  # type: ignore[attr-defined]
    room_id = data.get("room_id")
    with rooms_lock:
        room = rooms.get(room_id)
    if not room or sid not in room.sid_to_player:
        return
    try:
        _l = int(data.get("lives", 3))
        lives = 0 if _l == 0 else max(1, min(5, _l))
        pd = data.get("puzzle_difficulty", "medium")
        puzzle_difficulty = pd if pd in ("easy", "medium", "hard") else "medium"
        gm = data.get("mode", room.mode)
        new_mode = gm if gm in ("duel", "race") else room.mode
        turn_seconds = max(0, min(300, int(data.get("turn_seconds", room.turn_seconds))))
    except (TypeError, ValueError):
        lives = 3
        puzzle_difficulty = "medium"
        new_mode = room.mode
        turn_seconds = room.turn_seconds
    with room.lock:
        room.mode = new_mode
        room.game.mode = new_mode
        room.turn_seconds = turn_seconds
        room.game.reset(lives, puzzle_difficulty)
    _maybe_start_timer(room)
    socketio.emit("state", room.full_state(), to=room_id)


def _schedule_ai_move(room):
    if room.game.mode == 'race':
        race_delays = {
            'easy':   (20.0, 40.0),
            'medium': (12.0, 24.0),
            'hard':   ( 8.0, 16.0),
            'expert': ( 5.0, 10.0),
        }
        lo, hi = race_delays.get(room.ai_difficulty, (12.0, 24.0))
    else:
        lo, hi = 0.7, 1.5
    delay = random.uniform(lo, hi)
    t = threading.Timer(delay, _do_ai_move, args=[room])
    t.daemon = True
    t.start()


def _do_ai_move(room):
    ai = room.ai_player
    if ai is None:
        return
    with room.lock:
        if room.game.game_over:
            return
        if room.game.mode != 'race' and room.game.current_player != ai:
            return
        move = _get_ai_move(room.game, room.ai_difficulty, ai_player=ai)
        if not move:
            return
        r, c, v = move
        room.game.make_move(ai, r, c, v)
    _maybe_start_timer(room)
    socketio.emit("state", room.full_state(), to=room.room_id)
    if not room.game.game_over:
        if room.game.mode == 'race' or room.game.current_player == ai:
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
