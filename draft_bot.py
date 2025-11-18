import asyncio
import os
import discord
from discord.ext import commands
from dotenv import load_dotenv

# Load environment variables from .env
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN is not set in .env")

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

# --- Draft State Classes ---

class DraftState:
    def __init__(self, owner_id: int, rounds: int):
        self.owner_id = owner_id
        self.rounds = rounds
        self.teams = []        # list of discord.Member
        self.draft_order = []  # final order of teams
        self.current_round = 1
        self.current_index = 0  # index in draft_order
        self.direction = 1      # 1 = forward, -1 = backward (for snake)
        self.started = False
        self.completed = False

        self.available_items = []        # list of strings (players/items)
        self.picks_by_team = {}          # member_id -> [items]
        self.picked_items = set()        # set of already picked item names
        self.turn_timer_task = None
        self.item_sides = {}             # item name -> side label

    def add_team(self, member):
        if member not in self.teams:
            self.teams.append(member)
            self.picks_by_team[member.id] = []

    def set_pool(self, items, item_sides=None):
        # items: list[str]
        self.available_items = items
        self.picked_items = set()
        self.item_sides = item_sides or {}

    def begin(self):
        # lock teams and start
        self.draft_order = list(self.teams)
        self.current_round = 1
        self.current_index = 0
        self.direction = 1
        self.started = True

    def current_team(self):
        if not self.started or self.completed or not self.draft_order:
            return None
        return self.draft_order[self.current_index]

    def total_picks_for_team(self, member_id):
        return len(self.picks_by_team.get(member_id, []))

    def total_picks_made(self):
        return sum(len(picks) for picks in self.picks_by_team.values())

    def max_picks_total(self):
        return self.rounds * len(self.draft_order) if self.draft_order else 0

    def can_pick(self, member, item_name):
        if not self.started or self.completed:
            return False, "Draft has not started or is already complete."

        if self.current_team() != member:
            return False, "It is not your turn."

        if item_name not in self.available_items:
            return False, "That item is not in the draft pool."

        if item_name in self.picked_items:
            return False, "That item has already been drafted."

        # optional: limit picks per team by rounds
        if self.total_picks_for_team(member.id) >= self.rounds:
            return False, "You have already made all your picks."

        return True, None

    def make_pick(self, member, item_name):
        ok, error = self.can_pick(member, item_name)
        if not ok:
            return False, error

        # record pick
        self.picks_by_team[member.id].append(item_name)
        self.picked_items.add(item_name)

        self.cancel_timer()

        # advance pointer
        self.advance_turn()

        # check completion
        if self.total_picks_made() >= self.max_picks_total():
            self.completed = True

        return True, None

    def advance_turn(self):
        if self.completed:
            return

        # Move index
        self.current_index += self.direction

        # If we go past either end, change direction and move to next round
        if self.current_index >= len(self.draft_order):
            self.current_round += 1
            self.direction = -1
            self.current_index = len(self.draft_order) - 1

        elif self.current_index < 0:
            self.current_round += 1
            self.direction = 1
            self.current_index = 0

    def cancel_timer(self):
        if self.turn_timer_task and not self.turn_timer_task.done():
            self.turn_timer_task.cancel()
        self.turn_timer_task = None


# One draft per guild (server) for simplicity
guild_drafts = {}  # guild_id -> DraftState


def parse_pool_with_sides(items_text: str):
    """Parse text formatted as "Side A: p1, p2 | Side B: p3".

    Returns a list of (side, [items]) tuples when the format is valid, or
    ``None`` when the text either does not contain any groups or when a colon
    delimiter is missing (indicating the caller should treat the input as a
    simple comma-separated list instead).
    """

    chunks = [chunk.strip() for chunk in items_text.split("|") if chunk.strip()]
    if not chunks:
        return None

    groups = []
    for chunk in chunks:
        if ":" not in chunk:
            return None
        side, entries = chunk.split(":", 1)
        side = side.strip()
        items = [item.strip() for item in entries.split(",") if item.strip()]
        if not side or not items:
            continue
        groups.append((side, items))

    return groups or None


