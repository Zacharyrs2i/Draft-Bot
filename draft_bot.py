import asyncio
import os
import random

import discord
from discord.ext import commands
from dotenv import load_dotenv

# ===============================
# BOT SETUP
# ===============================

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

        self.teams: list[discord.Member] = []
        self.draft_order: list[discord.Member] = []

        self.current_round: int = 1
        self.current_index: int = 0  # index in draft_order
        self.direction: int = 1  # 1 = forward, -1 = backward (snake)
        self.started: bool = False
        self.completed: bool = False

        self.available_items: list[str] = []
        self.picks_by_team: dict[int, list[str]] = {}
        self.picked_items: set[str] = set()

        # Side logic
        self.item_sides: dict[str, str] = {}  # item -> side label (e.g., "Rangers")
        self.side_picks: dict[int, dict[str, int]] = {}  # user_id -> {side: count}

        # Timer
        self.turn_timer_task: asyncio.Task | None = None

        # Modes
        self.test_mode: bool = False  # allow single-team drafts for testing
        self.paused: bool = False  # pause / resume support
        self.banter_enabled: bool = False  # trash talk mode

        # Where to post announcements in this guild
        self.announce_channel_id: int | None = None

        # Has the final wrapup been sent already?
        self.wrapup_sent: bool = False

    # ---------------------------
    # Core state helpers
    # ---------------------------

    def add_team(self, member: discord.Member):
        """Add a team to the draft if not already there."""
        if member not in self.teams:
            self.teams.append(member)
            self.picks_by_team[member.id] = []

    def set_pool(self, items: list[str], item_sides: dict[str, str] | None = None):
        """Set the list of available draft items."""
        self.available_items = items
        self.picked_items = set()
        self.item_sides = item_sides or {}
        self.side_picks = {}

    def begin(self):
        """Lock teams and start the draft."""
        if not self.draft_order:
            self.draft_order = list(self.teams)

        self.current_round = 1
        self.current_index = 0
        self.direction = 1
        self.started = True
        self.completed = False
        self.paused = False
        self.wrapup_sent = False

    def current_team(self) -> discord.Member | None:
        """Return the member whose turn it is now."""
        if not self.started or self.completed or not self.draft_order:
            return None
        if self.current_index < 0 or self.current_index >= len(self.draft_order):
            return None
        return self.draft_order[self.current_index]

    def total_picks_for_team(self, member_id: int) -> int:
        return len(self.picks_by_team.get(member_id, []))

    def total_picks_made(self) -> int:
        return sum(len(picks) for picks in self.picks_by_team.values())

    def max_picks_total(self) -> int:
        return self.rounds * len(self.draft_order) if self.draft_order else 0

    # ---------------------------
    # Side balancing logic
    # ---------------------------

    def _remaining_items_by_side(self) -> dict[str, int]:
        """Helper: how many remaining items per side label."""
        counts: dict[str, int] = {}
        for item in self.available_items:
            if item in self.picked_items:
                continue
            side = self.item_sides.get(item)
            if not side:
                continue
            counts[side] = counts.get(side, 0) + 1
        return counts

    def _side_pick_allowed(self, member_id: int, side: str) -> tuple[bool, str | None]:
        """
        1-for-1 style side balancing:

        - Applies when there are exactly 2 distinct sides
          (e.g., Rangers / Regulars) and rounds >= 2.
        - Each member is limited to floor(rounds / 2) picks per side.
        - Once the OTHER side has **no remaining items**, the limit is relaxed.
        """
        if not self.item_sides:
            return True, None

        all_sides = set(self.item_sides.values())
        # We only enforce this special rule for exactly 2 sides
        if len(all_sides) != 2 or self.rounds < 2:
            return True, None

        max_per_side = self.rounds // 2
        if max_per_side <= 0:
            return True, None

        # How many picks this member already has for this side
        counts_for_member = self.side_picks.get(member_id, {})
        current_for_side = counts_for_member.get(side, 0)

        # Under the limit? Always okay.
        if current_for_side < max_per_side:
            return True, None

        # At/over the limit: only allowed if the "other" side has zero remaining items.
        other_side = next(s for s in all_sides if s != side)
        remaining_by_side = self._remaining_items_by_side()
        other_remaining = remaining_by_side.get(other_side, 0)

        if other_remaining > 0:
            msg = (
                f"You've already taken the maximum number of **{side}** picks "
                f"({max_per_side}). Your remaining picks must be from **{other_side}** "
                "while there are still options left on that side."
            )
            return False, msg

        # The other side is exhausted → we relax the rule and allow more of this side.
        return True, None

    # ---------------------------
    # Picking logic
    # ---------------------------

    def can_pick(self, member: discord.Member, item_name: str) -> tuple[bool, str | None]:
        """Check if this member can pick this item now."""
        if not self.started or self.completed:
            return False, "Draft has not started or is already complete."

        if self.paused:
            return False, "The draft is currently paused."

        if self.current_team() != member:
            return False, "It is not your turn."

        if item_name not in self.available_items:
            return False, "That item is not in the draft pool."

        if item_name in self.picked_items:
            return False, "That item has already been drafted."

        if self.total_picks_for_team(member.id) >= self.rounds:
            return False, "You have already made all your picks."

        side = self.item_sides.get(item_name)
        if side is not None:
            ok, msg = self._side_pick_allowed(member.id, side)
            if not ok:
                return False, msg

        return True, None

    def make_pick(self, member: discord.Member, item_name: str) -> tuple[bool, str | None]:
        """Record a pick and advance the turn."""
        ok, error = self.can_pick(member, item_name)
        if not ok:
            return False, error

        # Record pick
        self.picks_by_team.setdefault(member.id, []).append(item_name)
        self.picked_items.add(item_name)

        # Track side usage
        side = self.item_sides.get(item_name)
        if side:
            if member.id not in self.side_picks:
                self.side_picks[member.id] = {}
            self.side_picks[member.id][side] = self.side_picks[member.id].get(side, 0) + 1

        # Stop any active timer
        self.cancel_timer()

        # Advance turn
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

        # Went past the end (right side)
        if self.current_index >= len(self.draft_order):
            self.current_round += 1
            self.direction = -1
            self.current_index = len(self.draft_order) - 1

        # Went past the beginning (left side)
        elif self.current_index < 0:
            self.current_round += 1
            self.direction = 1
            self.current_index = 0

    def cancel_timer(self):
        """Cancel the current turn timer, if any."""
        if self.turn_timer_task and not self.turn_timer_task.done():
            self.turn_timer_task.cancel()
        self.turn_timer_task = None


