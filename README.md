# Sudoku Duel

A real-time two-player Sudoku game playable in the browser. Challenge a friend or go head-to-head against an AI opponent in two distinct game modes.

**Live app:** https://sudoku-duel.onrender.com

---

## Game Modes

### Duel
Turn-based. Players alternate placing digits. A wrong move costs a life; the player who runs out of lives loses. If the board fills completely, the player who placed the last valid digit wins.

### Race
Simultaneous. Both players receive identical boards and fill them independently at the same time. Progress bars show each player's completion percentage. First to finish wins. If a player uses all their lives they are disqualified. Opponent wrong guesses are shown as small red digits on your board and flash the affected cell red.

---

## Features

### Puzzle generation
- Three difficulties: **Easy**, **Medium**, **Hard**
- Technique-aware generation — difficulty reflects solving technique required, not just clue count:
  - Easy → solvable with naked singles only
  - Medium → requires hidden singles
  - Hard → requires pairs / pointing pairs / box-line reduction
- Every puzzle has a unique solution

### Lives
- Choose 1–5 lives, or **Unlimited** (Race mode only)
- A wrong guess (digit that violates Sudoku rules or makes the board unsolvable) costs one life
- Previously guessed wrong digits are shown as small red numbers inside the cell

### Turn timer (Duel only)
- Optional per-turn countdown: Off, 15 s, 30 s, 60 s
- Running out of time costs a life and passes the turn

### AI opponent
- Four difficulty levels:
  - **Easy** — 20% outright blunder; otherwise board-safe only 25% of the time (frequent mistakes)
  - **Medium** — 8% outright blunder; otherwise board-safe 75% of the time (occasional mistakes)
  - **Hard** — 3% outright blunder; otherwise always plays a board-safe move in a random cell
  - **Expert** — 1% outright blunder; otherwise targets the most-constrained cell with a board-safe move
- AI pace in Race mode is scaled by difficulty (Easy ~20–40 s/move, Expert ~5–10 s/move)

### Race mode extras
- Live progress bars for both players
- Cells your opponent has already filled appear as a light blue tint on your board
- Opponent wrong guesses flash the cell light red and add the digit to your wrong-guess display for that cell

### Player names
- Enter your name in the lobby before joining; it appears in the scoreboard and race progress bars

### New Game controls (in-game)
- Change lives, puzzle difficulty, mode, and AI difficulty without leaving the room

---

## Running locally

```bash
pip install flask flask-socketio gevent gevent-websocket
python app.py
```

Open `http://localhost:5000` in your browser. Share the URL (or your LAN IP) with another player and use the same room name to connect.

---

## Tech stack

- **Backend:** Python, Flask, Flask-SocketIO (`async_mode=gevent`)
- **Puzzle generation:** custom constraint-propagation solver with technique-aware difficulty scoring, run in a thread pool to avoid blocking the event loop
- **Frontend:** vanilla HTML/CSS/JS, Socket.IO client
- **Deployment:** Render (Gunicorn + GeventWebSocketWorker)