def parse_timer_duration(text: str):
    normalized = text.lower().replace(" ", "")
    allowed = {
        "30": 30,
        "30s": 30,
        "30sec": 30,
        "30secs": 30,
        "30seconds": 30,
        "1m": 60,
        "1min": 60,
        "1minute": 60,
        "60": 60,
        "60s": 60,
        "90": 90,
        "90s": 90,
        "90sec": 90,
        "90secs": 90,
        "90seconds": 90,
        "2m": 120,
        "2min": 120,
        "2minutes": 120,
        "120": 120,
        "120s": 120,
    }
    return allowed.get(normalized)


def format_duration(seconds: int):
    if seconds % 60 == 0:
        minutes = seconds // 60
        return f"{minutes} minute{'s' if minutes != 1 else ''}"
    return f"{seconds} seconds"


async def run_turn_timer(ctx, draft: DraftState, duration_seconds: int):
    team = draft.current_team()
    round_number = draft.current_round
    if team is None:
        return

    try:
        await asyncio.sleep(duration_seconds)
    except asyncio.CancelledError:
        return

    if draft.turn_timer_task != asyncio.current_task():
        return

    draft.turn_timer_task = None

    if draft.completed or draft.current_team() != team or draft.current_round != round_number:
        return

    await ctx.send(f"⏰ Time's up for {team.mention}! Please make your pick.")


# --- Commands ---

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    print("------")


@bot.command(name="startdraft")
async def start_draft(ctx, rounds: int):
    """Start a new draft (owner = command author). Example: !startdraft 5"""
    guild_id = ctx.guild.id

    if guild_id in guild_drafts and not guild_drafts[guild_id].completed:
        await ctx.send("A draft is already active in this server. Finish it or delete it first.")
        return

    draft = DraftState(owner_id=ctx.author.id, rounds=rounds)
    guild_drafts[guild_id] = draft

    await ctx.send(
        f"🎲 Draft created by {ctx.author.mention} for **{rounds} rounds**.\n"
        f"Others can join with `!join`. The owner should set the draft pool with `!setpool`."
    )


@bot.command(name="join")
async def join_draft(ctx):
    """Join the active draft."""
    guild_id = ctx.guild.id
    draft = guild_drafts.get(guild_id)

    if draft is None:
        await ctx.send("No active draft. Start one with `!startdraft`.")
        return

    if draft.started:
        await ctx.send("Draft has already started. You cannot join now.")
        return

    draft.add_team(ctx.author)
    await ctx.send(f"{ctx.author.mention} has joined the draft! Currently {len(draft.teams)} teams.")


@bot.command(name="setpool")
async def set_pool(ctx, *, items_text: str):
    """
    Set the draft pool as a comma-separated list.
    Example:
    !setpool Patrick Mahomes, CeeDee Lamb, Christian McCaffrey
    You can also group by side for clarity (e.g., real teams):
    !setpool Chiefs: Patrick Mahomes, Travis Kelce | 49ers: Christian McCaffrey, Deebo Samuel
    """
    guild_id = ctx.guild.id
    draft = guild_drafts.get(guild_id)

    if draft is None:
        await ctx.send("No active draft. Start one with `!startdraft`.")
        return

    if ctx.author.id != draft.owner_id:
        await ctx.send("Only the draft owner can set the draft pool.")
        return

    side_groups = parse_pool_with_sides(items_text)

    items = []
    item_sides = {}

    if side_groups:
        for side, entries in side_groups:
            for item in entries:
                if item in item_sides:
                    await ctx.send(f"Duplicate item detected: {item}")
                    return
                items.append(item)
                item_sides[item] = side
    else:
        if ":" in items_text and "|" in items_text:
            await ctx.send(
                "Pool format looks grouped but is invalid. "
                "Use `Side: item1, item2 | Side2: item3`."
            )
            return
        items = [i.strip() for i in items_text.split(",") if i.strip()]

    if not items:
        await ctx.send("You must provide at least one item.")
        return

    draft.set_pool(items, item_sides)

    if item_sides:
        counts_by_side = {}
        for side in item_sides.values():
            counts_by_side[side] = counts_by_side.get(side, 0) + 1
        side_counts = ", ".join(
            f"{side}: {count}" for side, count in sorted(counts_by_side.items())
        )
        await ctx.send(
            f"✅ Draft pool set with **{len(items)} items** across sides.\n"
            f"Breakdown — {side_counts}"
        )
    else:
        await ctx.send(f"✅ Draft pool set with **{len(items)} items**.")


