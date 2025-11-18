import os
import random
import asyncio

import discord
from discord.ext import commands
from dotenv import load_dotenv

# ===============================
# SETUP
# ===============================

# Load environment variables from .env
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN is not set in .env")

# Intents: need message_content so the bot can see text messages
intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)


# ===============================
# DRAFT STATE CLASS
# ===============================

class DraftState:
    def __init__(self, owner_id: int, rounds: int):
        self.owner_id = owner_id
        self.rounds = rounds

        self.teams = []              # list[discord.Member]
        self.draft_order = []        # list[discord.Member]

        self.current_round = 1
        self.current_index = 0       # index in draft_order
        self.direction = 1           # 1 = forward, -1 = backward (snake)
        self.started = False
        self.completed = False

        self.available_items = []    # list[str] - all possible picks
        self.picks_by_team = {}      # dict[user_id, list[str]]
        self.picked_items = set()    # set[str] - already drafted

    def add_team(self, member: discord.Member):
        """Add a team to the draft if not already there."""
        if member not in self.teams:
            self.teams.append(member)
            self.picks_by_team[member.id] = []

    def set_pool(self, items):
        """Set the list of available draft items."""
        self.available_items = items
        self.picked_items = set()

    def begin(self):
        """Lock teams and start the draft."""
        # If no order chosen yet (no fliporder), default to join order.
        if not self.draft_order:
            self.draft_order = list(self.teams)

        self.current_round = 1
        self.current_index = 0
        self.direction = 1
        self.started = True

    def current_team(self):
        """Return the member whose turn it is now."""
        if not self.started or self.completed or not self.draft_order:
            return None
        return self.draft_order[self.current_index]

    def total_picks_for_team(self, member_id: int) -> int:
        return len(self.picks_by_team.get(member_id, []))

    def total_picks_made(self) -> int:
        return sum(len(picks) for picks in self.picks_by_team.values())

    def max_picks_total(self) -> int:
        return self.rounds * len(self.draft_order) if self.draft_order else 0

    def can_pick(self, member: discord.Member, item_name: str):
        """Check if this member can pick this item now."""
        if not self.started or self.completed:
            return False, "Draft has not started or is already complete."

        if self.current_team() != member:
            return False, "It is not your turn."

        if item_name not in self.available_items:
            return False, "That item is not in the draft pool."

        if item_name in self.picked_items:
            return False, "That item has already been drafted."

        if self.total_picks_for_team(member.id) >= self.rounds:
            return False, "You have already made all your picks."

        return True, None

    def make_pick(self, member: discord.Member, item_name: str):
        """Record a pick and advance the turn."""
        ok, error = self.can_pick(member, item_name)
        if not ok:
            return False, error

        # Record pick
        self.picks_by_team[member.id].append(item_name)
        self.picked_items.add(item_name)

        # Advance
        self.advance_turn()

        # Check completion
        if self.total_picks_made() >= self.max_picks_total():
            self.completed = True

        return True, None

    def advance_turn(self):
        """Move pointer for the snake draft."""
        if self.completed or not self.draft_order:
            return

        self.current_index += self.direction

        # Went past the end
        if self.current_index >= len(self.draft_order):
            self.current_round += 1
            self.direction = -1
            self.current_index = len(self.draft_order) - 1

        # Went past the beginning
        elif self.current_index < 0:
            self.current_round += 1
            self.direction = 1
            self.current_index = 0


# One draft per guild (server)
guild_drafts = {}  # guild_id -> DraftState


# ===============================
# EMBED HELPER
# ===============================

def build_pool_embed(draft: DraftState) -> discord.Embed:
    """Create an embed showing remaining items in the pool."""
    remaining = [i for i in draft.available_items if i not in draft.picked_items]

    embed = discord.Embed(
        title="Remaining Draft Pool",
        description=f"{len(remaining)} items left",
    )

    if not remaining:
        embed.description = "No remaining items in the pool."
        return embed

    # Show up to 25 items nicely formatted
    max_show = 25
    lines = [f"- {name}" for name in remaining[:max_show]]
    more_count = len(remaining) - max_show

    text = "\n".join(lines)
    if more_count > 0:
        text += f"\n\n+ {more_count} more not shown..."

    embed.add_field(name="Available", value=text, inline=False)

    return embed