# One draft per guild (server)
guild_drafts: dict[int, DraftState] = {}


# ===============================
# TIMER HELPERS
# ===============================

def parse_timer_duration(text: str) -> int | None:
    normalized = text.lower().replace(" ", "")
    allowed: dict[str, int] = {
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


def format_duration(seconds: int) -> str:
    if seconds % 60 == 0:
        minutes = seconds // 60
        return f"{minutes} minute{'s' if minutes != 1 else ''}"
    return f"{seconds} seconds"


async def run_turn_timer(ctx: commands.Context, draft: DraftState, duration_seconds: int):
    team = draft.current_team()
    round_number = draft.current_round
    if team is None:
        return

    try:
        await asyncio.sleep(duration_seconds)
    except asyncio.CancelledError:
        return

    # Ensure this is still the active timer
    if draft.turn_timer_task != asyncio.current_task():
        return

    draft.turn_timer_task = None

    # Ensure state hasn't advanced or been paused/completed
    if draft.completed or draft.paused or draft.current_team() != team or draft.current_round != round_number:
        return

    await ctx.send(f"⏰ Time's up for {team.mention}! Please make your pick.")


# ===============================
# POOL PARSING (WITH SIDES)
# ===============================

def parse_pool_with_sides(text: str) -> list[tuple[str, list[str]]]:
    """
    Parse strings like:
    'Rangers: A, B, C | Regulars: D, E, F'

    Returns list of (side_name, [items...])
    """
    groups: list[tuple[str, list[str]]] = []
    for chunk in text.split("|"):
        chunk = chunk.strip()
        if not chunk:
            continue
        if ":" not in chunk:
            continue
        side, items_part = chunk.split(":", 1)
        side = side.strip()
        items = [i.strip() for i in items_part.split(",") if i.strip()]
        if not side or not items:
            continue
        groups.append((side, items))
    return groups


# ===============================
# EMBED & MESSAGE HELPERS
# ===============================

def build_pool_embed(draft: DraftState) -> discord.Embed:
    """Create an embed showing remaining items in the pool.

    If there are exactly two sides (e.g., Rangers / Regulars), show a
    two-column table:
        Rangers | Regulars
    Otherwise, fall back to a simple list.
    """
    remaining = [i for i in draft.available_items if i not in draft.picked_items]

    embed = discord.Embed(
        title="Remaining Draft Pool",
        description=f"{len(remaining)} items left",
    )

    if not remaining:
        embed.description = "No remaining items in the pool."
        return embed

    if draft.item_sides:
        grouped: dict[str, list[str]] = {}
        for item in remaining:
            side = draft.item_sides.get(item, "Unspecified")
            grouped.setdefault(side, []).append(item)

        sides = list(grouped.keys())
        if len(sides) == 2:
            left_side, right_side = sides[0], sides[1]
            left_items = grouped[left_side]
            right_items = grouped[right_side]

            max_rows = max(len(left_items), len(right_items))
            max_rows_shown = min(max_rows, 20)

            header = f"{left_side:<16} | {right_side:<16}"
            divider = "-" * len(header)
            rows = [header, divider]

            for i in range(max_rows_shown):
                left_name = left_items[i] if i < len(left_items) else ""
                right_name = right_items[i] if i < len(right_items) else ""
                rows.append(f"{left_name:<16} | {right_name:<16}")

            table_text = "```text\n" + "\n".join(rows) + "\n```"

            extra = ""
            if max_rows > max_rows_shown:
                extra = f"\n(+ {max_rows - max_rows_shown} more rows not shown)"

            embed.description = (
                f"{len(remaining)} items left\n"
                f"{left_side}: {len(left_items)}, {right_side}: {len(right_items)}"
            )
            embed.add_field(
                name="Available by side",
                value=table_text + extra,
                inline=False,
            )
            return embed

    # Fallback: simple list
    max_show = 25
    lines = [f"- {name}" for name in remaining[:max_show]]
    more_count = len(remaining) - max_show

    text = "\n".join(lines)
    if more_count > 0:
        text += f"\n\n+ {more_count} more not shown..."

    embed.add_field(name="Available", value=text, inline=False)
    return embed


def get_side_hint(draft: DraftState) -> str:
    """Return a hint about which side(s) the next player can/must pick from."""
    member = draft.current_team()
    if member is None or not draft.item_sides:
        return ""

    # Determine all sides and remaining items per side
    all_sides = set(draft.item_sides.values())
    if not all_sides:
        return ""

    remaining_by_side = draft._remaining_items_by_side()
    if not remaining_by_side:
        return ""

    allowed_sides: list[str] = []
    blocked_reasons: dict[str, str] = {}

    for side in all_sides:
        if remaining_by_side.get(side, 0) <= 0:
            continue
        ok, msg = draft._side_pick_allowed(member.id, side)
        if ok:
            allowed_sides.append(side)
        elif msg:
            blocked_reasons[side] = msg

    if len(allowed_sides) == 0:
        return "No valid sides remain for your picks."

    if len(allowed_sides) == 1:
        side = allowed_sides[0]
        other_sides = [s for s in all_sides if s != side and s in blocked_reasons]
        if other_sides:
            others_str = ", ".join(other_sides)
            return f"Your next pick must be from **{side}** (you cannot pick from {others_str})."
        return f"Your next pick must be from **{side}**."

    if len(allowed_sides) == 2:
        a, b = allowed_sides[0], allowed_sides[1]
        return f"You may pick from **{a}** or **{b}**."

    sides_str = ", ".join(allowed_sides)
    return f"You may pick from: {sides_str}."


def build_next_turn_message(draft: DraftState) -> str:
    """Combine next-up announcement with side hint."""
    member = draft.current_team()
    if member is None:
        return "No active turn."

    base = f"Next up: {member.mention} (Round {draft.current_round})"
    hint = get_side_hint(draft)
    if hint:
        return base + "\n" + hint
    return base


async def maybe_send_banter_after_pick(
    channel: discord.abc.Messageable,
    draft: DraftState,
    picker: discord.Member,
    item_name: str,
):
    """Send a random banter line if banter mode is enabled."""
    if not draft.banter_enabled:
        return

    # Basic 40% chance per normal pick
    if not draft.completed and random.random() > 0.4:
        return

    side = draft.item_sides.get(item_name)
    side_label = f" ({side})" if side else ""

    generic_lines = [
        f"🔥 Bold pick, {picker.display_name}. Let's see if it pays off.",
        f"💀 That’s either genius or madness, {picker.display_name}. No in-between.",
        f"🎲 Interesting choice, {picker.display_name}. I would've panic-picked by now.",
    ]

    side_lines = [
        f"⚔️ Another one for **{side}**. The other side looks nervous.",
        f"🛡️ **{side}** just gained a new victim—uh, teammate.",
    ] if side else []

    finale_lines = [
        "🎉 Draft complete! Time to pretend every pick was part of the plan.",
        "🏁 That’s the draft! May your choices age like fine wine and not milk.",
    ]

    # Special: end-of-draft banter
    if draft.completed:
        line = random.choice(finale_lines)
        await channel.send(line)
        return

    # Normal pick banter
    candidates = generic_lines + side_lines
    if not candidates:
        return
    line = random.choice(candidates)
    await channel.send(line)


def build_teams_table(draft: DraftState) -> str:
    """Build a text table of each team's picks, side-by-side."""
    teams = draft.draft_order if draft.draft_order else draft.teams
    if not teams:
        return ""

    picks_by_team = [draft.picks_by_team.get(team.id, []) for team in teams]
    column_widths: list[int] = []
    for team, picks in zip(teams, picks_by_team):
        longest_pick = max((len(pick) for pick in picks), default=0)
        column_widths.append(max(len(team.display_name), longest_pick) + 2)

    header = " | ".join(
        team.display_name.ljust(width) for team, width in zip(teams, column_widths)
    )

    max_rows = max((len(picks) for picks in picks_by_team), default=0)
    if max_rows == 0:
        return header

    rows = []
    for i in range(max_rows):
        row = " | ".join(
            (picks[i] if i < len(picks) else "").ljust(width)
            for picks, width in zip(picks_by_team, column_widths)
        )
        rows.append(row)

    table = "\n".join([header] + rows)
    return table


def write_draft_log_file(draft: DraftState, guild_id: int) -> str:
    """Write the draft log to a .txt file and return the filename."""
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

        teams = draft.draft_order if draft.draft_order else draft.teams
        for member in teams:
            f.write(f"{member.display_name}'s Picks:\n")
            picks = draft.picks_by_team.get(member.id, [])
            if picks:
                for i, pick in enumerate(picks, 1):
                    f.write(f"  {i}. {pick}\n")
            else:
                f.write("  No picks.\n")
            f.write("\n")

    return filename


async def send_draft_wrapup(channel: discord.abc.Messageable, draft: DraftState, guild_id: int):
    """
    Send a final wrapup summary and automatically export the draft log.
    Only runs once per draft (guarded by draft.wrapup_sent).
    """
    if draft.wrapup_sent:
        return
    draft.wrapup_sent = True

    # 1) Announce completion
    await channel.send("🎉 Draft is complete!")

    # 2) Final board (teams table)
    table = build_teams_table(draft)
    if table:
        await channel.send(f"📋 **Final Draft Board:**\n```{table}```")

    # 3) Side totals, if using sides
    if draft.item_sides:
        # Overall side totals
        side_totals: dict[str, int] = {}
        per_team_side_totals: dict[int, dict[str, int]] = {}

        for member_id, picks in draft.picks_by_team.items():
            for pick in picks:
                side = draft.item_sides.get(pick)
                if not side:
                    continue
                side_totals[side] = side_totals.get(side, 0) + 1

                team_counts = per_team_side_totals.setdefault(member_id, {})
                team_counts[side] = team_counts.get(side, 0) + 1

        if side_totals:
            overall_lines = [f"{side}: {count}" for side, count in side_totals.items()]
            overall_text = ", ".join(overall_lines)
            await channel.send(f"⚖️ **Side Totals:** {overall_text}")

            # Per-team breakdown
            lines = []
            teams = draft.draft_order if draft.draft_order else draft.teams
            for member in teams:
                counts = per_team_side_totals.get(member.id, {})
                if not counts:
                    continue
                parts = [f"{side}: {count}" for side, count in counts.items()]
                parts_text = ", ".join(parts)
                lines.append(f"{member.display_name} → {parts_text}")

            if lines:
                await channel.send("📊 **Per-Team Side Breakdown:**\n" + "\n".join(lines))

    # 4) Auto-export log file
    filename = write_draft_log_file(draft, guild_id)
    await channel.send("📄 Final draft log exported automatically:", file=discord.File(filename))


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
# AUTO-PICK LOGIC
# (TYPE NAME INSTEAD OF !pick)
# ===============================

async def try_auto_pick(message: discord.Message):
    """Try to interpret the user's message as a pick."""
    if message.guild is None:
        return  # ignore DMs for autopick

    guild_id = message.guild.id
    draft = guild_drafts.get(guild_id)

    # No active draft or not started / already done
    if draft is None or not draft.started or draft.completed:
        return

    member = message.author

    # Only react if it's this member's turn
    if draft.current_team() != member:
        return

    if draft.paused:
        await message.channel.send("❌ The draft is currently paused. Please wait for `!resume`.")
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

    # Banter (if enabled)
    await maybe_send_banter_after_pick(message.channel, draft, member, matched_item)

    # Turn / completion info + wrapup
    if draft.completed:
        await send_draft_wrapup(message.channel, draft, message.guild.id)
    else:
        await message.channel.send(build_next_turn_message(draft))


@bot.event
async def on_message(message: discord.Message):
    # Ignore messages from bots
    if message.author.bot:
        return

    # In guild text channels, try auto-pick then commands
    if message.guild is not None:
        await try_auto_pick(message)
        await bot.process_commands(message)
        return

    # In DMs, just process commands (if you ever add any)
    await bot.process_commands(message)


# ===============================
# UTILITY COMMANDS
# ===============================

@bot.command()
async def ping(ctx: commands.Context):
    """Simple test command."""
    await ctx.send("Pong!")


# ===============================
# DRAFT COMMANDS
# ===============================

@bot.command(name="startdraft")
async def start_draft(ctx: commands.Context, rounds: int):
    """
    Start a new draft.
    Example: !startdraft 5
    """
    if ctx.guild is None:
        await ctx.send("❌ This command can only be used in a server.")
        return

    guild_id = ctx.guild.id

    if guild_id in guild_drafts and not guild_drafts[guild_id].completed:
        await ctx.send("❌ A draft is already active in this server. Finish it or use `!forcestop`.")
        return

    draft = DraftState(owner_id=ctx.author.id, rounds=rounds)
    draft.announce_channel_id = ctx.channel.id
    guild_drafts[guild_id] = draft

    await ctx.send(
        f"🎲 Draft created by {ctx.author.mention} for **{rounds} rounds**.\n"
        f"Others can join with `!join`.\n"
        f"The owner can set the pool with `!setpool` or `!setpooldm`, and can randomize order with `!fliporder` or `!coinflip`.\n"
        f"Once the draft starts, players just type the name (or part of the name) of the item to pick."
    )


@bot.command(name="testmode")
async def enable_test_mode(ctx: commands.Context):
    """
    Enable test mode for the current draft.
    In test mode, you are allowed to begin the draft with only one team.
    Great for solo testing.
    """
    if ctx.guild is None:
        await ctx.send("❌ This command can only be used in a server.")
        return

    guild_id = ctx.guild.id
    draft = guild_drafts.get(guild_id)

    if draft is None:
        await ctx.send("❌ No active draft. Start one with `!startdraft` first.")
        return

    if ctx.author.id != draft.owner_id:
        await ctx.send("❌ Only the draft owner can enable test mode.")
        return

    draft.test_mode = True
    await ctx.send(
        "🧪 **Test mode enabled.** You can now begin the draft with only one team (yourself) for testing."
    )


@bot.command(name="join")
async def join_draft(ctx: commands.Context):
    """Join the active draft before it begins."""
    if ctx.guild is None:
        await ctx.send("❌ This command can only be used in a server.")
        return

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
async def set_pool(ctx: commands.Context, *, items_text: str):
    """
    Set the draft pool as a comma-separated list (in-channel).
    Example:
    !setpool Patrick Mahomes, CeeDee Lamb, Christian McCaffrey

    You can also group by side for clarity (e.g., real teams):
    !setpool Rangers: Option 1, Option 2 | Regulars: Option 3, Option 4
    """
    if ctx.guild is None:
        await ctx.send("❌ This command can only be used in a server.")
        return

    guild_id = ctx.guild.id
    draft = guild_drafts.get(guild_id)

    if draft is None:
        await ctx.send("❌ No active draft. Start one with `!startdraft`.")
        return

    if ctx.author.id != draft.owner_id:
        await ctx.send("❌ Only the draft owner can set the draft pool.")
        return

    side_groups = parse_pool_with_sides(items_text)

    if side_groups:
        items: list[str] = []
        item_sides: dict[str, str] = {}
        for side, entries in side_groups:
            for item in entries:
                if item in item_sides:
                    await ctx.send(f"❌ Duplicate item detected: `{item}`")
                    return
                items.append(item)
                item_sides[item] = side
    else:
        items = [i.strip() for i in items_text.split(",") if i.strip()]
        item_sides = {}

    if not items:
        await ctx.send("❌ You must provide at least one item.")
        return

    draft.set_pool(items, item_sides)

    if item_sides:
        counts_by_side: dict[str, int] = {}
        for side in item_sides.values():
            counts_by_side[side] = counts_by_side.get(side, 0) + 1
        side_counts = ", ".join(f"{side}: {count}" for side, count in counts_by_side.items())
        await ctx.send(
            f"✅ Draft pool set with **{len(items)} items** across sides.\n"
            f"Breakdown — {side_counts}"
        )
    else:
        await ctx.send(
            f"✅ Draft pool set with **{len(items)} items**.\n"
            f"Players will be able to draft by simply typing the item name (case-insensitive, partials allowed)."
        )


@bot.command(name="setpooldm")
async def set_pool_dm(ctx: commands.Context):
    """
    Start a DM with the draft owner to set the draft pool.
    The owner will paste a list of items (comma- or newline-separated) in DM.
    """
    if ctx.guild is None:
        await ctx.send("❌ This command can only be used in a server.")
        return

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

    combined = raw_text.replace("\n", ",")
    items = [i.strip() for i in combined.split(",") if i.strip()]

    if not items:
        await user.send("❌ I didn't find any valid items in your message. Please try `!setpooldm` again.")
        return

    draft.set_pool(items, {})

    await user.send(
        f"✅ Your draft pool has been set with **{len(items)}** items.\n"
        "You can now start the draft with `!begin` in the server."
    )

    await ctx.send(
        f"✅ {user.mention} has set the draft pool via DM with **{len(items)}** items."
    )


@bot.command(name="fliporder")
async def flip_order(ctx: commands.Context):
    """
    Randomize the draft order using the teams that have joined.
    Only the draft owner can do this, and only before the draft begins.
    """
    if ctx.guild is None:
        await ctx.send("❌ This command can only be used in a server.")
        return

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

    if len(draft.teams) < 2 and not draft.test_mode:
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


@bot.command(name="coinflip")
async def coinflip(ctx: commands.Context):
    """
    Flip a coin between exactly two joined teams to determine who gets first pick.
    Sets the draft order to [winner, loser].
    """
    if ctx.guild is None:
        await ctx.send("❌ This command can only be used in a server.")
        return

    guild_id = ctx.guild.id
    draft = guild_drafts.get(guild_id)

    if draft is None:
        await ctx.send("❌ No active draft. Start one with `!startdraft` first.")
        return

    if draft.started:
        await ctx.send("❌ Draft has already started. You cannot coinflip the order now.")
        return

    if ctx.author.id != draft.owner_id:
        await ctx.send("❌ Only the draft owner can run the coinflip.")
        return

    if len(draft.teams) != 2:
        await ctx.send(
            "❌ Coinflip is only supported when exactly **2** teams have joined.\n"
            "Make sure exactly two players have used `!join`."
        )
        return

    # Randomly choose winner & loser
    winner = random.choice(draft.teams)
    loser = draft.teams[0] if draft.teams[1] == winner else draft.teams[1]

    draft.draft_order = [winner, loser]

    await ctx.send(
        "🪙 **Coinflip result!**\n"
        f"Winner: {winner.mention} — you get **first pick**.\n"
        f"Draft order will be:\n"
        f"1️⃣ {winner.display_name}\n"
        f"2️⃣ {loser.display_name}\n\n"
        "Use `!begin` to start the draft with this order."
    )


@bot.command(name="begin")
async def begin_draft(ctx: commands.Context):
    """Begin the draft (lock teams and start snake order)."""
    if ctx.guild is None:
        await ctx.send("❌ This command can only be used in a server.")
        return

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

    if len(draft.teams) < 2 and not draft.test_mode:
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
        f"{build_next_turn_message(draft)}\n"
        f"To pick, just type the item name (case-insensitive, partials allowed)."
    )

    pool_embed = build_pool_embed(draft)
    await ctx.send(embed=pool_embed)


