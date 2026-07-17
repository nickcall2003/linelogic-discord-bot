import os
import asyncio
import logging
from datetime import datetime, timezone

import aiohttp
from aiohttp import web
import discord
from discord import app_commands
from discord.ext import commands

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("linelogic-bot")

# ---------- Config ----------

DISCORD_TOKEN = os.environ["DISCORD_BOT_TOKEN"]
GUILD_ID = int(os.environ["DISCORD_GUILD_ID"])

# TODO: replace with your real FastAPI base URL and swap the paths in each
# fetch_json(...) call below once you send over your actual route list.
LINE_LOGIC_API_BASE = os.environ.get("LINE_LOGIC_API_BASE", "https://api.thelinelogic.com")
LINE_LOGIC_API_KEY = os.environ.get("LINE_LOGIC_API_KEY", "")

WEBHOOK_SHARED_SECRET = os.environ.get("WEBHOOK_SHARED_SECRET", "")
PORT = int(os.environ.get("PORT", "8080"))

# v1 channels only — free tier
CHANNEL_IDS = {
    "daily_model": int(os.environ.get("CHANNEL_DAILY_MODEL", "0")),
    "official_results": int(os.environ.get("CHANNEL_OFFICIAL_RESULTS", "0")),
}

WELCOME_CHANNEL_ID = int(os.environ.get("CHANNEL_WELCOME", "0"))
BOT_LOGS_CHANNEL_ID = int(os.environ.get("CHANNEL_BOT_LOGS", "0"))
VERIFIED_ROLE_ID = int(os.environ.get("VERIFIED_ROLE_ID", "0"))

# AI explain layer. Dormant until your backend has the /api/explain route live
# (which is where the LLM key lives — the bot never calls an LLM directly, it just
# asks your backend). Flip to "1" once that endpoint is deployed.
AI_EXPLAIN_ENABLED = os.environ.get("AI_EXPLAIN_ENABLED", "0").strip() == "1"

# Notify role IDs — bot @-mentions these when posting, instead of pinging everyone.
# Right-click each role in Server Settings -> Roles -> Copy Role ID.
NOTIFY_ROLE_IDS = {
    "mlb": int(os.environ.get("ROLE_MLB_ALERTS", "0")),
    "nba": int(os.environ.get("ROLE_NBA_ALERTS", "0")),
    "nfl": int(os.environ.get("ROLE_NFL_ALERTS", "0")),
    "nhl": int(os.environ.get("ROLE_NHL_ALERTS", "0")),
    "ncaaf": int(os.environ.get("ROLE_NCAAF_ALERTS", "0")),  # College Football
    "ncaab": int(os.environ.get("ROLE_NCAAB_ALERTS", "0")),  # College Basketball
    "wnba": int(os.environ.get("ROLE_WNBA_ALERTS", "0")),
    "tennis": int(os.environ.get("ROLE_TENNIS_ALERTS", "0")),
    "soccer": int(os.environ.get("ROLE_SOCCER_ALERTS", "0")),
    "ufc": int(os.environ.get("ROLE_UFC_ALERTS", "0")),
    "stocks": int(os.environ.get("ROLE_STOCK_ALERTS", "0")),
    "line_movement": int(os.environ.get("ROLE_LINE_MOVEMENT_ALERTS", "0")),
}

BRAND_COLOR = int(os.environ.get("BRAND_COLOR_HEX", "2B6CB0"), 16)

# --- Premium / Whop — NOT active in v1. Leave these unset until you're ready. ---
# When you do activate: set PREMIUM_ROLE_ID, uncomment the /premium/webhook route
# near the bottom, and enable Whop's native Discord role sync (no code needed
# on your end for the actual billing — Whop handles grant/revoke itself; the
# webhook below is only a fallback if you ever want custom control).
PREMIUM_ROLE_ID = int(os.environ.get("PREMIUM_ROLE_ID", "0"))

intents = discord.Intents.default()
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)

# Tappable dropdown of the sports your backend supports (from SPORT_SEASON in
# your main.py). Discord shows these as buttons/menu on mobile — no typing, no
# typos, and no invalid sports reaching the API. Add/remove a line here if your
# supported sports change. (Max 25 choices; we're well under.)
SPORT_CHOICES = [
    app_commands.Choice(name="MLB", value="mlb"),
    app_commands.Choice(name="NBA", value="nba"),
    app_commands.Choice(name="NFL", value="nfl"),
    app_commands.Choice(name="NHL", value="nhl"),
    app_commands.Choice(name="WNBA", value="wnba"),
    app_commands.Choice(name="Tennis", value="tennis"),
    app_commands.Choice(name="Soccer", value="soccer"),
    app_commands.Choice(name="UFC", value="ufc"),
    app_commands.Choice(name="NCAA Football", value="ncaaf"),
    app_commands.Choice(name="NCAA Basketball", value="ncaab"),
    app_commands.Choice(name="NCAA Baseball", value="ncaabb"),
]

# Props only exist for these sports in your backend (/api/{sport}/props/... and
# /api/mlb/props/...). Keep this list in sync with team_props()/mlb_props().
PROP_SPORT_CHOICES = [
    app_commands.Choice(name="MLB", value="mlb"),
    app_commands.Choice(name="NBA", value="nba"),
    app_commands.Choice(name="WNBA", value="wnba"),
    app_commands.Choice(name="NFL", value="nfl"),
]