# ===============================
# EVENTS
# ===============================

@bot.event
async def on_ready():
    print(">>> on_ready FIRED <<<")
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    print("Guilds I'm in:", [f"{g.name} ({g.id})" for g in bot.guilds])
    print("------")


# ===============================
# AUTO-PICK LOGIC (TYPE NAME INSTEAD OF !pick)
# ===============================

async def try_auto_pick(message: discord.Message):
    """Try to interpret the user's message as a pick."""
    if message.guild is None:
        return  # ignore DMs

    guild_id = message.guild.id
    draft = guild_drafts.get(guild_id)

    # No active draft or not started / already done
    if draft is None or not draft.started or draft.completed:
        return

    member = message.author

    # Only react if it's this member's turn
    if draft.current_team() != member:
        return

    text = message.content.strip()
    if not text:
        return

    # Ignore bot commands (starting with the command prefix)
    if text.startswith(bot.command_prefix):
        return

    lowered = text.lower()

    # Remaining items
    remaining = [i for i in draft.available_items if i not in draft.picked_items]
    if not remaining:
        return

    # 1) Exact case-insensitive match
    exact_matches = [item for item in remaining if item.lower() == lowered]

    if len(exact_matches) == 1:
        matched_item = exact_matches[0]
    elif len(exact_matches) > 1:
        # This is unlikely, but handle it
        possibilities = ", ".join(exact_matches)
        await message.channel.send(
            f"⚠️ That matches multiple remaining items exactly: {possibilities}. "
            "Please type the full name more precisely."
        )
        return
    else:
        # 2) Partial substring match, case-insensitive
        partial_matches = [item for item in remaining if lowered in item.lower()]

        if len(partial_matches) == 1:
            matched_item = partial_matches[0]
        elif len(partial_matches) > 1:
            # Too many possibilities – ask to be more specific
            show = partial_matches[:10]
            options = "\n".join(f"- {name}" for name in show)
            extra = "" if len(partial_matches) <= 10 else f"\n(+ {len(partial_matches) - 10} more...)"
            await message.channel.send(
                f"⚠️ That text is ambiguous. Did you mean one of these?\n{options}{extra}\n"
                "Please type enough of the name to uniquely identify one item."
            )
            return
        else:
            # No match; silently ignore so people can chat
            return

    # At this point, matched_item is a unique, valid remaining item
    ok, error = draft.make_pick(member, matched_item)
    if not ok:
        await message.channel.send(f"❌ {error}")
        return

    # Confirm the pick
    await message.channel.send(f"✅ {member.mention} drafted **{matched_item}**!")

    # Show remaining pool as an embed
    pool_embed = build_pool_embed(draft)
    await message.channel.send(embed=pool_embed)

    # Turn / completion info
    if draft.completed:
        await message.channel.send("🎉 Draft is complete!")
    else:
        await message.channel.send(
            f"Next up: {draft.current_team().mention} (Round {draft.current_round})"
        )


@bot.event
async def on_message(message: discord.Message):
    # Ignore messages from bots
    if message.author.bot:
        return

    # Try to auto-pick based on plain text (no prefix)
    await try_auto_pick(message)

    # Still allow traditional commands like !startdraft, !status, etc.
    await bot.process_commands(message)


# ===============================
# UTILITY COMMANDS
# ===============================

@bot.command()
async def ping(ctx):
    """Simple test command."""
    await ctx.send("Pong!")


# ===============================
# DRAFT COMMANDS
# ===============================

