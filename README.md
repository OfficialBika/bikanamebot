# Waifu Name Bot

Python Telegram bot for:
- saving forwarded photo/video character posts
- matching future photo/video forwards/uploads
- returning exact name + dynamic command copy buttons
- owner approval system
- sudo forward auto-save permissions

## Features

- Supports **photo** and **video** matching
- Stores:
  - full character name
  - command name from forwarded post, such as `/hallow` or `/catch`
  - anime / rarity / card ID when available
  - media hashes for exact/fuzzy matching
- Output format:
  - `Hint` = first word of full name
  - `Full` = full saved name
- `Powered by Official Bika` clickable owner link
- Access control:
  - only **approved users** can use lookup
  - **owner** can approve users and manage sudo users
  - **sudo users** can forward/save new media

## Commands

### General
- `/start`

### Owner only
- `/approve` by reply
- `/approve @username`
- `/approve userID`
- `/addsudo` by reply
- `/addsudo @username`
- `/addsudo userID`
- `/rmsudo` by reply
- `/rmsudo @username`
- `/rmsudo userID`
- `/stats`

### Owner + sudo
- `/save` on replied media
- auto-save on forwarded media with name in caption/text

## Environment

Copy `.env.example` to `.env` and fill:

```env
BOT_TOKEN=YOUR_BOT_TOKEN
MONGO_URI=YOUR_MONGODB_URI
DB_NAME=hallow_match_bot
OWNER_ID=123456789
OWNER_AUTOSAVE_FORWARDS=true
PHOTO_PHASH_THRESHOLD=8
VIDEO_FRAME_THRESHOLD=10
VIDEO_AVG_THRESHOLD=12
OWNER_USERNAME=@Official_Bika
DEFAULT_COMMAND=/hallow
LOG_LEVEL=INFO
```

## Install

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python bot.py
```

## Notes

- Best results come from forwarding the **original media**.
- Exact matching uses `file_unique_id` and `sha256`.
- Fuzzy matching uses `pHash` for photos and sampled frame hashes for videos.
- `/approve @username` works best after the user has already talked to the bot once, because the bot stores known usernames locally.