@bot.command(name="pick")
async def make_pick_command(ctx: commands.Context, *, item_name: str):
    """
    OPTIONAL: Make your draft pick via command.
    Example: !pick Patrick Mahomes
    (Normal flow now uses plain text picks instead.)
    """
    if ctx.guild is None:
        await ctx.send("❌ This command can only be used in a server.")
        return

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

    pool_embed = build_pool_embed(draft)
    await ctx.send(embed=pool_embed)

    await maybe_send_banter_after_pick(ctx.channel, draft, ctx.author, item_name)

    if draft.completed:
        await send_draft_wrapup(ctx.channel, draft, ctx.guild.id)
    else:
        await ctx.send(build_next_turn_message(draft))


@bot.command(name="status")
async def draft_status(ctx: commands.Context):
    """Show current draft status."""
    if ctx.guild is None:
        await ctx.send("❌ This command can only be used in a server.")
        return

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

    status_lines = [
        f"📊 **Draft Status**",
        f"Round: {draft.current_round}/{draft.rounds}",
        f"Total picks made: {draft.total_picks_made()}/{draft.max_picks_total()}",
    ]

    if draft.paused:
        status_lines.append("State: ⏸️ Paused")
    elif draft.completed:
        status_lines.append("State: ✅ Complete")
    else:
        status_lines.append(f"Current turn: {draft.current_team().mention}")

    await ctx.send("\n".join(status_lines))


