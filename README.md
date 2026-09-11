# Book Club Telegram Bot

Sends a book part to subscribers every day, then a poll asking whether they've
read it. Fully in Arabic for the users; runs on Python.

## How it works

- Put your book parts as `part_1.txt`, `part_2.txt`, `part_3.txt`, ... in the
  `parts/` folder (already has 3 placeholder files — replace their content).
- Each day at `SEND_TIME`, the bot sends the next unsent part to every
  subscriber.
- Each day at `POLL_TIME`, it sends a poll: "have you read today's part?"
  with three options (read it / still reading / haven't started).
- Once a week (`REMINDER_DAY` + `REMINDER_TIME`, default Friday 20:00) it
  sends everyone a reminder about the livestream discussion. You don't have
  the exact time/link yet — just send `/set_livestream` once you do, no
  redeploy needed.
- You (the owner) get a DM confirming each send, and can check who answered
  what with `/report`.

## Step 1 — Create the bot

1. Open Telegram, message **@BotFather**.
2. Send `/newbot`, follow the prompts (name + username ending in "bot").
3. Copy the **API token** it gives you.
4. Message **@userinfobot** to get your own numeric Telegram user ID — this
   makes you the "owner" so the bot knows who's allowed to run admin
   commands.

## Step 2 — Configure

1. Copy `.env.example` to `.env`.
2. Fill in `BOT_TOKEN` and `OWNER_ID` with the values from Step 1.
3. Set `TIMEZONE`, `SEND_TIME`, and `POLL_TIME` to whatever fits your group.

## Step 3 — Add your book

Replace the text inside `parts/part_1.txt`, `part_2.txt`, `part_3.txt` with
your real content, and add more files (`part_4.txt`, `part_5.txt`, ...) for
the rest of the book. Numbering just needs to be sequential — the bot sends
them in order and lets you know if it runs out.

## Step 4 — Run it

```bash
pip install -r requirements.txt
python bot.py
```

Test it: message your bot `/start` from your own Telegram account, then
(as owner) send `/send_now` and `/poll_now` to trigger things immediately
without waiting for the schedule.

## Owner commands (only work for your OWNER_ID)

| Command | What it does |
|---|---|
| `/status` | Shows next part number, total parts, subscriber count |
| `/send_now` | Sends today's part immediately |
| `/poll_now` | Sends the poll immediately |
| `/skip_part` | Skips the current part without sending it |
| `/report` | Shows who answered the latest poll and how |
| `/set_livestream <text>` | Updates the weekly livestream reminder text (date, time, link) |
| `/reminder_now` | Sends the livestream reminder immediately |

## Subscriber commands

| Command | What it does |
|---|---|
| `/start` | Subscribes to the book club |
| `/stop` | Unsubscribes |
| `/help` | Lists commands |

## Where to run this (hosting)

The bot needs to be running 24/7 to fire on schedule — your laptop being
asleep means missed sends. Options, roughly easiest → most control:

- **Railway.app or Render.com** — free/cheap tiers, connect a GitHub repo,
  it stays running for you. Easiest if you don't want to manage a server.
- **PythonAnywhere** — has an "Always-on task" feature on paid plans; simple
  for Python-only projects like this one.
- **A small VPS** (e.g. Hetzner, DigitalOcean, ~$4-6/month) — run
  `python bot.py` inside a `screen`/`tmux` session or as a `systemd` service.
  More setup, but fully under your control and cheap.
- **Your own PC** — works for testing, but it has to stay on and connected
  to the internet for the schedule to fire.

For a small book club, Railway or PythonAnywhere is the least fuss. If you
want, I can write the exact deployment steps for whichever one you pick.

## Notes

- Data (subscriber list, progress, poll answers) is stored as plain JSON
  files in `data/` — no database needed for this scale.
- Progress (`next_part_index`) is saved automatically, so restarting the bot
  won't resend parts already sent.
