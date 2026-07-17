# linelogic-discord-bot (REPO 2)

The Line Logic Discord bot. This is a SEPARATE program from the backend — it runs
as its own Railway service. Do NOT merge these files with the backend repo.

## Files in this repo
- main.py           — the bot (slash commands + auto-post webhook server)
- requirements.txt  — Python dependencies
- Procfile          — tells Railway how to start it (web: python main.py)
- env.example.txt   — reference list of env vars (real values go in Railway → Variables)

## Deploy
1. Push these 4 files to a new GitHub repo.
2. Railway → New Project → Deploy from GitHub → this repo.
3. Railway → Variables → add every var from env.example.txt (real values).
4. Deploy; logs should show "Logged in as LineBot#...".
5. Settings → Networking → Generate Domain → that URL goes in the BACKEND's
   DISCORD_BOT_SERVICE_URL variable.

Full step-by-step is in CHEAT_SHEET.md (kept with your other guides, not in this repo).

## Do NOT commit real secrets
env.example.txt is only a template. Your real DISCORD_BOT_TOKEN and any API keys
live ONLY in Railway → Variables, never in a committed file.
