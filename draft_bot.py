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
# CONSTANTS (safety guardrails)
# ===============================

MIN_PARTIAL_MATCH_LEN = 4  # partial substring matching requires at least this many chars

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
        self.side_order: list[str] = []  # stable order [Side A, Side B] based on pool entry order

        # Draft channel lock (set on !begin)
        self.draft_channel_id: int | None = None

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

        # Pick history (for !undo). Each entry stores state BEFORE a pick was applied.
        self.pick_history: list[dict] = []

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
        self.side_order = []
        self.pick_history = []

        # If exactly two sides exist, set a stable A/B order based on first-seen side in available_items
        if self.item_sides:
            seen: list[str] = []
            for item in self.available_items:
                side = self.item_sides.get(item)
                if side and side not in seen:
                    seen.append(side)
                if len(seen) >= 2:
                    break
            if len(seen) == 2:
                self.side_order = [seen[0], seen[1]]

    def begin(self, channel_id: int | None = None):
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
        self.pick_history = []
        if channel_id is not None:
            self.draft_channel_id = channel_id

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
    # Side logic (alternation A/B)
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

    def _sample_remaining_items_for_side(self, side: str, limit: int = 10) -> list[str]:
        """Return up to `limit` remaining items that belong to a specific side."""
        out: list[str] = []
        for item in self.available_items:
            if item in self.picked_items:
                continue
            if self.item_sides.get(item) == side:
                out.append(item)
            if len(out) >= limit:
                break
        return out

    def _side_pick_allowed(self, member_id: int, side: str) -> tuple[bool, str | None]:
        """
        Enforce strict alternation forever:
          pick 1 -> Side A
          pick 2 -> Side B
          pick 3 -> Side A
          pick 4 -> Side B
          ...

        IMPORTANT: If either side globally runs out of items, selections open up
        and any remaining items can be picked (no alternation restrictions).
        """
        if not self.item_sides:
            return True, None

        if not self.side_order or len(self.side_order) != 2:
            return True, None

        remaining_by_side = self._remaining_items_by_side()
        if not remaining_by_side:
            return True, None

        side_a, side_b = self.side_order[0], self.side_order[1]
        a_remaining = remaining_by_side.get(side_a, 0)
        b_remaining = remaining_by_side.get(side_b, 0)

        # If either side is exhausted, open selection to all remaining items.
        if a_remaining <= 0 or b_remaining <= 0:
            return True, None

        # Enforce alternation
        picks_so_far = self.picks_by_team.get(member_id, [])
        pick_number = len(picks_so_far) + 1  # 1-based
        expected_side = side_a if (pick_number % 2 == 1) else side_b

        if side != expected_side:
            examples = self._sample_remaining_items_for_side(expected_side, limit=10)
            examples_text = ", ".join(examples) if examples else "None"
            msg = (
                f"❌ Your next pick must be from **{expected_side}**. "
                f"{expected_side} remaining: **{remaining_by_side.get(expected_side, 0)}**.\n"
                f"Examples: {examples_text}"
            )
            return False, msg

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

        # Save state BEFORE applying pick (for undo)
        self.pick_history.append(
            {
                "member_id": member.id,
                "item_name": item_name,
                "prev_round": self.current_round,
                "prev_index": self.current_index,
                "prev_direction": self.direction,
                "prev_completed": self.completed,
            }
        )

        # Record pick
        self.picks_by_team.setdefault(member.id, []).append(item_name)
        self.picked_items.add(item_name)

        # Track side usage (keeps wrapup side stats working)
        side = self.item_sides.get(item_name)
        if side:
            if member.id not in self.side_picks:
                self.side_picks[member.id] = {}
            self.side_picks[member.id][side] = self.side_picks[member.id].get(side, 0) + 1

        # Stop any active timer
        self.cancel_timer()

        # Advance turn
        self.advance_turn()

        # Check completion (also complete if pool exhausted)
        remaining = [i for i in self.available_items if i not in self.picked_items]
        if self.total_picks_made() >= self.max_picks_total() or len(remaining) == 0:
            self.completed = True

        return True, None

    def undo_last_pick(self) -> tuple[bool, str]:
        """Undo exactly the most recent pick (one at a time)."""
        if not self.pick_history:
            return False, "There is no pick to undo."

        last = self.pick_history.pop()

        member_id = last["member_id"]
        item_name = last["item_name"]

        # Remove the pick from picks_by_team
        picks = self.picks_by_team.get(member_id, [])
        if not picks or picks[-1] != item_name:
            # Fallback: remove the last occurrence if somehow out-of-sync
            if item_name in picks:
                picks.remove(item_name)
            else:
                # Can't safely undo
                return False, "Undo failed: could not find the pick in history on that team."
        else:
            picks.pop()

        # Unmark picked item
        if item_name in self.picked_items:
            self.picked_items.remove(item_name)

        # Fix side counts
        side = self.item_sides.get(item_name)
        if side and member_id in self.side_picks:
            if side in self.side_picks[member_id]:
                self.side_picks[member_id][side] -= 1
                if self.side_picks[member_id][side] <= 0:
                    del self.side_picks[member_id][side]
            if not self.side_picks[member_id]:
                del self.side_picks[member_id]

        # Restore state
        self.current_round = last["prev_round"]
        self.current_index = last["prev_index"]
        self.direction = last["prev_direction"]
        self.completed = False  # undo re-opens draft even if it was complete
        self.wrapup_sent = False

        # Cancel timer (since turn changed)
        self.cancel_timer()

        return True, f"Undid last pick: <@{member_id}> → **{item_name}**"

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