@bot.command(name="startdraft")
async def start_draft(ctx, rounds: int):
    """
    Start a new draft.
    Example: !startdraft 5
    """
    guild_id = ctx.guild.id

    if guild_id in guild_drafts and not guild_drafts[guild_id].completed:
        await ctx.send("❌ A draft is already active in this server. Finish it or use `!forcestop`.")
        return

    draft = DraftState(owner_id=ctx.author.id, rounds=rounds)
    guild_drafts[guild_id] = draft

    await ctx.send(
        f"🎲 Draft created by {ctx.author.mention} for **{rounds} rounds**.\n"
        f"Others can join with `!join`.\n"
        f"The owner can set the pool with `!setpool` or `!setpooldm`, and can randomize order with `!fliporder`.\n"
        f"Once the draft starts, players just type the name (or part of the name) of the item to pick."
    )


@bot.command(name="join")
async def join_draft(ctx):
    """Join the active draft before it begins."""
    guild_id = ctx.guild.id
    draft = guild_drafts.get(guild_id)

    if draft is None:
        await ctx.send("❌ No active draft. Start one with `!startdraft`.")
        return

    if draft.started:
        await ctx.send("❌ Draft has already started. You cannot join now.")
        return

    draft.add_team(ctx.author)
    await ctx.send(f"✅ {ctx.author.mention} has joined the draft! Currently **{len(draft.teams)}** teams.")


@bot.command(name="setpool")
async def set_pool(ctx, *, items_text: str):
    """
    Set the draft pool as a comma-separated list (in-channel).
    Example:
    !setpool Patrick Mahomes, CeeDee Lamb, Christian McCaffrey
    """
    guild_id = ctx.guild.id
    draft = guild_drafts.get(guild_id)

    if draft is None:
        await ctx.send("❌ No active draft. Start one with `!startdraft`.")
        return

    if ctx.author.id != draft.owner_id:
        await ctx.send("❌ Only the draft owner can set the draft pool.")
        return

    items = [i.strip() for i in items_text.split(",") if i.strip()]
    if not items:
        await ctx.send("❌ You must provide at least one item.")
        return

    draft.set_pool(items)
    await ctx.send(
        f"✅ Draft pool set with **{len(items)}** items.\n"
        f"Players will be able to draft by simply typing the item name (case-insensitive, partials allowed)."
    )


@bot.command(name="setpooldm")
async def set_pool_dm(ctx):
    """
    Start a DM with the draft owner to set the draft pool.
    The owner will paste a list of items (comma- or newline-separated) in DM.
    """
    guild_id = ctx.guild.id
    draft = guild_drafts.get(guild_id)

    if draft is None:
        await ctx.send("❌ No active draft. Start one with `!startdraft`.")
        return

    # Only the draft owner can use this
    if ctx.author.id != draft.owner_id:
        await ctx.send("❌ Only the draft owner can set the draft pool.")
        return

    user = ctx.author

    # Try to DM the user
    try:
        await user.send(
            "👋 Let's set up your draft pool.\n\n"
            "Please send me a message containing **all items** you want in the pool.\n"
            "You can separate them by **commas** or put **one per line**.\n\n"
            "Example:\n"
            "`Patrick Mahomes, Josh Allen, Joe Burrow`\n"
            "or\n"
            "`Patrick Mahomes\nJosh Allen\nJoe Burrow`"
        )
    except discord.Forbidden:
        await ctx.send(
            "❌ I couldn't DM you. Please enable DMs from this server, or send me a DM first, "
            "then run `!setpooldm` again."
        )
        return

    await ctx.send(f"📩 {user.mention}, I've sent you a DM to set up the draft pool.")

    def check(m: discord.Message):
        return m.author.id == user.id and isinstance(m.channel, discord.DMChannel)

    try:
        # Wait up to 5 minutes for the DM response
        msg = await bot.wait_for("message", check=check, timeout=300)
    except asyncio.TimeoutError:
        await user.send("⏳ Setup timed out. Run `!setpooldm` again in the server when you're ready.")
        return

    # Parse the content into items (support commas and/or newlines)
    raw_text = msg.content.replace("\r\n", "\n")  # normalize newlines
    raw_text = raw_text.replace("\r", "\n")

    # Replace newlines with commas so we can split once
    combined = raw_text.replace("\n", ",")
    items = [i.strip() for i in combined.split(",") if i.strip()]

    if not items:
        await user.send("❌ I didn't find any valid items in your message. Please try `!setpooldm` again.")
        return

    # Set the pool
    draft.set_pool(items)

    await user.send(
        f"✅ Your draft pool has been set with **{len(items)}** items.\n"
        "You can now start the draft with `!begin` in the server."
    )

    # Let the server know (without spamming the item list)
    await ctx.send(
        f"✅ {user.mention} has set the draft pool via DM with **{len(items)}** items."
    )