# ---------- Helpers ----------

async def fetch_json(session: aiohttp.ClientSession, path: str, params: dict | None = None, timeout: int = 10):
    url = f"{LINE_LOGIC_API_BASE}{path}"
    headers = {"Authorization": f"Bearer {LINE_LOGIC_API_KEY}"} if LINE_LOGIC_API_KEY else {}
    async with session.get(url, params=params, headers=headers, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
        resp.raise_for_status()
        return await resp.json()


def now_utc():
    return datetime.now(timezone.utc)


def role_mention(sport_key: str) -> str | None:
    role_id = NOTIFY_ROLE_IDS.get(sport_key)
    return f"<@&{role_id}>" if role_id else None


# ---------- Slash commands ----------

def _fmt_odds(v):
    """American odds as a display string (+150 / -164 / —)."""
    if v is None:
        return "—"
    try:
        v = int(round(float(v)))
    except (TypeError, ValueError):
        return str(v)
    return f"+{v}" if v > 0 else str(v)


def _pick_embed(p: dict) -> discord.Embed:
    """Build an embed from a pick object as returned by /api/picks/best and
    /api/picks/free (keys: sport, match, pick, prob 0-1, confidence,
    market_odds, fair_odds, edge_pct)."""
    prob_pct = round((p.get("prob") or 0) * 100)
    edge = p.get("edge_pct")
    edge_str = f"+{edge:.1f}%" if isinstance(edge, (int, float)) else "—"

    embed = discord.Embed(
        title=f"📊 {p.get('pick', 'Pick')}",
        description=p.get("match", ""),
        color=BRAND_COLOR,
        timestamp=now_utc(),
    )
    embed.add_field(name="Sport", value=str(p.get("sport", "—")).upper(), inline=True)
    embed.add_field(name="Model Win %", value=f"{prob_pct}%", inline=True)
    embed.add_field(name="Confidence", value=str(p.get("confidence", "—")), inline=True)
    embed.add_field(name="Market", value=_fmt_odds(p.get("market_odds")), inline=True)
    embed.add_field(name="Fair Odds", value=_fmt_odds(p.get("fair_odds")), inline=True)
    embed.add_field(name="Edge", value=edge_str, inline=True)
    embed.set_footer(text="Line Logic Model • thelinelogic.com")
    return embed


@bot.tree.command(name="model", description="The Line Logic model's read on any team — edge or not")
@app_commands.describe(
    name="Team or player, e.g. Braves",
    sport="Optional: pick a sport to narrow the search",
)
@app_commands.choices(sport=SPORT_CHOICES)
async def model_command(interaction: discord.Interaction, name: str,
                        sport: app_commands.Choice[str] | None = None):
    await interaction.response.defer()
    params = {"team": name}
    if sport:
        params["sport"] = sport.value
    async with aiohttp.ClientSession() as session:
        try:
            data = await fetch_json(session, "/api/model", params=params)
        except Exception:
            log.exception("model lookup failed for %s", name)
            await interaction.followup.send("Couldn't reach the model right now — try again shortly.")
            return

    if not data.get("found"):
        await interaction.followup.send(
            f"No upcoming game found for **{name}** in the next few days. "
            f"Check the spelling, or see the full board at thelinelogic.com"
        )
        return

    edge = data.get("edge_pct")
    has_edge = data.get("has_edge")
    win_pct = data.get("win_pct", round((data.get("prob") or 0) * 100))

    if has_edge:
        color = 0x2ECC71  # green — there's an edge
        headline = f"✅ Edge found: **+{edge:.1f}%**"
    elif edge is not None:
        color = 0xE67E22  # amber — priced, no edge
        headline = "⚖️ No edge — the market price is fair or against this side."
    else:
        color = BRAND_COLOR  # no market line captured yet
        headline = "ℹ️ No market line captured yet, so edge can't be computed."

    embed = discord.Embed(
        title=f"📊 {data.get('pick', name)}",
        description=f"{data.get('match', '')}\n{headline}",
        color=color,
        timestamp=now_utc(),
    )
    embed.add_field(name="Sport", value=str(data.get("sport", "—")).upper(), inline=True)
    embed.add_field(name="Model Win %", value=f"{win_pct}%", inline=True)
    embed.add_field(name="Confidence", value=str(data.get("confidence", "—")), inline=True)
    embed.add_field(name="Market", value=_fmt_odds(data.get("market_odds")), inline=True)
    embed.add_field(name="Fair Odds", value=_fmt_odds(data.get("fair_odds")), inline=True)
    embed.add_field(
        name="Edge",
        value=(f"+{edge:.1f}%" if isinstance(edge, (int, float)) else "—"),
        inline=True,
    )
    if data.get("date"):
        embed.set_footer(text=f"Game {data['date']} • Line Logic Model • thelinelogic.com")
    else:
        embed.set_footer(text="Line Logic Model • thelinelogic.com")
    await interaction.followup.send(embed=embed)


@bot.tree.command(name="today", description="Today's slate — how many games/matches are on the board per sport")
async def today_command(interaction: discord.Interaction):
    await interaction.response.defer()
    async with aiohttp.ClientSession() as session:
        try:
            data = await fetch_json(session, "/api/slate", timeout=25)
        except Exception:
            log.exception("today slate lookup failed")
            await interaction.followup.send("Couldn't reach the model right now — try again shortly.")
            return

    counts = data.get("counts", {}) or {}
    if not counts:
        await interaction.followup.send("Nothing on the board today yet.")
        return

    # Display names + the unit each sport is counted in, in a sensible order
    SPORT_DISPLAY = [
        ("mlb", "⚾ Baseball", "games"),
        ("nba", "🏀 Basketball", "games"),
        ("wnba", "👟 WNBA", "games"),
        ("nfl", "🏈 Football", "games"),
        ("ncaaf", "🏟️ College Football", "games"),
        ("ncaab", "🎓 College Basketball", "games"),
        ("nhl", "🏒 Hockey", "games"),
        ("soccer", "⚽ Soccer", "matches"),
        ("tennis", "🎾 Tennis", "matches"),
        ("ufc", "🥊 UFC", "fights"),
        ("golf", "⛳ Golf", "events"),
    ]

    lines = []
    listed = set()
    for key, label, unit in SPORT_DISPLAY:
        n = counts.get(key, 0)
        if n:
            lines.append(f"{label}: **{n}** {unit}")
            listed.add(key)
    # catch any sport not in the display list
    for key, n in counts.items():
        if key not in listed and n:
            lines.append(f"{key.upper()}: **{n}**")

    total = data.get("total", sum(counts.values()))

    embed = discord.Embed(
        title="📅 Today's Line Logic Slate",
        description="\n".join(lines),
        color=BRAND_COLOR,
        timestamp=now_utc(),
    )
    embed.add_field(name="Total on the board", value=f"**{total}**", inline=False)
    embed.add_field(
        name="Want the plays?",
        value="Use `/model [team]` for a specific read, or check the daily model feed for the top edges.",
        inline=False,
    )
    embed.set_footer(text="Line Logic • thelinelogic.com")
    await interaction.followup.send(embed=embed)


def _prop_stat(p: dict) -> str:
    return str(p.get("stat") or p.get("label") or "").strip()


def _prop_edge(p: dict):
    """Signed gap between the model's projection and the book line. Positive =
    model projects OVER the line, negative = UNDER. None if either is missing."""
    proj, line = p.get("projection"), p.get("line")
    try:
        return float(proj) - float(line)
    except (TypeError, ValueError):
        return None


async def _games_for_sport(session, sport: str, ids_only: bool = False):
    path = "/api/mlb/games" if sport == "mlb" else f"/api/{sport}/games"
    data = await fetch_json(session, path, timeout=25)
    games = data if isinstance(data, list) else (data.get("games") or [])
    return games


@bot.tree.command(name="prop", description="Today's biggest player-prop edges from the Line Logic model")
@app_commands.describe(
    sport="Pick a sport",
    player="Optional: filter to one player, e.g. Judge",
)
@app_commands.choices(sport=PROP_SPORT_CHOICES)
async def prop_command(interaction: discord.Interaction,
                       sport: app_commands.Choice[str],
                       player: str = ""):
    await interaction.response.defer()
    sport_val = sport.value
    needle = player.lower().strip()

    async with aiohttp.ClientSession() as session:
        try:
            games = await _games_for_sport(session, sport_val)
        except Exception:
            log.exception("prop: games fetch failed for %s", sport_val)
            await interaction.followup.send(f"Couldn't load today's {sport_val.upper()} games.")
            return

        # Only pull props for games that haven't finished (live/upcoming), and
        # cap how many games we hit so one command doesn't hammer the backend.
        active = [g for g in games if str(g.get("status", "")).lower() not in
                  ("final", "finished", "post", "completed", "closed")]
        active = active[:8]
        if not active:
            await interaction.followup.send(f"No upcoming {sport_val.upper()} games with props right now.")
            return

        collected = []
        for g in active:
            gid = g.get("id") or g.get("game_id")
            if gid is None:
                continue
            try:
                pdata = await fetch_json(session, f"/api/{sport_val}/props/{gid}")
            except Exception:
                continue
            matchup = ""
            try:
                matchup = f"{(g.get('away') or {}).get('name','')} @ {(g.get('home') or {}).get('name','')}".strip(" @")
            except Exception:
                pass
            for pr in (pdata.get("props") or []):
                if pr.get("projection") is None or pr.get("line") is None:
                    continue
                if needle and needle not in str(pr.get("player", "")).lower():
                    continue
                edge = _prop_edge(pr)
                if edge is None:
                    continue
                pr["_edge"] = edge
                pr["_matchup"] = matchup
                collected.append(pr)

    if not collected:
        msg = (f"No props with a model projection for **{player}** today."
               if needle else f"No {sport_val.upper()} props with a model edge on the board yet.")
        await interaction.followup.send(msg)
        return

    # Rank by absolute projection-vs-line gap — the model's strongest leans
    collected.sort(key=lambda p: abs(p["_edge"]), reverse=True)
    top = collected[:6]

    title = (f"🎯 {sport_val.upper()} Prop Edges — {player}"
             if needle else f"🎯 Today's Top {sport_val.upper()} Prop Edges")
    embed = discord.Embed(title=title, color=BRAND_COLOR, timestamp=now_utc())
    for pr in top:
        edge = pr["_edge"]
        lean = "OVER" if edge > 0 else "UNDER"
        stat = _prop_stat(pr)
        proj = pr.get("projection")
        line = pr.get("line")
        try:
            proj_s = f"{float(proj):g}"
            line_s = f"{float(line):g}"
        except (TypeError, ValueError):
            proj_s, line_s = str(proj), str(line)
        embed.add_field(
            name=f"{pr.get('player','—')} — {stat}",
            value=(f"Model **{lean} {line_s}** {stat}\n"
                   f"Projection {proj_s} vs line {line_s} • gap {abs(edge):.1f}"
                   + (f"\n{pr['_matchup']}" if pr.get("_matchup") else "")),
            inline=False,
        )
    embed.set_footer(text="Model projections — not a guarantee. Line Logic • thelinelogic.com")
    await interaction.followup.send(embed=embed)


# ---------- AI explain layer ----------
# These call ONE backend route, /api/explain, which is where the LLM lives. The
# bot never talks to an LLM directly. Your backend feeds the LLM only your own
# model + context data, so the AI explains the pick — it never invents it. All
# three stay hidden until AI_EXPLAIN_ENABLED=1 and the backend route exists.

async def _explain_disabled_notice(interaction: discord.Interaction):
    await interaction.followup.send(
        "The AI explain feature isn't switched on yet. (It goes live once the "
        "`/api/explain` route is deployed on the backend and `AI_EXPLAIN_ENABLED=1`.)"
    )


@bot.tree.command(name="explain", description="AI breakdown of why the model likes a play — built on Line Logic's own data")
@app_commands.describe(query="Team, player, or matchup, e.g. Braves ML")
async def explain_command(interaction: discord.Interaction, query: str):
    await interaction.response.defer()
    if not AI_EXPLAIN_ENABLED:
        await _explain_disabled_notice(interaction)
        return
    async with aiohttp.ClientSession() as session:
        try:
            data = await fetch_json(session, "/api/generate", params={"q": query, "mode": "full"})
        except Exception:
            log.exception("explain failed for %s", query)
            await interaction.followup.send(f"Couldn't generate a breakdown for **{query}** right now.")
            return

    if not data.get("found") or not data.get("content"):
        await interaction.followup.send(
            f"No model play found for **{query}** to explain. Try a team that's on today's board."
        )
        return

    edge = data.get("edge_pct")
    edge_str = f"+{edge:.1f}%" if isinstance(edge, (int, float)) else "—"
    embed = discord.Embed(
        title=f"🧠 {data.get('pick', query)}",
        description=str(data["content"])[:4000],  # embed description hard limit
        color=BRAND_COLOR,
        timestamp=now_utc(),
    )
    if data.get("match"):
        embed.insert_field_at(0, name="Matchup", value=data["match"], inline=False)
    embed.add_field(name="Model Win %", value=f"{data.get('win_pct', '—')}%", inline=True)
    embed.add_field(name="Market", value=_fmt_odds(data.get("market_odds")), inline=True)
    embed.add_field(name="Edge", value=edge_str, inline=True)
    embed.set_footer(text="AI explains the model's read — not a guarantee, and lines move. Line Logic")
    await interaction.followup.send(embed=embed)


@bot.tree.command(name="why", description="Quick bulleted reasons behind a model play")
@app_commands.describe(query="Team or player, e.g. Braves")
async def why_command(interaction: discord.Interaction, query: str):
    await interaction.response.defer()
    if not AI_EXPLAIN_ENABLED:
        await _explain_disabled_notice(interaction)
        return
    async with aiohttp.ClientSession() as session:
        try:
            data = await fetch_json(session, "/api/generate", params={"q": query, "mode": "why"})
        except Exception:
            log.exception("why failed for %s", query)
            await interaction.followup.send(f"Couldn't pull the quick view for **{query}** right now.")
            return

    bullets = data.get("bullets") or []
    if not data.get("found") or not bullets:
        await interaction.followup.send(f"No model play found for **{query}**.")
        return

    edge = data.get("edge_pct")
    edge_str = f"+{edge:.1f}%" if isinstance(edge, (int, float)) else "—"
    body = "\n".join(f"• {b}" for b in bullets[:6])
    embed = discord.Embed(
        title=f"⚡ Why: {data.get('pick', query)}",
        description=f"**Model edge: {edge_str}**\n\n{body}",
        color=BRAND_COLOR,
        timestamp=now_utc(),
    )
    embed.set_footer(text="Run /explain for the full breakdown • Line Logic")
    await interaction.followup.send(embed=embed)


@bot.tree.command(name="sources", description="What data fed a model play — honest provenance, no filler")
@app_commands.describe(query="Team or player")
async def sources_command(interaction: discord.Interaction, query: str):
    await interaction.response.defer()
    if not AI_EXPLAIN_ENABLED:
        await _explain_disabled_notice(interaction)
        return
    async with aiohttp.ClientSession() as session:
        try:
            data = await fetch_json(session, "/api/generate", params={"q": query, "mode": "sources"})
        except Exception:
            log.exception("sources failed for %s", query)
            await interaction.followup.send(f"Couldn't pull the data trail for **{query}** right now.")
            return

    sources = data.get("sources") or {}
    if not data.get("found") or not sources:
        await interaction.followup.send(f"No data trail available for **{query}**.")
        return

    embed = discord.Embed(
        title=f"🔍 Data behind: {data.get('pick', query)}",
        description="Only what actually fed this projection is listed here.",
        color=BRAND_COLOR,
        timestamp=now_utc(),
    )
    for label, src in list(sources.items())[:10]:
        embed.add_field(name=str(label), value=f"✓ {src}", inline=True)
    embed.set_footer(text="Line Logic • thelinelogic.com")
    await interaction.followup.send(embed=embed)


@bot.tree.command(name="confidence", description="What the model's confidence grade means for a play — not certainty of outcome")
@app_commands.describe(query="Team or player, e.g. Braves")
async def confidence_command(interaction: discord.Interaction, query: str):
    await interaction.response.defer()
    if not AI_EXPLAIN_ENABLED:
        await _explain_disabled_notice(interaction)
        return
    async with aiohttp.ClientSession() as session:
        try:
            data = await fetch_json(session, "/api/generate", params={"q": query, "mode": "confidence"})
        except Exception:
            log.exception("confidence failed for %s", query)
            await interaction.followup.send(f"Couldn't pull confidence for **{query}** right now.")
            return

    if not data.get("found") or not data.get("content"):
        await interaction.followup.send(f"No model play found for **{query}**.")
        return

    embed = discord.Embed(
        title=f"🎯 Confidence: {data.get('confidence', '—')} — {data.get('pick', query)}",
        description=str(data["content"])[:2000],
        color=BRAND_COLOR,
        timestamp=now_utc(),
    )
    embed.set_footer(text="Confidence = quality of the opportunity, not certainty. Line Logic")
    await interaction.followup.send(embed=embed)


@bot.tree.command(name="tweet", description="A ready-to-post X blurb for a model play (copy-paste)")
@app_commands.describe(query="Team or player, e.g. Braves ML")
async def tweet_command(interaction: discord.Interaction, query: str):
    await interaction.response.defer()
    if not AI_EXPLAIN_ENABLED:
        await _explain_disabled_notice(interaction)
        return
    async with aiohttp.ClientSession() as session:
        try:
            data = await fetch_json(session, "/api/generate", params={"q": query, "mode": "tweet"})
        except Exception:
            log.exception("tweet failed for %s", query)
            await interaction.followup.send(f"Couldn't draft a post for **{query}** right now.")
            return

    if not data.get("found") or not data.get("content"):
        await interaction.followup.send(f"No model play found for **{query}**.")
        return

    # Send as plain text in a code-style block so it's easy to copy on mobile
    await interaction.followup.send(f"**Draft post — copy below:**\n>>> {data['content']}")


@bot.tree.command(name="writeup", description="Long-form model breakdown (Overview, Model, Matchup, Market, Risks)")
@app_commands.describe(query="Team or player, e.g. Braves")
async def writeup_command(interaction: discord.Interaction, query: str):
    await interaction.response.defer()
    if not AI_EXPLAIN_ENABLED:
        await _explain_disabled_notice(interaction)
        return
    async with aiohttp.ClientSession() as session:
        try:
            data = await fetch_json(session, "/api/generate", params={"q": query, "mode": "writeup"}, timeout=40)
        except Exception:
            log.exception("writeup failed for %s", query)
            await interaction.followup.send(f"Couldn't generate a write-up for **{query}** right now.")
            return

    if not data.get("found") or not data.get("content"):
        await interaction.followup.send(f"No model play found for **{query}**.")
        return

    # Write-ups can exceed Discord's 2000-char message limit — chunk safely.
    text = str(data["content"]).strip()
    header = f"🧠 **{data.get('pick', query)}** — {data.get('match', '')}\n\n"
    text = header + text
    # split into <=1900 char chunks, skipping any empty ones
    chunks = [text[i:i + 1900] for i in range(0, len(text), 1900)]
    chunks = [c for c in chunks if c.strip()]
    if not chunks:
        await interaction.followup.send(f"No write-up available for **{query}** right now.")
        return
    await interaction.followup.send(chunks[0])
    for extra in chunks[1:]:
        await interaction.followup.send(extra)


@bot.tree.command(name="ask", description="Ask about a model play — grounded in Line Logic's data, not a general chatbot")
@app_commands.describe(query="Team or player the question is about", question="Your question")
async def ask_command(interaction: discord.Interaction, query: str, question: str):
    await interaction.response.defer()
    if not AI_EXPLAIN_ENABLED:
        await _explain_disabled_notice(interaction)
        return
    async with aiohttp.ClientSession() as session:
        try:
            data = await fetch_json(
                session, "/api/generate",
                params={"q": query, "mode": "ask", "question": question},
            )
        except Exception:
            log.exception("ask failed for %s", query)
            await interaction.followup.send("Couldn't answer that right now — try again shortly.")
            return

    if not data.get("found"):
        await interaction.followup.send(
            f"No model play found for **{query}**, so there's nothing grounded to answer from."
        )
        return

    answer = data.get("answer")
    if not answer:
        await interaction.followup.send("The model doesn't include enough to answer that confidently.")
        return

    embed = discord.Embed(
        title=f"💬 {query}",
        description=f"**Q:** {question}\n\n{str(answer)[:3500]}",
        color=BRAND_COLOR,
        timestamp=now_utc(),
    )
    embed.set_footer(text="Grounded in Line Logic's model data • not betting advice")
    await interaction.followup.send(embed=embed)


# Static betting glossary — no LLM, no backend call, so it works even with AI off
# and costs nothing. Add terms freely. Keys are matched case-insensitively.
GLOSSARY = {
    "edge": "The gap between the model's fair price and the sportsbook's price. A positive edge means the book is offering more than the true odds — that's the value the model targets.",
    "clv": "Closing Line Value — whether you bet at a better price than the line closed at. Consistently beating the closing line is the strongest sign a model has real, repeatable value.",
    "ev": "Expected Value — the average profit or loss a bet would return if you could make it many times. Positive EV (+EV) means it's mathematically worth making, even if it loses on any given night.",
    "expected value": "Expected Value — the average profit or loss a bet would return if you could make it many times. Positive EV (+EV) means it's mathematically worth making, even if it loses on any given night.",
    "vig": "Vig (or juice) — the sportsbook's built-in commission. It's why a coin-flip bet pays -110 on both sides instead of +100; the book keeps the difference.",
    "juice": "Juice (or vig) — the sportsbook's built-in commission. It's why a coin-flip bet pays -110 on both sides instead of +100; the book keeps the difference.",
    "implied probability": "The win probability baked into a set of odds. -150 implies about 60%. Comparing implied probability to the model's probability is how an edge is found.",
    "unit": "A standard bet size, usually 1% of your bankroll. Sizing in units instead of dollars keeps your risk consistent and protects you through losing streaks.",
    "steam": "Steam — a fast, coordinated line move across many sportsbooks at once, usually from sharp money hitting the same side hard.",
    "sharp money": "Money from professional, winning bettors. Books move lines quickly when they detect it because it signals where the true price is heading.",
    "fair odds": "The price that matches the model's true win probability, with no sportsbook vig added. The edge is the distance between fair odds and the market price.",
}


@bot.tree.command(name="learn", description="Plain-English definition of a betting term")
@app_commands.describe(term="e.g. edge, CLV, EV, vig, implied probability, unit")
async def learn_command(interaction: discord.Interaction, term: str):
    key = term.lower().strip()
    definition = GLOSSARY.get(key)
    if not definition:
        # light fuzzy match so "closing line value" finds "clv", etc.
        for k, v in GLOSSARY.items():
            if key in k or k in key:
                definition = v
                break
    if not definition:
        available = ", ".join(sorted({k for k in GLOSSARY if " " not in k}))
        await interaction.response.send_message(
            f"I don't have a definition for **{term}** yet. Try one of: {available}",
            ephemeral=True,
        )
        return
    embed = discord.Embed(
        title=f"📚 {term.strip().title()}",
        description=definition,
        color=BRAND_COLOR,
    )
    embed.set_footer(text="Line Logic • learn the why, not just the pick")
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="track", description="Track a pick to your capper record (model-board picks only)")
@app_commands.describe(
    pick="Team or player, e.g. Braves",
    units="How many units (default 1)",
    sport="Optional: narrow the sport",
)
@app_commands.choices(sport=SPORT_CHOICES)
async def track_command(interaction: discord.Interaction, pick: str,
                        units: float = 1.0,
                        sport: app_commands.Choice[str] | None = None):
    await interaction.response.defer()
    body = {
        "user_id": str(interaction.user.id),
        "username": interaction.user.display_name,
        "team": pick,
        "stake_units": units,
    }
    if sport:
        body["sport"] = sport.value

    async with aiohttp.ClientSession() as session:
        try:
            url = f"{LINE_LOGIC_API_BASE}/api/capper/track"
            headers = {"Authorization": f"Bearer {LINE_LOGIC_API_KEY}"} if LINE_LOGIC_API_KEY else {}
            async with session.post(url, json=body, headers=headers,
                                    timeout=aiohttp.ClientTimeout(total=25)) as resp:
                data = await resp.json()
        except Exception:
            log.exception("track failed for %s", pick)
            await interaction.followup.send("Couldn't track that right now — try again shortly.")
            return

    if not data.get("ok"):
        if data.get("error") == "not_on_board":
            await interaction.followup.send(
                f"**{pick}** isn't on the model board right now, so it can't be tracked yet. "
                f"You can only track picks the model has on the current slate."
            )
        else:
            await interaction.followup.send("Couldn't track that pick — check the name and try again.")
        return

    odds = data.get("market_odds")
    odds_str = _fmt_odds(odds)
    embed = discord.Embed(
        title="✅ Pick Tracked",
        description=f"**{data.get('pick')}**\n{data.get('match','')}",
        color=0x2ECC71,
        timestamp=now_utc(),
    )
    embed.add_field(name="Sport", value=str(data.get("sport", "—")).upper(), inline=True)
    embed.add_field(name="Odds", value=odds_str, inline=True)
    embed.add_field(name="Stake", value=f"{data.get('stake_units', 1)}u", inline=True)
    embed.set_footer(text=f"Tracked by {interaction.user.display_name} • graded after the game")
    await interaction.followup.send(embed=embed)
    await interaction.response.defer()
    async with aiohttp.ClientSession() as session:
        try:
            # Same endpoint that powers the website Track Record page
            data = await fetch_json(session, "/api/accuracy", params={"days": 30}, timeout=25)
        except Exception:
            log.exception("accuracy lookup failed")
            await interaction.followup.send("Performance data isn't available right now.")
            return

    ov = data.get("overall", {})
    by_sport = data.get("by_sport", {})
    if not ov:
        await interaction.followup.send("Performance data isn't available right now.")
        return

    at_w = ov.get("alltime_wins", 0)
    at_l = ov.get("alltime_losses", 0)
    at_pct = ov.get("alltime_pct")
    units = ov.get("units_30d")
    roi = ov.get("roi_30d")
    n_sports = len(by_sport)

    desc = f"**{at_pct}%** all-time · {at_w}-{at_l} across {n_sports} sports"
    if isinstance(units, (int, float)):
        desc += f"\n**{units:+.2f}u** on graded wagers (30d)"

    embed = discord.Embed(
        title="📈 Line Logic — Verified Track Record",
        description=desc,
        color=BRAND_COLOR,
        timestamp=now_utc(),
    )

    SPORT_LABEL = {
        "mlb": "⚾ MLB", "nba": "🏀 NBA", "wnba": "👟 WNBA", "nfl": "🏈 NFL",
        "nhl": "🏒 NHL", "ncaaf": "🏟️ NCAA FB", "ncaab": "🎓 NCAA BB",
        "ncaabb": "⚾ NCAA Baseball", "soccer": "⚽ Soccer", "tennis": "🎾 Tennis",
        "ufc": "🥊 UFC", "golf": "⛳ Golf",
    }
    # rank sports by all-time wins so the biggest bodies of work show first
    ranked = sorted(by_sport.items(),
                    key=lambda kv: (kv[1].get("alltime_wins") or 0), reverse=True)
    shown = 0
    for sp, s in ranked:
        if shown >= 8:
            break
        label = SPORT_LABEL.get(sp, sp.upper())
        aw = s.get("alltime_wins", 0)
        al = s.get("alltime_losses", 0)
        apct = s.get("alltime_pct")
        u = s.get("units_30d")
        roi_s = s.get("roi_30d")
        priced = s.get("priced_30d", 0)
        if priced and isinstance(u, (int, float)):
            val = f"{aw}-{al} ({apct}%) · **{u:+.2f}u** · {roi_s:+.1f}% ROI on {priced} wagers (30d)"
        else:
            val = f"{aw}-{al} ({apct}%) · +EV wagers building"
        embed.add_field(name=label, value=val, inline=False)
        shown += 1

    embed.set_footer(text="Verified across every graded pick • thelinelogic.com")
    await interaction.followup.send(embed=embed)