@bot.command(name="begin")
async def begin_draft(ctx):
    """Begin the draft (lock teams and start snake order)."""
    guild_id = ctx.guild.id
    draft = guild_drafts.get(guild_id)

    if draft is None:
        await ctx.send("No active draft.")
        return

    if ctx.author.id != draft.owner_id:
        await ctx.send("Only the draft owner can begin the draft.")
        return

    if draft.started:
        await ctx.send("Draft has already started.")
        return

    if len(draft.teams) < 2:
        await ctx.send("You need at least 2 teams to begin the draft.")
        return

    if len(draft.available_items) == 0:
        await ctx.send("You must set the draft pool first with `!setpool`.")
        return

    draft.begin()
    order_names = ", ".join(member.display_name for member in draft.draft_order)
    await ctx.send(
        "🚨 Draft has begun!\n"
        f"Order: {order_names}\n"
        f"Current turn: {draft.current_team().mention} (Round {draft.current_round})"
    )


@bot.command(name="timer")
async def start_timer(ctx, *, duration: str):
    """Start a turn timer (owner only). Options: 30s, 1m, 90s, 2m."""
    guild_id = ctx.guild.id
    draft = guild_drafts.get(guild_id)

    if draft is None:
        await ctx.send("No active draft.")
        return

    if ctx.author.id != draft.owner_id:
        await ctx.send("Only the draft creator can start a timer.")
        return

    if not draft.started:
        await ctx.send("You need to begin the draft before starting a timer.")
        return

    if draft.completed:
        await ctx.send("Draft is already complete.")
        return

    seconds = parse_timer_duration(duration)
    if seconds is None:
        await ctx.send("Invalid duration. Choose one of: 30s, 1m, 90s, 2m.")
        return

    team = draft.current_team()
    if team is None:
        await ctx.send("There is no active turn to time.")
        return

    draft.cancel_timer()

    task = asyncio.create_task(run_turn_timer(ctx, draft, seconds))
    draft.turn_timer_task = task

    await ctx.send(
        f"⏳ Timer started for {team.mention}: {format_duration(seconds)}."
    )


@bot.command(name="pick")
async def make_pick(ctx, *, item_name: str):
    """Make your pick. Example: !pick Patrick Mahomes"""
    guild_id = ctx.guild.id
    draft = guild_drafts.get(guild_id)

    if draft is None:
        await ctx.send("No active draft.")
        return

    ok, error = draft.make_pick(ctx.author, item_name)
    if not ok:
        await ctx.send(f"❌ {error}")
        return

    await ctx.send(f"✅ {ctx.author.mention} drafted **{item_name}**!")

    if draft.completed:
        await ctx.send("🎉 Draft is complete!")
    else:
        await ctx.send(
            f"Next up: {draft.current_team().mention} (Round {draft.current_round})"
        )


@bot.command(name="status")
async def draft_status(ctx):
    """Show current draft status."""
    guild_id = ctx.guild.id
    draft = guild_drafts.get(guild_id)

    if draft is None:
        await ctx.send("No active draft.")
        return

    if not draft.started:
        await ctx.send(
            f"Draft not started yet. Teams joined: "
            f"{', '.join(m.display_name for m in draft.teams) or 'none'}"
        )
        return

    if draft.completed:
        await ctx.send("Draft is complete.")
        return

    await ctx.send(
        f"📊 Draft Status:\n"
        f"Round: {draft.current_round}/{draft.rounds}\n"
        f"Current turn: {draft.current_team().mention}\n"
        f"Total picks made: {draft.total_picks_made()}/{draft.max_picks_total()}"
    )