@bot.command(name="fliporder")
async def flip_order(ctx):
    """
    Randomize the draft order using the teams that have joined.
    Only the draft owner can do this, and only before the draft begins.
    """
    guild_id = ctx.guild.id
    draft = guild_drafts.get(guild_id)

    if draft is None:
        await ctx.send("❌ No active draft. Start one with `!startdraft`.")
        return

    if draft.started:
        await ctx.send("❌ Draft has already started. You cannot change the order now.")
        return

    if ctx.author.id != draft.owner_id:
        await ctx.send("❌ Only the draft owner can randomize the draft order.")
        return

    if len(draft.teams) < 2:
        await ctx.send("❌ You need at least 2 teams joined to flip the order.")
        return

    draft.draft_order = list(draft.teams)
    random.shuffle(draft.draft_order)

    order_text = "\n".join(
        f"{i+1}. {member.display_name}" for i, member in enumerate(draft.draft_order)
    )

    await ctx.send(
        "🪙 **Randomized draft order:**\n"
        f"{order_text}\n\n"
        "Use `!begin` to start the draft with this order."
    )


@bot.command(name="begin")
async def begin_draft(ctx):
    """Begin the draft (lock teams and start snake order)."""
    guild_id = ctx.guild.id
    draft = guild_drafts.get(guild_id)

    if draft is None:
        await ctx.send("❌ No active draft.")
        return

    if ctx.author.id != draft.owner_id:
        await ctx.send("❌ Only the draft owner can begin the draft.")
        return

    if draft.started:
        await ctx.send("❌ Draft has already started.")
        return

    if len(draft.teams) < 2:
        await ctx.send("❌ You need at least 2 teams to begin the draft.")
        return

    if len(draft.available_items) == 0:
        await ctx.send("❌ You must set the draft pool first with `!setpool` or `!setpooldm`.")
        return

    draft.begin()
    order_names = ", ".join(member.display_name for member in draft.draft_order)

    await ctx.send(
        "🚨 **Draft has begun!**\n"
        f"**Order:** {order_names}\n"
        f"Current turn: {draft.current_team().mention} (Round {draft.current_round})\n"
        f"To pick, just type the item name (case-insensitive, partials allowed)."
    )

    # NEW: Show the pool immediately when the draft begins
    pool_embed = build_pool_embed(draft)
    await ctx.send(embed=pool_embed)


@bot.command(name="pick")
async def make_pick(ctx, *, item_name: str):
    """
    OPTIONAL: Make your draft pick via command.
    Example: !pick Patrick Mahomes
    (Normal flow now uses plain text picks instead.)
    """
    guild_id = ctx.guild.id
    draft = guild_drafts.get(guild_id)

    if draft is None:
        await ctx.send("❌ No active draft.")
        return

    ok, error = draft.make_pick(ctx.author, item_name)
    if not ok:
        await ctx.send(f"❌ {error}")
        return

    await ctx.send(f"✅ {ctx.author.mention} drafted **{item_name}**!")

    # Show remaining pool as an embed
    pool_embed = build_pool_embed(draft)
    await ctx.send(embed=pool_embed)

    # Turn / completion info
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
        await ctx.send("❌ No active draft.")
        return

    if not draft.started:
        await ctx.send(
            "Draft not started yet.\n"
            "Teams joined: "
            f"{', '.join(m.display_name for m in draft.teams) or 'none'}"
        )
        return

    if draft.completed:
        await ctx.send("✅ Draft is complete.")
        return

    await ctx.send(
        f"📊 **Draft Status**\n"
        f"Round: {draft.current_round}/{draft.rounds}\n"
        f"Current turn: {draft.current_team().mention}\n"
        f"Total picks made: {draft.total_picks_made()}/{draft.max_picks_total()}"
    )