_STOCK_DISCLAIMER = ("Educational, paper-traded model signals — not financial advice. "
                     "Consult a licensed advisor before investing.")


@bot.tree.command(name="stock", description="Market quote, or the day's hot pick if no ticker given")
@app_commands.describe(symbol="Optional ticker, e.g. NVDA. Leave blank for the Hot Pick of the Day.")
async def stock_command(interaction: discord.Interaction, symbol: str = ""):
    await interaction.response.defer()
    symbol = symbol.upper().strip()

    async with aiohttp.ClientSession() as session:
        # No ticker -> the paper-model's "Hot Pick of the Day" (/api/stocks/hotpick)
        if not symbol:
            try:
                data = await fetch_json(session, "/api/stocks/hotpick", timeout=30)
            except Exception:
                log.exception("hotpick failed")
                await interaction.followup.send("Market data isn't available right now.")
                return
            hot = data.get("hot")
            if not hot:
                await interaction.followup.send(
                    "Nothing trending up steadily today — the model is holding its paper positions.\n"
                    f"_{_STOCK_DISCLAIMER}_"
                )
                return
            embed = discord.Embed(
                title=f"🔥 Hot Pick of the Day — {hot.get('ticker', '')}",
                description=hot.get("name", ""),
                color=0x2ECC71,
                timestamp=now_utc(),
            )
            embed.add_field(name="Price", value=f"${hot.get('price', '—')}", inline=True)
            embed.add_field(name=f"Last {hot.get('days', '?')} sessions", value=f"+{hot.get('pct', '—')}%", inline=True)
            embed.add_field(name="Up days", value=f"{hot.get('up_days', '—')}/{hot.get('days', '—')}", inline=True)
            vr = hot.get("vol_ratio")
            if isinstance(vr, (int, float)) and vr >= 1.2:
                embed.add_field(name="Volume", value=f"{round((vr - 1) * 100)}% above usual", inline=True)
            embed.set_footer(text=_STOCK_DISCLAIMER)
            await interaction.followup.send(embed=embed)
            return

        # Ticker given -> on-demand quote (/api/stocks/quote)
        try:
            data = await fetch_json(session, "/api/stocks/quote", params={"symbol": symbol, "range": "1D"}, timeout=25)
        except Exception:
            log.exception("stock quote failed for %s", symbol)
            await interaction.followup.send(f"Couldn't pull a quote for **{symbol}** right now.")
            return

    if data.get("error"):
        await interaction.followup.send(f"No price data for **{symbol}**.")
        return

    chg = data.get("change_pct")
    up = isinstance(chg, (int, float)) and chg >= 0
    chg_str = f"{chg:+.2f}%" if isinstance(chg, (int, float)) else "—"
    embed = discord.Embed(
        title=f"📈 {data.get('symbol', symbol)} — {data.get('name', '')}",
        color=0x2ECC71 if up else 0xE74C3C,
        timestamp=now_utc(),
    )
    embed.add_field(name="Price", value=f"${data.get('price', '—')}", inline=True)
    embed.add_field(name="Change (1D)", value=f"{'▲' if up else '▼'} {chg_str}", inline=True)
    embed.set_footer(text=_STOCK_DISCLAIMER)
    await interaction.followup.send(embed=embed)