@bot.command(name="mypicks")
async def my_picks(ctx: commands.Context):
    """Show your personal picks."""
    if ctx.guild is None:
        await ctx.send("❌ This command can only be used in a server.")
        return

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
async def show_pool(ctx: commands.Context):
    """Show remaining items in the draft pool."""
    if ctx.guild is None:
        await ctx.send("❌ This command can only be used in a server.")
        return

    guild_id = ctx.guild.id
    draft = guild_drafts.get(guild_id)

    if draft is None:
        await ctx.send("❌ No active draft.")
        return

    remaining = [i for i in draft.available_items if i not in draft.picked_items]

    if not remaining:
        await ctx.send("ℹ️ No remaining items in the pool.")
        return

    if draft.item_sides:
        grouped: dict[str, list[str]] = {}
        for item in remaining:
            side = draft.item_sides.get(item, "Unspecified")
            grouped.setdefault(side, []).append(item)

        lines = []
        for side, items in grouped.items():
            display = ", ".join(items[:10])
            extra = "" if len(items) <= 10 else f" (+{len(items) - 10} more)"
            lines.append(f"{side}: {display}{extra}")

        lines_text = "\n".join(lines)
        await ctx.send(
            f"📦 Remaining items by side ({len(remaining)} total):\n{lines_text}"
        )
    else:
        embed = build_pool_embed(draft)
        await ctx.send(embed=embed)


