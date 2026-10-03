# Telegram Music Bot

Python + aiogram music bot for a Telegram channel.

## Features

- Admin-only management panel
- Publish preview audio to the channel
- One-version posts show `دانلود آهنگ کامل`
- Multiple versions show `نسخه ۱ | نسخه ۲ | ...`
- Deep links immediately send the requested full file
- Replace a full-song file without changing the existing deep link
- Add new versions
- Search by numeric ID or title/artist
- Edit title/artist
- Delete a song and its channel post
- Basic statistics
- SQLite database

## Files

- `bot.py` - main bot
- `requirements.txt` - Python dependencies
- `.env.example` - environment variable template
- `README.md` - project notes

## Environment variables

Set these in your hosting provider's Environment Variables section:

- `BOT_TOKEN`
- `ADMIN_ID`
- `CHANNEL_ID`
- `BOT_USERNAME`

Do not upload a real `.env` file or expose your bot token on GitHub.

## Run

```bash
pip install -r requirements.txt
python bot.py
```

## Telegram setup

The bot must be an administrator of the target channel with permission to post/edit/delete channel messages.

The bot should also have a public username because channel download links use Telegram deep links.

## Important

Only upload and distribute audio files you have permission to distribute.