def build_pool_embed_page(
    draft: DraftState,
    page_index: int,
    rows_per_page: int = 20,
) -> discord.Embed:
    """
    Build a single page of the pool embed.

    - If exactly 2 sides exist, show a 2-column table (rows_per_page rows).
    - Otherwise, show a simple list (rows_per_page items).
    """
    remaining = [i for i in draft.available_items if i not in draft.picked_items]

    embed = discord.Embed(
        title="Remaining Draft Pool",
        description=f"{len(remaining)} items left",
    )

    if not remaining:
        embed.description = "No remaining items in the pool."
        embed.set_footer(text="Page 1/1")
        return embed

    # ---- Two-side table view ----
    if draft.item_sides:
        grouped: dict[str, list[str]] = {}
        for item in remaining:
            side = draft.item_sides.get(item, "Unspecified")
            grouped.setdefault(side, []).append(item)

        sides = sorted(grouped.keys())
        if len(sides) == 2:
            left_side, right_side = sides[0], sides[1]
            left_items = grouped[left_side]
            right_items = grouped[right_side]

            # Paginate by ROWS (not total items)
            max_rows = max(len(left_items), len(right_items))
            total_pages = max(1, (max_rows + rows_per_page - 1) // rows_per_page)

            page_index = max(0, min(page_index, total_pages - 1))
            start = page_index * rows_per_page
            end = start + rows_per_page

            header = f"{left_side:<16} | {right_side:<16}"
            divider = "-" * len(header)
            rows = [header, divider]

            for i in range(start, min(end, max_rows)):
                left_name = left_items[i] if i < len(left_items) else ""
                right_name = right_items[i] if i < len(right_items) else ""
                rows.append(f"{left_name:<16} | {right_name:<16}")

            table_text = "```text\n" + "\n".join(rows) + "\n```"

            embed.description = (
                f"{len(remaining)} items left\n"
                f"{left_side}: {len(left_items)}, {right_side}: {len(right_items)}"
            )
            embed.add_field(name="Available by side", value=table_text, inline=False)
            embed.set_footer(text=f"Page {page_index + 1}/{total_pages} • {rows_per_page} rows/page")
            return embed

    # ---- Fallback list view ----
    total_pages = max(1, (len(remaining) + rows_per_page - 1) // rows_per_page)
    page_index = max(0, min(page_index, total_pages - 1))

    start = page_index * rows_per_page
    end = start + rows_per_page
    slice_items = remaining[start:end]

    lines = [f"- {name}" for name in slice_items]
    embed.add_field(name="Available", value="\n".join(lines), inline=False)
    embed.set_footer(text=f"Page {page_index + 1}/{total_pages} • {rows_per_page} items/page")
    return embed


def get_side_hint(draft: DraftState) -> str:
    """Return a hint about which side the next player must pick from (if enforced)."""
    member = draft.current_team()
    if member is None or not draft.item_sides:
        return ""

    if not draft.side_order or len(draft.side_order) != 2:
        return ""

    remaining_by_side = draft._remaining_items_by_side()
    if not remaining_by_side:
        return ""

    side_a, side_b = draft.side_order
    a_remaining = remaining_by_side.get(side_a, 0)
    b_remaining = remaining_by_side.get(side_b, 0)

    # If either side exhausted, open selection
    if a_remaining <= 0 or b_remaining <= 0:
        return "One side is exhausted — you may pick from any remaining items."

    picks_so_far = draft.picks_by_team.get(member.id, [])
    pick_number = len(picks_so_far) + 1
    expected_side = side_a if (pick_number % 2 == 1) else side_b

    return f"Your next pick must be from **{expected_side}**."


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

    if not draft.completed and random.random() > 0.4:
        return

    side = draft.item_sides.get(item_name)
    generic_lines = [
        f"🔥 Bold pick, {picker.display_name}. Let's see if it pays off.",
        f"💀 That’s either genius or madness, {picker.display_name}. No in-between.",
        f"🎲 Interesting choice, {picker.display_name}. I would've panic-picked by now.",
    ]

    side_lines = (
        [
            f"⚔️ Another one for **{side}**. The other side looks nervous.",
            f"🛡️ **{side}** just gained a new victim—uh, teammate.",
        ]
        if side
        else []
    )

    finale_lines = [
        "🎉 Draft complete! Time to pretend every pick was part of the plan.",
        "🏁 That’s the draft! May your choices age like fine wine and not milk.",
    ]

    if draft.completed:
        await channel.send(random.choice(finale_lines))
        return

    candidates = generic_lines + side_lines
    if candidates:
        await channel.send(random.choice(candidates))


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

    return "\n".join([header] + rows)


def write_draft_log_file(draft: DraftState, guild_id: int) -> str:
    """Write the draft log to a .txt file and return the filename."""
    os.makedirs("logs", exist_ok=True)
    filename = os.path.join("logs", f"draft_log_{guild_id}.txt")

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
    """Send a final wrapup summary and automatically export the draft log. Only runs once per draft."""
    if draft.wrapup_sent:
        return
    draft.wrapup_sent = True

    await channel.send("🎉 Draft is complete!")

    table = build_teams_table(draft)
    if table:
        await channel.send(f"📋 **Final Draft Board:**\n```{table}```")

    if draft.item_sides:
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
            overall_text = ", ".join(f"{s}: {c}" for s, c in side_totals.items())
            await channel.send(f"⚖️ **Side Totals:** {overall_text}")

            teams = draft.draft_order if draft.draft_order else draft.teams
            lines = []
            for member in teams:
                counts = per_team_side_totals.get(member.id, {})
                if counts:
                    lines.append(
                        f"{member.display_name} → {', '.join(f'{s}: {c}' for s, c in counts.items())}"
                    )
            if lines:
                await channel.send("📊 **Per-Team Side Breakdown:**\n" + "\n".join(lines))

    filename = write_draft_log_file(draft, guild_id)
    await channel.send("📄 Final draft log exported automatically:", file=discord.File(filename))


# ===============================
# POOL PAGINATION (OPTION A)
# - Only the captain who ran !pool can click buttons
# - Same channel as command
# - 20 rows per page
# - Buttons disabled ONLY after that same captain makes their next pick
# ===============================

# Active pool paginator per (guild_id, user_id)
active_pool_view: dict[tuple[int, int], "PoolPaginator"] = {}


async def expire_pool_view_for_user(guild_id: int, user_id: int):
    """Disable the active !pool paginator buttons for this specific user in this guild."""
    key = (guild_id, user_id)
    view = active_pool_view.get(key)
    if view is None:
        return

    for child in view.children:
        if isinstance(child, discord.ui.Button):
            child.disabled = True

    if view.message is not None:
        try:
            embed = view.message.embeds[0] if view.message.embeds else view.current_embed()
            embed.set_footer(text="Expired after your pick.")
            await view.message.edit(embed=embed, view=view)
        except discord.HTTPException:
            pass

    active_pool_view.pop(key, None)


class PoolPaginator(discord.ui.View):
    def __init__(self, draft: DraftState, author_id: int, rows_per_page: int = 20):
        super().__init__(timeout=None)  # expires on next pick by that author
        self.draft = draft
        self.author_id = author_id
        self.rows_per_page = rows_per_page
        self.page_index = 0
        self.message: discord.Message | None = None

    def current_embed(self) -> discord.Embed:
        return build_pool_embed_page(self.draft, self.page_index, self.rows_per_page)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "❌ Only the captain who ran `!pool` can use these buttons.",
                ephemeral=True,
            )
            return False
        return True

    def _max_pages(self) -> int:
        remaining = [i for i in self.draft.available_items if i not in self.draft.picked_items]
        if not remaining:
            return 1

        if self.draft.item_sides:
            grouped: dict[str, list[str]] = {}
            for item in remaining:
                side = self.draft.item_sides.get(item, "Unspecified")
                grouped.setdefault(side, []).append(item)

            sides = sorted(grouped.keys())
            if len(sides) == 2:
                max_rows = max(len(grouped[sides[0]]), len(grouped[sides[1]]))
                return max(1, (max_rows + self.rows_per_page - 1) // self.rows_per_page)

        return max(1, (len(remaining) + self.rows_per_page - 1) // self.rows_per_page)

    def _clamp_page(self):
        pages = self._max_pages()
        if self.page_index < 0:
            self.page_index = 0
        if self.page_index > pages - 1:
            self.page_index = pages - 1

    async def _update(self, interaction: discord.Interaction):
        self._clamp_page()
        await interaction.response.edit_message(embed=self.current_embed(), view=self)

    @discord.ui.button(label="◀ Prev", style=discord.ButtonStyle.secondary)
    async def prev_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page_index -= 1
        await self._update(interaction)

    @discord.ui.button(label="Refresh 🔄", style=discord.ButtonStyle.primary)
    async def refresh_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self._clamp_page()
        await self._update(interaction)

    @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.secondary)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page_index += 1
        await self._update(interaction)

    @discord.ui.button(label="Close ✖", style=discord.ButtonStyle.danger)
    async def close_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True

        # Clear registry if this was the active view
        if interaction.guild is not None:
            active_pool_view.pop((interaction.guild.id, self.author_id), None)

        embed = self.current_embed()
        embed.set_footer(text="Closed.")
        await interaction.response.edit_message(embed=embed, view=self)


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

    if draft is None or not draft.started or draft.completed:
        return

    # Channel lock: only allow picks in the channel where !begin occurred
    if draft.draft_channel_id is not None and message.channel.id != draft.draft_channel_id:
        return

    member = message.author

    if draft.current_team() != member:
        return

    if draft.paused:
        await message.channel.send("❌ The draft is currently paused. Please wait for `!resume`.")
        return

    text = message.content.strip()
    if not text:
        return

    # Ignore bot commands (starting with the command prefix)
    if text.startswith(str(bot.command_prefix)):
        return

    lowered = text.lower()

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
        # Partial matching guardrail
        if len(lowered) < MIN_PARTIAL_MATCH_LEN:
            return  # ignore short partials to reduce accidental picks

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
            return  # no match: silently ignore so people can chat

    ok, error = draft.make_pick(member, matched_item)
    if not ok:
        await message.channel.send(str(error))
        return

    # Expire ONLY the pool message created by the picker
    await expire_pool_view_for_user(guild_id, member.id)

    await message.channel.send(f"✅ {member.mention} drafted **{matched_item}**!")

    # Show same style as !pool (page 1), 20 rows
    await message.channel.send(embed=build_pool_embed_page(draft, page_index=0, rows_per_page=20))
    await message.channel.send("Use `!pool` to browse all pages.")

    await maybe_send_banter_after_pick(message.channel, draft, member, matched_item)

    if draft.completed:
        await send_draft_wrapup(message.channel, draft, guild_id)
    else:
        await message.channel.send(build_next_turn_message(draft))


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    if message.guild is not None:
        await try_auto_pick(message)
        await bot.process_commands(message)
        return

    await bot.process_commands(message)


# ===============================
# UTILITY COMMANDS
# ===============================


@bot.command()
async def ping(ctx: commands.Context):
    await ctx.send("Pong!")


# ===============================
# DRAFT COMMANDS
# ===============================


@bot.command(name="startdraft")
async def start_draft(ctx: commands.Context, rounds: int):
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
        "Others can join with `!join`.\n"
        "The owner can set the pool with `!setpool` or `!setpooldm`, and can randomize order with `!fliporder` or `!coinflip`.\n"
        "Once the draft starts, players just type the name (or part of the name) of the item to pick."
    )


@bot.command(name="testmode")
async def enable_test_mode(ctx: commands.Context):
    if ctx.guild is None:
        await ctx.send("❌ This command can only be used in a server.")
        return

    draft = guild_drafts.get(ctx.guild.id)
    if draft is None:
        await ctx.send("❌ No active draft. Start one with `!startdraft` first.")
        return

    if ctx.author.id != draft.owner_id:
        await ctx.send("❌ Only the draft owner can enable test mode.")
        return

    draft.test_mode = True
    await ctx.send("🧪 **Test mode enabled.** You can now begin the draft with only one team for testing.")


@bot.command(name="join")
async def join_draft(ctx: commands.Context):
    if ctx.guild is None:
        await ctx.send("❌ This command can only be used in a server.")
        return

    draft = guild_drafts.get(ctx.guild.id)
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
    if ctx.guild is None:
        await ctx.send("❌ This command can only be used in a server.")
        return

    draft = guild_drafts.get(ctx.guild.id)
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
        await ctx.send(f"✅ Draft pool set with **{len(items)} items** across sides.\nBreakdown — {side_counts}")
    else:
        await ctx.send(
            f"✅ Draft pool set with **{len(items)} items**.\n"
            "Players will be able to draft by simply typing the item name (case-insensitive, partials allowed)."
        )


@bot.command(name="setpooldm")
async def set_pool_dm(ctx: commands.Context):
    if ctx.guild is None:
        await ctx.send("❌ This command can only be used in a server.")
        return

    draft = guild_drafts.get(ctx.guild.id)
    if draft is None:
        await ctx.send("❌ No active draft. Start one with `!startdraft`.")
        return

    if ctx.author.id != draft.owner_id:
        await ctx.send("❌ Only the draft owner can set the draft pool.")
        return

    user = ctx.author

    try:
        await user.send(
            "👋 Let's set up your draft pool.\n\n"
            "Please send me a message containing **all items** you want in the pool.\n"
            "You can separate them by **commas** or put **one per line**.\n\n"
            "Example:\n"
            "`Item A, Item B, Item C`\n"
            "or\n"
            "`Item A\nItem B\nItem C`"
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
        msg = await bot.wait_for("message", check=check, timeout=300)
    except asyncio.TimeoutError:
        await user.send("⏳ Setup timed out. Run `!setpooldm` again in the server when you're ready.")
        return

    raw_text = msg.content.replace("\r\n", "\n").replace("\r", "\n")
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
    await ctx.send(f"✅ {user.mention} has set the draft pool via DM with **{len(items)}** items.")


@bot.command(name="fliporder")
async def flip_order(ctx: commands.Context):
    if ctx.guild is None:
        await ctx.send("❌ This command can only be used in a server.")
        return

    draft = guild_drafts.get(ctx.guild.id)
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

    order_text = "\n".join(f"{i+1}. {m.display_name}" for i, m in enumerate(draft.draft_order))
    await ctx.send("🪙 **Randomized draft order:**\n" + order_text + "\n\nUse `!begin` to start the draft.")


@bot.command(name="coinflip")
async def coinflip(ctx: commands.Context):
    if ctx.guild is None:
        await ctx.send("❌ This command can only be used in a server.")
        return

    draft = guild_drafts.get(ctx.guild.id)
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
        await ctx.send("❌ Coinflip requires exactly **2** teams joined via `!join`.")
        return

    winner = random.choice(draft.teams)
    loser = draft.teams[0] if draft.teams[1] == winner else draft.teams[1]
    draft.draft_order = [winner, loser]

    await ctx.send(
        "🪙 **Coinflip result!**\n"
        f"Winner: {winner.mention} — first pick.\n"
        f"Order:\n1️⃣ {winner.display_name}\n2️⃣ {loser.display_name}\n\n"
        "Use `!begin` to start the draft."
    )


@bot.command(name="begin")
async def begin_draft(ctx: commands.Context):
    if ctx.guild is None:
        await ctx.send("❌ This command can only be used in a server.")
        return

    draft = guild_drafts.get(ctx.guild.id)
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

    draft.begin(channel_id=ctx.channel.id)

    order_names = ", ".join(m.display_name for m in draft.draft_order)
    await ctx.send(
        "🚨 **Draft has begun!**\n"
        f"**Order:** {order_names}\n"
        f"{build_next_turn_message(draft)}\n"
        "To pick, just type the item name (case-insensitive, partials allowed).\n"
        f"✅ Picks are locked to this channel: {ctx.channel.mention}"
    )

    await ctx.send(embed=build_pool_embed_page(draft, page_index=0, rows_per_page=20))
    await ctx.send("Use `!pool` to browse all pages.")


@bot.command(name="pick")
async def make_pick_command(ctx: commands.Context, *, item_name: str):
    if ctx.guild is None:
        await ctx.send("❌ This command can only be used in a server.")
        return

    guild_id = ctx.guild.id
    draft = guild_drafts.get(guild_id)
    if draft is None:
        await ctx.send("❌ No active draft.")
        return

    # Channel lock
    if draft.draft_channel_id is not None and ctx.channel.id != draft.draft_channel_id:
        await ctx.send(f"❌ Picks are locked to <#{draft.draft_channel_id}>.")
        return

    ok, error = draft.make_pick(ctx.author, item_name)
    if not ok:
        await ctx.send(str(error))
        return

    # Expire ONLY the pool message created by the picker
    await expire_pool_view_for_user(guild_id, ctx.author.id)

    await ctx.send(f"✅ {ctx.author.mention} drafted **{item_name}**!")

    await ctx.send(embed=build_pool_embed_page(draft, page_index=0, rows_per_page=20))
    await ctx.send("Use `!pool` to browse all pages.")

    await maybe_send_banter_after_pick(ctx.channel, draft, ctx.author, item_name)

    if draft.completed:
        await send_draft_wrapup(ctx.channel, draft, guild_id)
    else:
        await ctx.send(build_next_turn_message(draft))


@bot.command(name="undo")
async def undo_last(ctx: commands.Context):
    """Undo the most recent pick (owner/admin only), one at a time."""
    if ctx.guild is None:
        await ctx.send("❌ This command can only be used in a server.")
        return

    guild_id = ctx.guild.id
    draft = guild_drafts.get(guild_id)
    if draft is None:
        await ctx.send("❌ No active draft.")
        return

    # Owner OR admin only
    if ctx.author.id != draft.owner_id and not ctx.author.guild_permissions.administrator:
        await ctx.send("❌ Only the draft owner or a server admin can use `!undo`.")
        return

    ok, msg = draft.undo_last_pick()
    if not ok:
        await ctx.send(f"❌ {msg}")
        return

    await ctx.send(f"↩️ {msg}")

    # Show pool + next turn (recommended)
    await ctx.send(embed=build_pool_embed_page(draft, page_index=0, rows_per_page=20))
    await ctx.send("Use `!pool` to browse all pages.")
    await ctx.send(build_next_turn_message(draft))


@bot.command(name="pool")
async def show_pool(ctx: commands.Context):
    """
    Show remaining items in the draft pool (paginated).
    - 20 rows per page
    - Buttons usable ONLY by the captain who ran !pool
    - Buttons disabled ONLY after that same captain makes their next pick
    """
    if ctx.guild is None:
        await ctx.send("❌ This command can only be used in a server.")
        return

    guild_id = ctx.guild.id
    draft = guild_drafts.get(guild_id)

    if draft is None:
        await ctx.send("❌ No active draft.")
        return

    # Replace any existing pool view for this same user
    await expire_pool_view_for_user(guild_id, ctx.author.id)

    view = PoolPaginator(draft=draft, author_id=ctx.author.id, rows_per_page=20)
    msg = await ctx.send(embed=view.current_embed(), view=view)
    view.message = msg

    active_pool_view[(guild_id, ctx.author.id)] = view


@bot.command(name="status")
async def draft_status(ctx: commands.Context):
    if ctx.guild is None:
        await ctx.send("❌ This command can only be used in a server.")
        return

    draft = guild_drafts.get(ctx.guild.id)
    if draft is None:
        await ctx.send("❌ No active draft.")
        return

    if not draft.started:
        await ctx.send(
            "Draft not started yet.\nTeams joined: "
            f"{', '.join(m.display_name for m in draft.teams) or 'none'}"
        )
        return

    lines = [
        "📊 **Draft Status**",
        f"Round: {draft.current_round}/{draft.rounds}",
        f"Total picks made: {draft.total_picks_made()}/{draft.max_picks_total()}",
    ]

    if draft.paused:
        lines.append("State: ⏸️ Paused")
    elif draft.completed:
        lines.append("State: ✅ Complete")
    else:
        current = draft.current_team()
        lines.append(f"Current turn: {current.mention if current else 'None'}")

    await ctx.send("\n".join(lines))


@bot.command(name="mypicks")
async def my_picks(ctx: commands.Context):
    if ctx.guild is None:
        await ctx.send("❌ This command can only be used in a server.")
        return

    draft = guild_drafts.get(ctx.guild.id)
    if draft is None:
        await ctx.send("❌ No active draft.")
        return

    picks = draft.picks_by_team.get(ctx.author.id, [])
    if not picks:
        await ctx.send("ℹ️ You have no picks yet.")
        return

    text = "\n".join(f"{i+1}. {p}" for i, p in enumerate(picks))
    await ctx.send(f"📜 **Your picks:**\n{text}")


@bot.command(name="order")
async def show_order(ctx: commands.Context):
    if ctx.guild is None:
        await ctx.send("❌ This command can only be used in a server.")
        return

    draft = guild_drafts.get(ctx.guild.id)
    if draft is None:
        await ctx.send("❌ No active draft.")
        return

    if not draft.draft_order:
        await ctx.send("ℹ️ Draft order not set yet. Use `!fliporder`, `!coinflip`, or `!begin`.")
        return

    order = "\n".join(f"{i+1}. {m.display_name}" for i, m in enumerate(draft.draft_order))
    await ctx.send(f"📋 **Draft Order:**\n{order}")


@bot.command(name="teams")
async def show_teams(ctx: commands.Context):
    if ctx.guild is None:
        await ctx.send("❌ This command can only be used in a server.")
        return

    draft = guild_drafts.get(ctx.guild.id)
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
    if ctx.guild is None:
        await ctx.send("❌ This command can only be used in a server.")
        return

    draft = guild_drafts.get(ctx.guild.id)
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
    draft.turn_timer_task = asyncio.create_task(run_turn_timer(ctx, draft, seconds))
    await ctx.send(f"⏳ Timer started for {team.mention}: {format_duration(seconds)}.")


@bot.command(name="pause")
async def pause_draft(ctx: commands.Context):
    if ctx.guild is None:
        await ctx.send("❌ This command can only be used in a server.")
        return

    draft = guild_drafts.get(ctx.guild.id)
    if draft is None:
        await ctx.send("❌ No active draft to pause.")
        return
    if draft.completed:
        await ctx.send("❌ The draft is already complete.")
        return
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
    if ctx.guild is None:
        await ctx.send("❌ This command can only be used in a server.")
        return

    draft = guild_drafts.get(ctx.guild.id)
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


@bot.command(name="banter")
async def toggle_banter(ctx: commands.Context, mode: str):
    if ctx.guild is None:
        await ctx.send("❌ This command can only be used in a server.")
        return

    draft = guild_drafts.get(ctx.guild.id)
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


@bot.command(name="forcestop")
async def force_stop(ctx: commands.Context):
    if ctx.guild is None:
        await ctx.send("❌ This command can only be used in a server.")
        return

    draft = guild_drafts.get(ctx.guild.id)
    if draft is None:
        await ctx.send("❌ There is no active draft to stop.")
        return

    if ctx.author.id != draft.owner_id and not ctx.author.guild_permissions.administrator:
        await ctx.send("❌ Only the draft owner or a server admin can force stop the draft.")
        return

    del guild_drafts[ctx.guild.id]
    await ctx.send("⛔ **The draft has been forcefully stopped.** All draft data has been cleared.")


@bot.command(name="exportdraft")
async def export_draft(ctx: commands.Context):
    if ctx.guild is None:
        await ctx.send("❌ This command can only be used in a server.")
        return

    draft = guild_drafts.get(ctx.guild.id)
    if draft is None:
        await ctx.send("❌ No active draft or draft data to export.")
        return

    filename = write_draft_log_file(draft, ctx.guild.id)
    await ctx.send("📄 Draft log exported:", file=discord.File(filename))


# ===============================
# RUN THE BOT
# ===============================


def load_token() -> str:
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
