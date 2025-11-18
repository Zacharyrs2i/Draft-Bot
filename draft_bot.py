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

    def add_team(self, member):
        if member not in self.teams:
            self.teams.append(member)
            self.picks_by_team[member.id] = []

    def set_pool(self, items):
        # items: list[str]
        self.available_items = items
        self.picked_items = set()

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


# One draft per guild (server) for simplicity
guild_drafts = {}  # guild_id -> DraftState


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
    """
    guild_id = ctx.guild.id
    draft = guild_drafts.get(guild_id)

    if draft is None:
        await ctx.send("No active draft. Start one with `!startdraft`.")
        return

    if ctx.author.id != draft.owner_id:
        await ctx.send("Only the draft owner can set the draft pool.")
        return

    items = [i.strip() for i in items_text.split(",") if i.strip()]
    if not items:
        await ctx.send("You must provide at least one item.")
        return

    draft.set_pool(items)
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

    display = ", ".join(remaining[:50])
    extra = "" if len(remaining) <= 50 else f" (+{len(remaining) - 50} more)"
    await ctx.send(f"Remaining items ({len(remaining)} total):\n{display}{extra}")


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