@bot.event
async def on_ready():
    guild = discord.Object(id=GUILD_ID)
    # Commands are defined globally; copy them into this guild so they register
    # instantly (guild syncs are immediate; pure-global syncs can take ~1 hour).
    bot.tree.copy_global_to(guild=guild)
    synced = await bot.tree.sync(guild=guild)
    log.info("Logged in as %s. Synced %d commands to guild %s", bot.user, len(synced), GUILD_ID)


async def bot_log(message: str):
    if not BOT_LOGS_CHANNEL_ID:
        return
    channel = bot.get_channel(BOT_LOGS_CHANNEL_ID)
    if channel:
        await channel.send(f"`{now_utc().strftime('%Y-%m-%d %H:%M UTC')}` {message}")


@bot.event
async def on_member_join(member: discord.Member):
    guild = member.guild
    member_count = guild.member_count

    if VERIFIED_ROLE_ID:
        role = guild.get_role(VERIFIED_ROLE_ID)
        if role:
            try:
                await member.add_roles(role, reason="Auto-verified on join")
            except discord.Forbidden:
                log.warning("Missing permission to assign Verified role — check bot role position")

    if WELCOME_CHANNEL_ID:
        channel = bot.get_channel(WELCOME_CHANNEL_ID)
        if channel:
            embed = discord.Embed(
                title=f"👋 Welcome, {member.display_name}!",
                description=(
                    "Welcome to Line Logic 📊\n\n"
                    "Before you get started:\n"
                    "1️⃣ Read the rules\n"
                    "2️⃣ Pick your sports in the roles channel\n"
                    "3️⃣ Check the daily model\n\n"
                    f"You're member **#{member_count}**. Glad to have you."
                ),
                color=BRAND_COLOR,
                timestamp=now_utc(),
            )
            embed.set_thumbnail(url=member.display_avatar.url)
            await channel.send(embed=embed)

    await bot_log(f"👤 {member} joined — now {member_count} members")