@bot.command(name="order")
async def show_order(ctx: commands.Context):
    """Show draft order (teams)."""
    if ctx.guild is None:
        await ctx.send("❌ This command can only be used in a server.")
        return

    guild_id = ctx.guild.id
    draft = guild_drafts.get(guild_id)

    if draft is None:
        await ctx.send("❌ No active draft.")
        return

    if not draft.draft_order:
        await ctx.send("ℹ️ Draft order not set yet. Use `!fliporder`, `!coinflip`, or `!begin`.")
        return

    order = "\n".join(
        f"{i+1}. {m.display_name}" for i, m in enumerate(draft.draft_order)
    )
    await ctx.send(f"📋 **Draft Order:**\n{order}")


@bot.command(name="teams")
async def show_teams(ctx: commands.Context):
    """Show each team's picks in a side-by-side table."""
    if ctx.guild is None:
        await ctx.send("❌ This command can only be used in a server.")
        return

    guild_id = ctx.guild.id
    draft = guild_drafts.get(guild_id)

    if draft is None:
        await ctx.send("❌ No active draft.")
        return

    table = build_teams_table(draft)
    if not table:
        await ctx.send("ℹ️ No teams have joined the draft yet.")
        return

    await ctx.send(f"📋 Teams:\n```{table}```")