@bot.command(name="mypicks")
async def my_picks(ctx):
    """Show your personal picks."""
    guild_id = ctx.guild.id
    draft = guild_drafts.get(guild_id)

    if draft is None:
        await ctx.send("❌ No active draft.")
        return

    picks = draft.picks_by_team.get(ctx.author.id, [])
    if not picks:
        await ctx.send("ℹ️ You have no picks yet.")
        return

    text = "\n".join(f"{i+1}. {p}" for i, p in enumerate(picks))
    await ctx.send(f"📜 **Your picks:**\n{text}")


@bot.command(name="pool")
async def show_pool(ctx):
    """Show remaining items in the draft pool."""
    guild_id = ctx.guild.id
    draft = guild_drafts.get(guild_id)

    if draft is None:
        await ctx.send("❌ No active draft.")
        return

    embed = build_pool_embed(draft)
    await ctx.send(embed=embed)


@bot.command(name="order")
async def show_order(ctx):
    """Show draft order (teams)."""
    guild_id = ctx.guild.id
    draft = guild_drafts.get(guild_id)

    if draft is None:
        await ctx.send("❌ No active draft.")
        return

    if not draft.draft_order:
        await ctx.send("ℹ️ Draft order not set yet. Use `!fliporder` or `!begin`.")
        return

    order = "\n".join(
        f"{i+1}. {m.display_name}" for i, m in enumerate(draft.draft_order)
    )
    await ctx.send(f"📋 **Draft Order:**\n{order}")

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



# ===============================
# FORCE STOP COMMAND
# ===============================

@bot.command(name="forcestop")
@commands.has_permissions(administrator=True)
async def force_stop(ctx):
    """
    Force stop the current draft (admin or draft owner).
    Deletes draft state for this server.
    """
    guild_id = ctx.guild.id
    draft = guild_drafts.get(guild_id)

    if draft is None:
        await ctx.send("❌ There is no active draft to stop.")
        return

    # Allow draft owner even if not admin
    if ctx.author.id != draft.owner_id and not ctx.author.guild_permissions.administrator:
        await ctx.send("❌ Only the draft owner or a server admin can force stop the draft.")
        return

    del guild_drafts[guild_id]
    await ctx.send("⛔ **The draft has been forcefully stopped.** All draft data has been cleared.")


# ===============================
# EXPORT DRAFT LOG
# ===============================

@bot.command(name="exportdraft")
async def export_draft(ctx):
    """
    Export the full draft log and upload it as a .txt file.
    """
    guild_id = ctx.guild.id
    draft = guild_drafts.get(guild_id)

    if draft is None:
        await ctx.send("❌ No active draft or draft data to export.")
        return

    filename = f"draft_log_{guild_id}.txt"

    with open(filename, "w", encoding="utf-8") as f:
        f.write("=== DRAFT LOG ===\n\n")
        f.write(f"Total Rounds: {draft.rounds}\n\n")

        f.write("Draft Order:\n")
        if draft.draft_order:
            for idx, member in enumerate(draft.draft_order):
                f.write(f"{idx + 1}. {member.display_name}\n")
        else:
            f.write("Draft order not set.\n")
        f.write("\n")

        for member in draft.draft_order:
            f.write(f"{member.display_name}'s Picks:\n")
            picks = draft.picks_by_team.get(member.id, [])
            if picks:
                for i, pick in enumerate(picks, 1):
                    f.write(f"  {i}. {pick}\n")
            else:
                f.write("  No picks.\n")
            f.write("\n")

    await ctx.send("📄 Draft log exported:", file=discord.File(filename))


# ===============================
# RUN THE BOT
# ===============================

print("TOKEN loaded? ", bool(TOKEN))
print("TOKEN preview: ", TOKEN[:6] + "..." if TOKEN else "None")

bot.run(TOKEN)