@bot.event
async def on_member_remove(member: discord.Member):
    await bot_log(f"👋 {member} left — now {member.guild.member_count} members")


# ---------- Inbound webhook server ----------
# Your backend calls these to auto-post the daily model card and results.
# Include "notify_sport" in the payload (e.g. "mlb") to auto-ping the right
# notify role instead of pinging everyone.

routes = web.RouteTableDef()


def check_secret(request: web.Request) -> bool:
    if not WEBHOOK_SHARED_SECRET:
        return True
    return request.headers.get("X-Webhook-Secret") == WEBHOOK_SHARED_SECRET


@routes.post("/post/{channel_key}")
async def post_to_channel(request: web.Request):
    """
    curl -X POST https://your-bot.up.railway.app/post/daily_model \
      -H "X-Webhook-Secret: $WEBHOOK_SHARED_SECRET" \
      -H "Content-Type: application/json" \
      -d '{"title": "MLB — Braves ML", "description": "Model Probability: 61% | Market: +105 | Edge: +8.7% | Confidence: B+",
           "notify_sport": "mlb"}'
    """
    if not check_secret(request):
        return web.json_response({"error": "unauthorized"}, status=401)

    channel_key = request.match_info["channel_key"]
    channel_id = CHANNEL_IDS.get(channel_key)
    if not channel_id:
        return web.json_response({"error": f"unknown channel_key '{channel_key}'"}, status=400)

    payload = await request.json()
    channel = bot.get_channel(channel_id)
    if channel is None:
        return web.json_response({"error": "bot cannot see that channel"}, status=500)

    embed = discord.Embed(
        title=payload.get("title", ""),
        description=payload.get("description", ""),
        color=BRAND_COLOR,
        timestamp=now_utc(),
    )
    for f in payload.get("fields", []):
        embed.add_field(name=f.get("name", ""), value=f.get("value", ""), inline=f.get("inline", False))
    embed.set_footer(text="Line Logic • thelinelogic.com")

    content = role_mention(payload.get("notify_sport", "")) if payload.get("notify_sport") else None
    await channel.send(content=content, embed=embed)
    return web.json_response({"ok": True})