@bot.command(name="timer")
async def start_timer(ctx: commands.Context, *, duration: str):
    """Start a turn timer (owner only). Options: 30s, 1m, 90s, 2m."""
    if ctx.guild is None:
        await ctx.send("❌ This command can only be used in a server.")
        return

    guild_id = ctx.guild.id
    draft = guild_drafts.get(guild_id)

    if draft is None:
        await ctx.send("❌ No active draft.")
        return

    if ctx.author.id != draft.owner_id:
        await ctx.send("❌ Only the draft creator can start a timer.")
        return

    if not draft.started:
        await ctx.send("❌ You need to begin the draft before starting a timer.")
        return

    if draft.completed:
        await ctx.send("❌ Draft is already complete.")
        return

    seconds = parse_timer_duration(duration)
    if seconds is None:
        await ctx.send("❌ Invalid duration. Choose one of: 30s, 1m, 90s, 2m.")
        return

    team = draft.current_team()
    if team is None:
        await ctx.send("❌ There is no active turn to time.")
        return

    draft.cancel_timer()

    task = asyncio.create_task(run_turn_timer(ctx, draft, seconds))
    draft.turn_timer_task = task

    await ctx.send(
        f"⏳ Timer started for {team.mention}: {format_duration(seconds)}."
    )


# ===============================
# PAUSE / RESUME COMMANDS
# ===============================