@bot.command(name="mypicks")
async def my_picks(ctx):
    """Show your picks."""
    guild_id = ctx.guild.id
    draft = guild_drafts.get(guild_id)

    if draft is None:
        await ctx.send("No active draft.")
        return

    picks = draft.picks_by_team.get(ctx.author.id, [])
    if not picks:
        await ctx.send("You have no picks yet.")
        return

    text = "\n".join(f"{i+1}. {p}" for i, p in enumerate(picks))
    await ctx.send(f"📜 Your picks:\n{text}")


@bot.command(name="pool")
async def show_pool(ctx):
    """Show remaining items in the draft pool."""
    guild_id = ctx.guild.id
    draft = guild_drafts.get(guild_id)

    if draft is None:
        await ctx.send("No active draft.")
        return

    remaining = [i for i in draft.available_items if i not in draft.picked_items]
    if not remaining:
        await ctx.send("No remaining items in the pool.")
        return

    if draft.item_sides:
        grouped = {}
        for item in remaining:
            side = draft.item_sides.get(item, "Unspecified")
            grouped.setdefault(side, []).append(item)

        lines = []
        for side, items in sorted(grouped.items()):
            sorted_items = sorted(items)
            display = ", ".join(sorted_items[:10])
            extra = "" if len(sorted_items) <= 10 else f" (+{len(sorted_items) - 10} more)"
            lines.append(f"{side} ({len(items)}): {display}{extra}")

        lines_text = "\n".join(lines)
        await ctx.send(
            f"Remaining items by side ({len(remaining)} total):\n{lines_text}"
        )
    else:
        display = ", ".join(remaining[:50])
        extra = "" if len(remaining) <= 50 else f" (+{len(remaining) - 50} more)"
        await ctx.send(f"Remaining items ({len(remaining)} total):\n{display}{extra}")


@bot.command(name="teams")
async def show_teams(ctx):
    """Show each team's picks in a side-by-side table."""
    guild_id = ctx.guild.id
    draft = guild_drafts.get(guild_id)

    if draft is None:
        await ctx.send("No active draft.")
        return

    teams = draft.draft_order if draft.draft_order else draft.teams
    if not teams:
        await ctx.send("No teams have joined the draft yet.")
        return

    # Build column widths based on team names and their longest pick
    picks_by_team = [draft.picks_by_team.get(team.id, []) for team in teams]
    column_widths = []
    for team, picks in zip(teams, picks_by_team):
        longest_pick = max((len(pick) for pick in picks), default=0)
        column_widths.append(max(len(team.display_name), longest_pick) + 2)

    header = " | ".join(
        team.display_name.ljust(width) for team, width in zip(teams, column_widths)
    )

    max_rows = max((len(picks) for picks in picks_by_team), default=0)
    if max_rows == 0:
        await ctx.send(f"📋 Teams:\n```{header}```")
        return

    rows = []
    for i in range(max_rows):
        row = " | ".join(
            (picks[i] if i < len(picks) else "").ljust(width)
            for picks, width in zip(picks_by_team, column_widths)
        )
        rows.append(row)

    table = "\n".join([header] + rows)
    await ctx.send(f"📋 Teams:\n```{table}```")


@bot.command(name="order")
async def show_order(ctx):
    """Show draft order (teams)."""
    guild_id = ctx.guild.id
    draft = guild_drafts.get(guild_id)

    if draft is None:
        await ctx.send("No active draft.")
        return

    if not draft.draft_order:
        await ctx.send("Draft order not set yet.")
        return

    order = "\n".join(
        f"{i+1}. {m.display_name}" for i, m in enumerate(draft.draft_order)
    )
    await ctx.send(f"📋 Draft Order:\n{order}")


bot.run(TOKEN)