# --- Premium role sync — DISABLED in v1. Uncomment when you activate Whop/premium. ---
#
# @routes.post("/premium/webhook")
# async def premium_webhook(request: web.Request):
#     if not check_secret(request):
#         return web.json_response({"error": "unauthorized"}, status=401)
#     payload = await request.json()
#     user_id = int(payload.get("discord_user_id", 0))
#     action = payload.get("action")
#     guild = bot.get_guild(GUILD_ID)
#     if guild is None or not user_id or not PREMIUM_ROLE_ID:
#         return web.json_response({"error": "misconfigured"}, status=500)
#     member = guild.get_member(user_id) or await guild.fetch_member(user_id)
#     role = guild.get_role(PREMIUM_ROLE_ID)
#     if action == "grant":
#         await member.add_roles(role, reason="Premium subscription active")
#     elif action == "revoke":
#         await member.remove_roles(role, reason="Premium subscription ended")
#     else:
#         return web.json_response({"error": "unknown action"}, status=400)
#     return web.json_response({"ok": True})


@routes.get("/health")
async def health(request: web.Request):
    return web.json_response({"ok": True, "bot_ready": bot.is_ready()})


async def start_web_server():
    app = web.Application()
    app.add_routes(routes)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    log.info("Webhook server listening on port %d", PORT)


async def main():
    async with bot:
        await start_web_server()
        await bot.start(DISCORD_TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