@bot.command(name="pause")
async def pause_draft(ctx: commands.Context):
    """Pause the current draft (no picks allowed while paused)."""
    if ctx.guild is None:
        await ctx.send("❌ This command can only be used in a server.")
        return

    guild_id = ctx.guild.id
    draft = guild_drafts.get(guild_id)

    if draft is None:
        await ctx.send("❌ No active draft to pause.")
        return

    if draft.completed:
        await ctx.send("❌ The draft is already complete.")
        return

    # Only owner or admin
    if ctx.author.id != draft.owner_id and not ctx.author.guild_permissions.administrator:
        await ctx.send("❌ Only the draft owner or a server admin can pause the draft.")
        return

    if draft.paused:
        await ctx.send("⏸️ The draft is already paused.")
        return

    draft.paused = True
    draft.cancel_timer()

    await ctx.send(f"⏸️ Draft paused by {ctx.author.mention}. No picks can be made until `!resume`.")


@bot.command(name="resume")
async def resume_draft(ctx: commands.Context):
    """Resume a previously paused draft."""
    if ctx.guild is None:
        await ctx.send("❌ This command can only be used in a server.")
        return

    guild_id = ctx.guild.id
    draft = guild_drafts.get(guild_id)

    if draft is None:
        await ctx.send("❌ No active draft to resume.")
        return

    if draft.completed:
        await ctx.send("❌ The draft is already complete.")
        return

    if ctx.author.id != draft.owner_id and not ctx.author.guild_permissions.administrator:
        await ctx.send("❌ Only the draft owner or a server admin can resume the draft.")
        return

    if not draft.paused:
        await ctx.send("▶️ The draft is not currently paused.")
        return

    draft.paused = False
    await ctx.send(f"▶️ Draft resumed by {ctx.author.mention}.\n{build_next_turn_message(draft)}")


# ===============================
# BANTER TOGGLE
# ===============================

@bot.command(name="banter")
async def toggle_banter(ctx: commands.Context, mode: str):
    """Toggle banter mode on/off for this draft."""
    if ctx.guild is None:
        await ctx.send("❌ This command can only be used in a server.")
        return

    guild_id = ctx.guild.id
    draft = guild_drafts.get(guild_id)

    if draft is None:
        await ctx.send("❌ No active draft to configure.")
        return

    if ctx.author.id != draft.owner_id and not ctx.author.guild_permissions.administrator:
        await ctx.send("❌ Only the draft owner or a server admin can change banter mode.")
        return

    mode_lower = mode.lower()
    if mode_lower in ("on", "enable", "enabled"):
        draft.banter_enabled = True
        await ctx.send("😈 Banter mode enabled. Expect sarcasm.")
    elif mode_lower in ("off", "disable", "disabled"):
        draft.banter_enabled = False
        await ctx.send("🤐 Banter mode disabled. I'll behave now.")
    else:
        await ctx.send("❌ Invalid mode. Use `!banter on` or `!banter off`.")


# ===============================
# FORCE STOP COMMAND
# ===============================

@bot.command(name="forcestop")
@commands.has_permissions(administrator=True)
async def force_stop(ctx: commands.Context):
    """
    Force stop the current draft (admin or draft owner).
    Deletes draft state for this server.
    """
    if ctx.guild is None:
        await ctx.send("❌ This command can only be used in a server.")
        return

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
async def export_draft(ctx: commands.Context):
    """
    Export the full draft log and upload it as a .txt file.
    """
    if ctx.guild is None:
        await ctx.send("❌ This command can only be used in a server.")
        return

    guild_id = ctx.guild.id
    draft = guild_drafts.get(guild_id)

    if draft is None:
        await ctx.send("❌ No active draft or draft data to export.")
        return

    filename = write_draft_log_file(draft, guild_id)
    await ctx.send("📄 Draft log exported:", file=discord.File(filename))


# ===============================
# RUN THE BOT
# ===============================

def load_token() -> str:
    """Load the Discord token from the environment."""
    load_dotenv()
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise RuntimeError("DISCORD_TOKEN is not set in .env")
    return token


if __name__ == "__main__":
    token = load_token()

    print("TOKEN loaded? ", bool(token))
    print("TOKEN preview: ", token[:6] + "..." if token else "None")

    bot.run(token)
