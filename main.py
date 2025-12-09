import os
from datetime import datetime, timedelta
from typing import Optional, Tuple

import discord
from discord.ext import commands
from discord import app_commands
import aiosqlite

from flask import Flask
from threading import Thread

# ===================== Replit用 HTTP keep-alive =====================

app = Flask(__name__)

@app.route("/")
def home():
    return "ok"  # 監視ツール用

def run_web():
    app.run(host="0.0.0.0", port=8080)

def keep_alive():
    t = Thread(target=run_web)
    t.daemon = True
    t.start()


# ===================== Bot 設定 =====================

COMMAND_PREFIX = "!"
FACTION_CREATE_COST = 1000  # 派閥作成コスト
DB_PATH = "bot.db"

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.guilds = True

last_message_times: dict[int, datetime] = {}  # 通貨用クールダウン


# ===================== 共通: フォーラム作成ヘルパー =====================

async def create_forum_channel(
    guild: discord.Guild,
    name: str,
    category: Optional[discord.CategoryChannel] = None,
    overwrites=None,
    topic: Optional[str] = None,
):
    """discord.py のバージョン差を吸収してフォーラムチャンネルを作る"""
    kwargs = {}
    if category is not None:
        kwargs["category"] = category
    if overwrites is not None:
        kwargs["overwrites"] = overwrites
    if topic is not None:
        kwargs["topic"] = topic

    # 新しい方
    if hasattr(guild, "create_forum"):
        return await guild.create_forum(name=name, **kwargs)
    # 古い方
    if hasattr(guild, "create_forum_channel"):
        return await guild.create_forum_channel(name, **kwargs)
    # フォーラム無い場合はテキストで代用
    return await guild.create_text_channel(
        name=name,
        category=category,
        overwrites=overwrites,
        topic=topic,
    )


# ===================== DB 初期化 =====================

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                balance INTEGER NOT NULL DEFAULT 0
            );
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS factions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                leader_id INTEGER NOT NULL,
                base_role_id INTEGER NOT NULL,
                leader_role_id INTEGER NOT NULL,
                officer_role_id INTEGER NOT NULL,
                category_id INTEGER NOT NULL,
                forum_channel_id INTEGER NOT NULL,
                chat_channel_id INTEGER NOT NULL,
                vc_channel_id INTEGER NOT NULL,
                listen_vc_channel_id INTEGER NOT NULL,
                control_panel_channel_id INTEGER NOT NULL,
                destroyed INTEGER NOT NULL DEFAULT 0,
                is_open INTEGER NOT NULL DEFAULT 0
            );
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS faction_members (
                user_id INTEGER NOT NULL,
                faction_id INTEGER NOT NULL,
                role TEXT NOT NULL, -- 'leader' / 'officer' / 'member'
                PRIMARY KEY (user_id, faction_id)
            );
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS wars (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                attacker_faction_id INTEGER NOT NULL,
                defender_faction_id INTEGER NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                attacker_messages INTEGER NOT NULL DEFAULT 0,
                defender_messages INTEGER NOT NULL DEFAULT 0
            );
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS guild_settings (
                guild_id INTEGER PRIMARY KEY,
                war_status_channel_id INTEGER
            );
            """
        )
        # 既存DBに is_open が無い場合だけ追加
        try:
            await db.execute(
                "ALTER TABLE factions ADD COLUMN is_open INTEGER NOT NULL DEFAULT 0;"
            )
        except Exception:
            pass

        await db.commit()


# ===================== 通貨関連 =====================

async def get_balance(user_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,))
        row = await cur.fetchone()
        await cur.close()
        if row:
            return row[0]
        await db.execute(
            "INSERT INTO users (user_id, balance) VALUES (?, ?)",
            (user_id, 0),
        )
        await db.commit()
        return 0


async def add_balance(user_id: int, amount: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,))
        row = await cur.fetchone()
        if row:
            new_balance = row[0] + amount
            await db.execute(
                "UPDATE users SET balance = ? WHERE user_id = ?",
                (new_balance, user_id),
            )
        else:
            new_balance = amount
            await db.execute(
                "INSERT INTO users (user_id, balance) VALUES (?, ?)",
                (user_id, new_balance),
            )
        await db.commit()
        await cur.close()
        return new_balance


async def remove_balance(user_id: int, amount: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,))
        row = await cur.fetchone()
        if not row or row[0] < amount:
            await cur.close()
            return False
        new_balance = row[0] - amount
        await db.execute(
            "UPDATE users SET balance = ? WHERE user_id = ?",
            (new_balance, user_id),
        )
        await db.commit()
        await cur.close()
        return True


# ===================== 派閥関連 =====================

async def get_user_faction_id(user_id: int, guild_id: int) -> Optional[int]:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """
            SELECT f.id
            FROM faction_members fm
            JOIN factions f ON fm.faction_id = f.id
            WHERE fm.user_id = ? AND f.guild_id = ? AND f.destroyed = 0
            """,
            (user_id, guild_id),
        )
        row = await cur.fetchone()
        await cur.close()
        return row[0] if row else None


async def get_faction_by_id(faction_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """
            SELECT id, guild_id, name, leader_id, base_role_id, leader_role_id,
                   officer_role_id, category_id, forum_channel_id, chat_channel_id,
                   vc_channel_id, listen_vc_channel_id, control_panel_channel_id,
                   destroyed, is_open
            FROM factions
            WHERE id = ?
            """,
            (faction_id,),
        )
        row = await cur.fetchone()
        await cur.close()
        return row


async def get_faction_by_name(name: str, guild_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """
            SELECT id, name, leader_id, base_role_id, leader_role_id,
                   officer_role_id, category_id, forum_channel_id, chat_channel_id,
                   vc_channel_id, listen_vc_channel_id, control_panel_channel_id,
                   destroyed, is_open
            FROM factions
            WHERE guild_id = ? AND name = ? AND destroyed = 0
            """,
            (guild_id, name),
        )
        row = await cur.fetchone()
        await cur.close()
        return row


async def add_faction_member(user_id: int, faction_id: int, role: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT OR REPLACE INTO faction_members (user_id, faction_id, role)
            VALUES (?, ?, ?)
            """,
            (user_id, faction_id, role),
        )
        await db.commit()


async def remove_faction_member(user_id: int, faction_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM faction_members WHERE user_id = ? AND faction_id = ?",
            (user_id, faction_id),
        )
        await db.commit()


async def get_faction_role(user_id: int, guild_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """
            SELECT fm.faction_id, fm.role
            FROM faction_members fm
            JOIN factions f ON fm.faction_id = f.id
            WHERE fm.user_id = ? AND f.guild_id = ? AND f.destroyed = 0
            """,
            (user_id, guild_id),
        )
        row = await cur.fetchone()
        await cur.close()
        if row:
            return row[0], row[1]
        return None, None


# ===================== 戦争関連 =====================

async def get_active_war(guild_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """
            SELECT id, attacker_faction_id, defender_faction_id,
                   attacker_messages, defender_messages
            FROM wars
            WHERE guild_id = ? AND active = 1
            """,
            (guild_id,),
        )
        row = await cur.fetchone()
        await cur.close()
        return row


async def add_message_for_war(user_id: int, guild_id: int):
    faction_id = await get_user_faction_id(user_id, guild_id)
    if not faction_id:
        return

    war = await get_active_war(guild_id)
    if not war:
        return

    war_id, attacker_id, defender_id, attacker_msgs, defender_msgs = war
    if faction_id not in (attacker_id, defender_id):
        return

    async with aiosqlite.connect(DB_PATH) as db:
        if faction_id == attacker_id:
            attacker_msgs += 1
            await db.execute(
                "UPDATE wars SET attacker_messages = ? WHERE id = ?",
                (attacker_msgs, war_id),
            )
        else:
            defender_msgs += 1
            await db.execute(
                "UPDATE wars SET defender_messages = ? WHERE id = ?",
                (defender_msgs, war_id),
            )
        await db.commit()


# ===================== ギルド設定（戦争状況チャンネル） =====================

async def get_guild_war_status_channel_id(guild_id: int) -> Optional[int]:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT war_status_channel_id FROM guild_settings WHERE guild_id = ?",
            (guild_id,),
        )
        row = await cur.fetchone()
        await cur.close()
        return row[0] if row else None


async def set_guild_war_status_channel_id(guild_id: int, channel_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT OR REPLACE INTO guild_settings (guild_id, war_status_channel_id)
            VALUES (?, ?)
            """,
            (guild_id, channel_id),
        )
        await db.commit()


async def get_war_status_channel(guild: discord.Guild) -> Optional[discord.TextChannel]:
    channel_id = await get_guild_war_status_channel_id(guild.id)
    if channel_id:
        ch = guild.get_channel(channel_id)
        if isinstance(ch, discord.TextChannel):
            return ch
    return None


# ===================== 派閥解体 & 解散ヘルパー =====================

async def destroy_faction(guild: discord.Guild, faction_row):
    (
        faction_id,
        guild_id,
        name,
        leader_id,
        base_role_id,
        leader_role_id,
        officer_role_id,
        category_id,
        forum_channel_id,
        chat_channel_id,
        vc_channel_id,
        listen_vc_channel_id,
        control_panel_channel_id,
        destroyed,
        is_open,
    ) = faction_row

    # チャンネル削除
    for cid in [
        forum_channel_id,
        chat_channel_id,
        vc_channel_id,
        listen_vc_channel_id,
        control_panel_channel_id,
    ]:
        ch = guild.get_channel(cid)
        if ch:
            try:
                await ch.delete(reason="Faction destroyed")
            except discord.HTTPException:
                pass

    # カテゴリ削除
    category = guild.get_channel(category_id)
    if category:
        try:
            await category.delete(reason="Faction destroyed")
        except discord.HTTPException:
            pass

    # ロール削除
    for rid in [base_role_id, leader_role_id, officer_role_id]:
        role = guild.get_role(rid)
        if role:
            try:
                await role.delete(reason="Faction destroyed")
            except discord.HTTPException:
                pass

    # DB 更新
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE factions SET destroyed = 1 WHERE id = ?",
            (faction_id,),
        )
        await db.execute(
            "DELETE FROM faction_members WHERE faction_id = ?",
            (faction_id,),
        )
        await db.commit()


async def attempt_disband_faction(
    guild: discord.Guild,
    user: discord.Member,
    faction_id_override: Optional[int] = None,
) -> Tuple[bool, str]:
    """派閥解散の共通処理（コマンド/ボタン両方から呼ぶ）"""
    if faction_id_override is None:
        faction_id, _role = await get_faction_role(user.id, guild.id)
        if not faction_id:
            return False, "あなたはどの派閥にも所属していません。"
    else:
        faction_id = faction_id_override
        user_fid = await get_user_faction_id(user.id, guild.id)
        if user_fid != faction_id and not user.guild_permissions.administrator:
            return False, "この派閥のメンバーではありません。"

    faction = await get_faction_by_id(faction_id)
    if not faction or faction[13] == 1:
        return False, "派閥情報が見つかりません。"

    (
        fid,
        guild_id,
        name,
        leader_id,
        base_role_id,
        leader_role_id,
        officer_role_id,
        category_id,
        forum_id,
        chat_id,
        vc_id,
        listen_id,
        panel_id,
        destroyed,
        is_open,
    ) = faction

    if guild.id != guild_id:
        return False, "この派閥は別サーバーのものです。"

    if user.id != leader_id and not user.guild_permissions.administrator:
        return False, "派閥解散はリーダー（またはサーバー管理者）のみ可能です。"

    await destroy_faction(guild, faction)
    return True, f"派閥 **{name}** を解散しました。"


# ===================== Bot クラス =====================

class FactionBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix=COMMAND_PREFIX, intents=intents)

    async def setup_hook(self):
        await init_db()
        try:
            synced = await self.tree.sync()
            print(f"Synced {len(synced)} command(s).")
        except Exception as e:
            print(f"Failed to sync commands: {e}")


bot = FactionBot()


# ===================== コントロールパネル用 View =====================

class FactionControlView(discord.ui.View):
    """派閥コントロールパネルのボタン"""

    def __init__(self, faction_id: int):
        super().__init__(timeout=None)
        self.faction_id = faction_id

    async def _check_permission(self, interaction: discord.Interaction):
        guild = interaction.guild
        user = interaction.user
        if guild is None or not isinstance(user, discord.Member):
            return False, "サーバー内でのみ使用できます。"

        faction = await get_faction_by_id(self.faction_id)
        if not faction or faction[13] == 1:
            return False, "この派閥情報が見つかりません。"

        if guild.id != faction[1]:
            return False, "この派閥は別サーバーのものです。"

        user_fid, user_role = await get_faction_role(user.id, guild.id)
        if user_fid != self.faction_id and not user.guild_permissions.administrator:
            return False, "この派閥のメンバーではありません。"

        return True, {
            "guild": guild,
            "user": user,
            "role": user_role or "admin",
            "faction": faction,
        }

    @discord.ui.button(
        label="派閥情報",
        style=discord.ButtonStyle.secondary,
        custom_id="faction_panel:info",
    )
    async def info_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        await interaction.response.defer(ephemeral=True)
        ok, data = await self._check_permission(interaction)
        if not ok:
            await interaction.followup.send(str(data), ephemeral=True)
            return

        guild: discord.Guild = data["guild"]
        user: discord.Member = data["user"]
        faction = data["faction"]

        (
            fid,
            guild_id,
            name,
            leader_id,
            base_role_id,
            leader_role_id,
            officer_role_id,
            category_id,
            forum_id,
            chat_id,
            vc_id,
            listen_id,
            panel_id,
            destroyed,
            is_open,
        ) = faction

        leader = guild.get_member(leader_id)
        leader_name = leader.display_name if leader else "不明"

        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute(
                "SELECT COUNT(*) FROM faction_members WHERE faction_id = ?",
                (fid,),
            )
            row = await cur.fetchone()
            await cur.close()
        member_count = row[0] if row else 0

        join_mode = (
            "オープン（誰でも /f_join で参加可能）"
            if is_open
            else "クローズ（招待制）"
        )

        msg = (
            f"**{name}** の情報:\n"
            f"・リーダー: {leader_name}\n"
            f"・メンバー数: {member_count}\n"
            f"・参加モード: {join_mode}"
        )
        await interaction.followup.send(msg, ephemeral=True)

    @discord.ui.button(
        label="参加モード切替",
        style=discord.ButtonStyle.primary,
        custom_id="faction_panel:toggle_open",
    )
    async def toggle_open_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        await interaction.response.defer(ephemeral=True)
        ok, data = await self._check_permission(interaction)
        if not ok:
            await interaction.followup.send(str(data), ephemeral=True)
            return

        guild: discord.Guild = data["guild"]
        user: discord.Member = data["user"]
        role: str = data["role"]
        faction = data["faction"]

        if role not in ("leader", "officer") and not user.guild_permissions.administrator:
            await interaction.followup.send(
                "参加モードの変更はリーダーまたは幹部のみ可能です。",
                ephemeral=True,
            )
            return

        (
            fid,
            guild_id,
            name,
            leader_id,
            base_role_id,
            leader_role_id,
            officer_role_id,
            category_id,
            forum_id,
            chat_id,
            vc_id,
            listen_id,
            panel_id,
            destroyed,
            is_open,
        ) = faction

        new_state = 0 if is_open else 1
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE factions SET is_open = ? WHERE id = ?",
                (new_state, fid),
            )
            await db.commit()

        text = (
            "オープン（誰でも /f_join で参加可能）"
            if new_state
            else "クローズ（招待制）"
        )
        await interaction.followup.send(
            f"派閥 **{name}** の参加モードを **{text}** に変更しました。",
            ephemeral=True,
        )

    @discord.ui.button(
        label="派閥解散",
        style=discord.ButtonStyle.danger,
        custom_id="faction_panel:disband",
    )
    async def disband_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        user = interaction.user
        if guild is None or not isinstance(user, discord.Member):
            await interaction.followup.send("サーバー内でのみ使用できます。", ephemeral=True)
            return

        success, msg = await attempt_disband_faction(guild, user, self.faction_id)
        await interaction.followup.send(msg, ephemeral=True)


# ===================== イベント =====================

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or message.guild is None:
        return

    now = datetime.utcnow()
    last = last_message_times.get(message.author.id)
    if last is None or (now - last) >= timedelta(seconds=10):
        last_message_times[message.author.id] = now
        await add_balance(message.author.id, 1)

    await add_message_for_war(message.author.id, message.guild.id)

    await bot.process_commands(message)


# ===================== 通貨コマンド =====================

@bot.tree.command(name="money", description="自分または指定ユーザーの所持金を表示します")
@app_commands.describe(user="確認したいユーザー（省略時は自分）")
async def money_cmd(
    interaction: discord.Interaction,
    user: Optional[discord.Member] = None,
):
    target = user or interaction.user
    bal = await get_balance(target.id)
    await interaction.response.send_message(
        f"{target.mention} の所持金は `{bal}` コインです。",
        ephemeral=True,
    )


@bot.tree.command(name="give", description="管理者用: 指定ユーザーにコインを付与します")
@app_commands.describe(user="付与するユーザー", amount="付与するコイン数")
async def give_cmd(
    interaction: discord.Interaction,
    user: discord.Member,
    amount: int,
):
    if (
        not isinstance(interaction.user, discord.Member)
        or not interaction.user.guild_permissions.administrator
    ):
        await interaction.response.send_message(
            "このコマンドは管理者のみが実行できます。",
            ephemeral=True,
        )
        return

    new_bal = await add_balance(user.id, amount)
    await interaction.response.send_message(
        f"{user.mention} に `{amount}` コイン付与しました。（合計: {new_bal}）",
        ephemeral=True,
    )


# ===================== 派閥コマンド =====================

@bot.tree.command(name="create_faction", description="新しい派閥を作成します")
@app_commands.describe(name="作成する派閥名")
async def create_faction_cmd(interaction: discord.Interaction, name: str):
    guild = interaction.guild
    user = interaction.user

    if guild is None or not isinstance(user, discord.Member):
        await interaction.response.send_message(
            "サーバー内で実行してください。",
            ephemeral=True,
        )
        return

    existing = await get_user_faction_id(user.id, guild.id)
    if existing:
        await interaction.response.send_message(
            "すでに派閥に所属しています。",
            ephemeral=True,
        )
        return

    if not await remove_balance(user.id, FACTION_CREATE_COST):
        bal = await get_balance(user.id)
        await interaction.response.send_message(
            f"お金が足りません。必要: {FACTION_CREATE_COST} / 所持: {bal}",
            ephemeral=True,
        )
        return

    if await get_faction_by_name(name, guild.id):
        await interaction.response.send_message(
            "同じ名前の派閥が既に存在します。別の名前を使ってください。",
            ephemeral=True,
        )
        return

    # ここから重い処理なので先に defer
    await interaction.response.defer(ephemeral=True)

    # ロール
    faction_role = await guild.create_role(name=f"[派閥] {name}", mentionable=True)
    leader_role = await guild.create_role(name=f"[派閥] {name} リーダー", mentionable=True)
    officer_role = await guild.create_role(name=f"[派閥] {name} 幹部", mentionable=True)

    await user.add_roles(faction_role, leader_role)

    # カテゴリ
    category: discord.CategoryChannel = await guild.create_category(f"派閥: {name}")

    overwrites_common = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        faction_role: discord.PermissionOverwrite(
            view_channel=True, send_messages=True, connect=True, speak=True
        ),
        leader_role: discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            connect=True,
            speak=True,
            manage_channels=True,
            manage_roles=True,
        ),
        officer_role: discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            connect=True,
            speak=True,
            manage_channels=True,
        ),
    }

    # フォーラム
    forum_ch = await create_forum_channel(
        guild,
        "フォーラム",
        category=category,
        overwrites=overwrites_common,
        topic=f"{name} のフォーラム",
    )

    # 雑談
    chat_ch = await guild.create_text_channel(
        "雑談",
        category=category,
        overwrites=overwrites_common,
        topic=f"{name} の雑談チャンネル",
    )

    # VC
    vc_ch = await guild.create_voice_channel(
        "VC",
        category=category,
        overwrites=overwrites_common,
    )

    # 聞き専テキスト
    listen_vc_ch = await guild.create_text_channel(
        "VC聞き専",
        category=category,
        overwrites=overwrites_common,
        topic=f"{name} のVC聞き専テキストチャンネル",
    )

    # コントロールパネル（ボタン用）
    overwrites_panel = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        leader_role: discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            manage_channels=True,
            manage_roles=True,
        ),
        officer_role: discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            manage_channels=True,
        ),
    }
    control_panel_ch = await guild.create_text_channel(
        "派閥コントロールパネル",
        category=category,
        overwrites=overwrites_panel,
        topic=f"{name} の派閥管理用チャンネル",
    )

    # DB 登録
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """
            INSERT INTO factions (
                guild_id, name, leader_id, base_role_id, leader_role_id,
                officer_role_id, category_id, forum_channel_id, chat_channel_id,
                vc_channel_id, listen_vc_channel_id, control_panel_channel_id
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                guild.id,
                name,
                user.id,
                faction_role.id,
                leader_role.id,
                officer_role.id,
                category.id,
                forum_ch.id,
                chat_ch.id,
                vc_ch.id,
                listen_vc_ch.id,
                control_panel_ch.id,
            ),
        )
        faction_id = cur.lastrowid
        await db.commit()

    await add_faction_member(user.id, faction_id, "leader")

    # ボタン付きパネル
    view = FactionControlView(faction_id)
    await control_panel_ch.send(
        "ここから派閥の管理ができます：\n"
        "・派閥情報\n"
        "・参加モード切替（オープン/クローズ）\n"
        "・派閥解散\n",
        view=view,
    )

    await interaction.followup.send(
        f"派閥 **{name}** を作成しました！必要コスト `{FACTION_CREATE_COST}` コインを消費しました。",
        ephemeral=True,
    )


@bot.tree.command(name="f_invite", description="自分の派閥にメンバーを招待します")
@app_commands.describe(member="招待するメンバー")
async def faction_invite_cmd(interaction: discord.Interaction, member: discord.Member):
    guild = interaction.guild
    user = interaction.user

    if guild is None or not isinstance(user, discord.Member):
        await interaction.response.send_message(
            "サーバー内でのみ使用できます。",
            ephemeral=True,
        )
        return

    my_faction_id, my_role = await get_faction_role(user.id, guild.id)
    if not my_faction_id or my_role not in ("leader", "officer"):
        await interaction.response.send_message(
            "このコマンドは派閥のリーダーまたは幹部のみ使用できます。",
            ephemeral=True,
        )
        return

    if await get_user_faction_id(member.id, guild.id):
        await interaction.response.send_message(
            "そのユーザーは既にどこかの派閥に所属しています。",
            ephemeral=True,
        )
        return

    faction = await get_faction_by_id(my_faction_id)
    if not faction or faction[13] == 1:
        await interaction.response.send_message(
            "派閥情報が見つかりません。",
            ephemeral=True,
        )
        return

    (
        fid,
        g_id,
        name,
        leader_id,
        base_role_id,
        leader_role_id,
        officer_role_id,
        category_id,
        forum_id,
        chat_id,
        vc_id,
        listen_id,
        panel_id,
        destroyed,
        is_open,
    ) = faction

    base_role = guild.get_role(base_role_id)
    if not base_role:
        await interaction.response.send_message(
            "派閥ロールが見つかりません。管理者に連絡してください。",
            ephemeral=True,
        )
        return

    await member.add_roles(base_role)
    await add_faction_member(member.id, my_faction_id, "member")
    await interaction.response.send_message(
        f"{member.mention} を派閥 **{name}** に招待しました。",
        ephemeral=True,
    )


@bot.tree.command(name="f_kick", description="派閥からメンバーを追放します")
@app_commands.describe(member="追放するメンバー")
async def faction_kick_cmd(interaction: discord.Interaction, member: discord.Member):
    guild = interaction.guild
    user = interaction.user

    if guild is None or not isinstance(user, discord.Member):
        await interaction.response.send_message(
            "サーバー内でのみ使用できます。",
            ephemeral=True,
        )
        return

    my_faction_id, my_role = await get_faction_role(user.id, guild.id)
    if not my_faction_id or my_role not in ("leader", "officer"):
        await interaction.response.send_message(
            "このコマンドは派閥のリーダーまたは幹部のみ使用できます。",
            ephemeral=True,
        )
        return

    target_faction_id = await get_user_faction_id(member.id, guild.id)
    if target_faction_id != my_faction_id:
        await interaction.response.send_message(
            "そのユーザーはあなたの派閥に所属していません。",
            ephemeral=True,
        )
        return

    faction = await get_faction_by_id(my_faction_id)
    if not faction:
        await interaction.response.send_message(
            "派閥情報が見つかりません。",
            ephemeral=True,
        )
        return

    (
        fid,
        g_id,
        name,
        leader_id,
        base_role_id,
        leader_role_id,
        officer_role_id,
        category_id,
        forum_id,
        chat_id,
        vc_id,
        listen_id,
        panel_id,
        destroyed,
        is_open,
    ) = faction

    if member.id == leader_id:
        await interaction.response.send_message(
            "リーダーは追放できません。",
            ephemeral=True,
        )
        return

    base_role = guild.get_role(base_role_id)
    officer_role_obj = guild.get_role(officer_role_id)
    roles_to_remove = []
    if base_role and base_role in member.roles:
        roles_to_remove.append(base_role)
    if officer_role_obj and officer_role_obj in member.roles:
        roles_to_remove.append(officer_role_obj)

    if roles_to_remove:
        await member.remove_roles(*roles_to_remove)

    await remove_faction_member(member.id, my_faction_id)
    await interaction.response.send_message(
        f"{member.mention} を派閥 **{name}** から追放しました。",
        ephemeral=True,
    )


@bot.tree.command(name="f_promote", description="メンバーを幹部に昇格させます")
@app_commands.describe(member="昇格させるメンバー")
async def faction_promote_cmd(
    interaction: discord.Interaction,
    member: discord.Member,
):
    guild = interaction.guild
    user = interaction.user

    if guild is None or not isinstance(user, discord.Member):
        await interaction.response.send_message(
            "サーバー内でのみ使用できます。",
            ephemeral=True,
        )
        return

    my_faction_id, my_role = await get_faction_role(user.id, guild.id)
    if not my_faction_id or my_role not in ("leader", "officer"):
        await interaction.response.send_message(
            "このコマンドは派閥のリーダーまたは幹部のみ使用できます。",
            ephemeral=True,
        )
        return

    target_faction_id = await get_user_faction_id(member.id, guild.id)
    if target_faction_id != my_faction_id:
        await interaction.response.send_message(
            "そのユーザーはあなたの派閥に所属していません。",
            ephemeral=True,
        )
        return

    faction = await get_faction_by_id(my_faction_id)
    if not faction:
        await interaction.response.send_message(
            "派閥情報が見つかりません。",
            ephemeral=True,
        )
        return

    (
        fid,
        g_id,
        name,
        leader_id,
        base_role_id,
        leader_role_id,
        officer_role_id,
        category_id,
        forum_id,
        chat_id,
        vc_id,
        listen_id,
        panel_id,
        destroyed,
        is_open,
    ) = faction

    officer_role = guild.get_role(officer_role_id)
    base_role = guild.get_role(base_role_id)
    if not officer_role or not base_role:
        await interaction.response.send_message(
            "派閥ロールが見つかりません。",
            ephemeral=True,
        )
        return

    await member.add_roles(base_role, officer_role)
    await add_faction_member(member.id, my_faction_id, "officer")
    await interaction.response.send_message(
        f"{member.mention} を派閥 **{name}** の幹部にしました。",
        ephemeral=True,
    )


@bot.tree.command(name="f_demote", description="幹部をメンバーに降格させます")
@app_commands.describe(member="降格させるメンバー")
async def faction_demote_cmd(
    interaction: discord.Interaction,
    member: discord.Member,
):
    guild = interaction.guild
    user = interaction.user

    if guild is None or not isinstance(user, discord.Member):
        await interaction.response.send_message(
            "サーバー内でのみ使用できます。",
            ephemeral=True,
        )
        return

    my_faction_id, my_role = await get_faction_role(user.id, guild.id)
    if not my_faction_id or my_role not in ("leader", "officer"):
        await interaction.response.send_message(
            "このコマンドは派閥のリーダーまたは幹部のみ使用できます。",
            ephemeral=True,
        )
        return

    target_faction_id = await get_user_faction_id(member.id, guild.id)
    if target_faction_id != my_faction_id:
        await interaction.response.send_message(
            "そのユーザーはあなたの派閥に所属していません。",
            ephemeral=True,
        )
        return

    faction = await get_faction_by_id(my_faction_id)
    if not faction:
        await interaction.response.send_message(
            "派閥情報が見つかりません。",
            ephemeral=True,
        )
        return

    (
        fid,
        g_id,
        name,
        leader_id,
        base_role_id,
        leader_role_id,
        officer_role_id,
        category_id,
        forum_id,
        chat_id,
        vc_id,
        listen_id,
        panel_id,
        destroyed,
        is_open,
    ) = faction

    officer_role_obj = guild.get_role(officer_role_id)
    if officer_role_obj and officer_role_obj in member.roles:
        await member.remove_roles(officer_role_obj)
    await add_faction_member(member.id, my_faction_id, "member")
    await interaction.response.send_message(
        f"{member.mention} を派閥 **{name}** の幹部から降格しました。",
        ephemeral=True,
    )


@bot.tree.command(name="f_info", description="自分の所属している派閥情報を表示します")
async def faction_info_cmd(interaction: discord.Interaction):
    guild = interaction.guild
    user = interaction.user

    if guild is None or not isinstance(user, discord.Member):
        await interaction.response.send_message(
            "サーバー内でのみ使用できます。",
            ephemeral=True,
        )
        return

    faction_id, role = await get_faction_role(user.id, guild.id)
    if not faction_id:
        await interaction.response.send_message(
            "あなたはどの派閥にも所属していません。",
            ephemeral=True,
        )
        return

    faction = await get_faction_by_id(faction_id)
    if not faction:
        await interaction.response.send_message(
            "派閥情報が見つかりません。",
            ephemeral=True,
        )
        return

    (
        fid,
        g_id,
        name,
        leader_id,
        base_role_id,
        leader_role_id,
        officer_role_id,
        category_id,
        forum_id,
        chat_id,
        vc_id,
        listen_id,
        panel_id,
        destroyed,
        is_open,
    ) = faction

    leader = guild.get_member(leader_id)
    leader_name = leader.display_name if leader else "不明"

    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT COUNT(*) FROM faction_members WHERE faction_id = ?",
            (faction_id,),
        )
        row = await cur.fetchone()
        await cur.close()
    member_count = row[0] if row else 0

    join_mode = (
        "オープン（誰でも /f_join で参加可能）" if is_open else "クローズ（招待制）"
    )

    await interaction.response.send_message(
        f"**{name}** の情報:\n"
        f"・リーダー: {leader_name}\n"
        f"・メンバー数: {member_count}\n"
        f"・あなたの役職: {role}\n"
        f"・参加モード: {join_mode}",
        ephemeral=True,
    )


@bot.tree.command(name="f_leave", description="所属している派閥から脱退します")
async def faction_leave_cmd(interaction: discord.Interaction):
    guild = interaction.guild
    user = interaction.user

    if guild is None or not isinstance(user, discord.Member):
        await interaction.response.send_message(
            "サーバー内でのみ使用できます。",
            ephemeral=True,
        )
        return

    faction_id, role = await get_faction_role(user.id, guild.id)
    if not faction_id:
        await interaction.response.send_message(
            "あなたはどの派閥にも所属していません。",
            ephemeral=True,
        )
        return

    faction = await get_faction_by_id(faction_id)
    if not faction:
        await interaction.response.send_message(
            "派閥情報が見つかりません。",
            ephemeral=True,
        )
        return

    (
        fid,
        g_id,
        name,
        leader_id,
        base_role_id,
        leader_role_id,
        officer_role_id,
        category_id,
        forum_id,
        chat_id,
        vc_id,
        listen_id,
        panel_id,
        destroyed,
        is_open,
    ) = faction

    if user.id == leader_id:
        await interaction.response.send_message(
            "リーダーは脱退できません。（解散機能を使ってください）",
            ephemeral=True,
        )
        return

    base_role = guild.get_role(base_role_id)
    officer_role_obj = guild.get_role(officer_role_id)
    roles_to_remove = []
    if base_role and base_role in user.roles:
        roles_to_remove.append(base_role)
    if officer_role_obj and officer_role_obj in user.roles:
        roles_to_remove.append(officer_role_obj)
    if roles_to_remove:
        await user.remove_roles(*roles_to_remove)

    await remove_faction_member(user.id, faction_id)
    await interaction.response.send_message(
        f"派閥 **{name}** から脱退しました。",
        ephemeral=True,
    )


# 派閥解散コマンド
@bot.tree.command(
    name="f_disband",
    description="所属している派閥を解散します（リーダー専用）",
)
async def faction_disband_cmd(interaction: discord.Interaction):
    guild = interaction.guild
    user = interaction.user

    if guild is None or not isinstance(user, discord.Member):
        await interaction.response.send_message(
            "サーバー内でのみ使用できます。",
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=True)
    success, msg = await attempt_disband_faction(guild, user)
    await interaction.followup.send(msg, ephemeral=True)


# ===================== 参加モード切替 & f_join =====================

@bot.tree.command(
    name="f_set_open",
    description="派閥の参加モードをオープン/クローズに切り替えます",
)
@app_commands.choices(
    mode=[
        app_commands.Choice(name="オープン（誰でも参加）", value="open"),
        app_commands.Choice(name="クローズ（招待制）", value="close"),
    ]
)
async def faction_set_open_cmd(
    interaction: discord.Interaction,
    mode: app_commands.Choice[str],
):
    guild = interaction.guild
    user = interaction.user

    if guild is None or not isinstance(user, discord.Member):
        await interaction.response.send_message(
            "サーバー内でのみ使用できます。",
            ephemeral=True,
        )
        return

    faction_id, role = await get_faction_role(user.id, guild.id)
    if not faction_id or role not in ("leader", "officer"):
        await interaction.response.send_message(
            "このコマンドは派閥のリーダーまたは幹部のみ使用できます。",
            ephemeral=True,
        )
        return

    is_open = 1 if mode.value == "open" else 0

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE factions SET is_open = ? WHERE id = ?",
            (is_open, faction_id),
        )
        await db.commit()

    text = (
        "オープン（誰でも /f_join で参加可能）"
        if is_open
        else "クローズ（招待制）"
    )
    await interaction.response.send_message(
        f"あなたの派閥の参加モードを **{text}** に変更しました。",
        ephemeral=True,
    )


@bot.tree.command(name="f_join", description="オープンな派閥に参加します")
@app_commands.describe(faction_name="参加したい派閥名")
async def faction_join_cmd(
    interaction: discord.Interaction,
    faction_name: str,
):
    guild = interaction.guild
    user = interaction.user

    if guild is None or not isinstance(user, discord.Member):
        await interaction.response.send_message(
            "サーバー内でのみ使用できます。",
            ephemeral=True,
        )
        return

    if await get_user_faction_id(user.id, guild.id):
        await interaction.response.send_message(
            "すでにどこかの派閥に所属しています。",
            ephemeral=True,
        )
        return

    faction = await get_faction_by_name(faction_name, guild.id)
    if not faction:
        await interaction.response.send_message(
            "指定された派閥が見つかりません。",
            ephemeral=True,
        )
        return

    (
        faction_id,
        name,
        leader_id,
        base_role_id,
        leader_role_id,
        officer_role_id,
        category_id,
        forum_id,
        chat_id,
        vc_id,
        listen_id,
        panel_id,
        destroyed,
        is_open,
    ) = faction

    if destroyed:
        await interaction.response.send_message(
            "その派閥はすでに解体されています。",
            ephemeral=True,
        )
        return

    if not is_open:
        await interaction.response.send_message(
            "その派閥はクローズ状態です。参加には招待が必要です。",
            ephemeral=True,
        )
        return

    base_role = guild.get_role(base_role_id)
    if not base_role:
        await interaction.response.send_message(
            "派閥ロールが見つかりません。管理者に連絡してください。",
            ephemeral=True,
        )
        return

    await user.add_roles(base_role)
    await add_faction_member(user.id, faction_id, "member")
    await interaction.response.send_message(
        f"派閥 **{name}** に参加しました！",
        ephemeral=True,
    )


# ===================== 戦争コマンド =====================

@bot.tree.command(name="f_war_start", description="他派閥に戦争を宣言します")
@app_commands.describe(enemy_faction_name="戦争を仕掛ける相手派閥名")
async def faction_war_start_cmd(
    interaction: discord.Interaction,
    enemy_faction_name: str,
):
    guild = interaction.guild
    user = interaction.user

    if guild is None or not isinstance(user, discord.Member):
        await interaction.response.send_message(
            "サーバー内でのみ使用できます。",
            ephemeral=True,
        )
        return

    if await get_active_war(guild.id):
        await interaction.response.send_message(
            "既に他の戦争が進行中です。先に終了させてください。",
            ephemeral=True,
        )
        return

    my_faction_id, my_role = await get_faction_role(user.id, guild.id)
    if not my_faction_id or my_role not in ("leader", "officer"):
        await interaction.response.send_message(
            "このコマンドは派閥のリーダーまたは幹部のみ使用できます。",
            ephemeral=True,
        )
        return

    my_faction = await get_faction_by_id(my_faction_id)
    if not my_faction:
        await interaction.response.send_message(
            "自派閥情報が見つかりません。",
            ephemeral=True,
        )
        return

    enemy_faction = await get_faction_by_name(enemy_faction_name, guild.id)
    if not enemy_faction:
        await interaction.response.send_message(
            "指定された派閥が見つかりません。",
            ephemeral=True,
        )
        return

    if enemy_faction[0] == my_faction_id:
        await interaction.response.send_message(
            "自分の派閥に戦争を宣言することはできません。",
            ephemeral=True,
        )
        return

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO wars (
                guild_id, attacker_faction_id, defender_faction_id,
                active, attacker_messages, defender_messages
            )
            VALUES (?, ?, ?, 1, 0, 0)
            """,
            (guild.id, my_faction_id, enemy_faction[0]),
        )
        await db.commit()

    attacker_name = my_faction[2]
    defender_name = enemy_faction[1]

    await interaction.response.send_message(
        f"派閥 **{attacker_name}** が **{defender_name}** に戦争を宣言しました！",
        ephemeral=True,
    )

    war_channel = await get_war_status_channel(guild)
    if war_channel:
        await war_channel.send(
            "⚔️ **戦争開始**\n"
            f"攻撃側: **{attacker_name}**\n"
            f"防衛側: **{defender_name}**\n"
            "ここに戦争状況が通知されます。",
        )


@bot.tree.command(name="f_war_status", description="現在の戦争状況を表示します")
async def faction_war_status_cmd(interaction: discord.Interaction):
    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message(
            "サーバー内でのみ使用できます。",
            ephemeral=True,
        )
        return

    war = await get_active_war(guild.id)
    if not war:
        await interaction.response.send_message(
            "現在進行中の戦争はありません。",
            ephemeral=True,
        )
        return

    war_id, attacker_id, defender_id, attacker_msgs, defender_msgs = war
    attacker = await get_faction_by_id(attacker_id)
    defender = await get_faction_by_id(defender_id)
    if not attacker or not defender:
        await interaction.response.send_message(
            "戦争情報の取得に失敗しました。",
            ephemeral=True,
        )
        return

    msg = (
        "現在の戦争状況:\n"
        f"・攻撃側 **{attacker[2]}** メッセージ数: {attacker_msgs}\n"
        f"・防衛側 **{defender[2]}** メッセージ数: {defender_msgs}"
    )

    await interaction.response.send_message(msg, ephemeral=True)

    war_channel = await get_war_status_channel(guild)
    if war_channel:
        await war_channel.send("📊 " + msg)


@bot.tree.command(
    name="f_war_end",
    description="進行中の戦争を終了し、勝敗を確定します（管理者専用）",
)
async def faction_war_end_cmd(interaction: discord.Interaction):
    guild = interaction.guild
    user = interaction.user

    if guild is None or not isinstance(user, discord.Member):
        await interaction.response.send_message(
            "サーバー内でのみ使用できます。",
            ephemeral=True,
        )
        return

    if not user.guild_permissions.administrator:
        await interaction.response.send_message(
            "このコマンドはサーバー管理者のみが実行できます。",
            ephemeral=True,
        )
        return

    war = await get_active_war(guild.id)
    if not war:
        await interaction.response.send_message(
            "現在進行中の戦争はありません。",
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=True)

    war_id, attacker_id, defender_id, attacker_msgs, defender_msgs = war
    attacker = await get_faction_by_id(attacker_id)
    defender = await get_faction_by_id(defender_id)
    if not attacker or not defender:
        await interaction.followup.send(
            "戦争情報の取得に失敗しました。",
            ephemeral=True,
        )
        return

    # 勝敗判定
    if attacker_msgs > defender_msgs:
        winner, loser = attacker, defender
        winner_msgs, loser_msgs = attacker_msgs, defender_msgs
    elif defender_msgs > attacker_msgs:
        winner, loser = defender, attacker
        winner_msgs, loser_msgs = defender_msgs, attacker_msgs
    else:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("UPDATE wars SET active = 0 WHERE id = ?", (war_id,))
            await db.commit()
        msg = (
            "戦争は引き分けです。\n"
            f"攻撃側 **{attacker[2]}**: {attacker_msgs} メッセージ\n"
            f"防衛側 **{defender[2]}**: {defender_msgs} メッセージ"
        )
        await interaction.followup.send(msg, ephemeral=True)
        war_channel = await get_war_status_channel(guild)
        if war_channel:
            await war_channel.send("⚪ " + msg)
        return

    await destroy_faction(guild, loser)

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE wars SET active = 0 WHERE id = ?", (war_id,))
        await db.commit()

    msg = (
        "戦争終了！\n"
        f"勝者: **{winner[2]}** （{winner_msgs} メッセージ）\n"
        f"敗者: **{loser[2]}** （{loser_msgs} メッセージ）\n"
        f"敗北派閥 **{loser[2]}** は解体されました。"
    )
    await interaction.followup.send(msg, ephemeral=True)

    war_channel = await get_war_status_channel(guild)
    if war_channel:
        await war_channel.send("🏁 " + msg)


# ===================== 全体チャンネルセットアップ =====================

@bot.tree.command(
    name="setup_global",
    description="全体雑談・全体VCなどをまとめて作成します（管理者専用）",
)
async def setup_global_cmd(interaction: discord.Interaction):
    guild = interaction.guild
    user = interaction.user

    if guild is None or not isinstance(user, discord.Member):
        await interaction.response.send_message(
            "サーバー内でのみ使用できます。",
            ephemeral=True,
        )
        return

    if not user.guild_permissions.administrator:
        await interaction.response.send_message(
            "このコマンドはサーバー管理者のみが実行できます。",
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=True)

    category: discord.CategoryChannel = await guild.create_category("全体")

    # テキスト
    await guild.create_text_channel("全体雑談", category=category)

    # 全体フォーラム
    await create_forum_channel(guild, "フォーラム", category=category)

    await guild.create_text_channel("管理者への意見", category=category)

    # VC
    await guild.create_voice_channel("全体VC1", category=category)
    await guild.create_voice_channel("全体VC2", category=category)

    # 聞き専テキスト
    await guild.create_text_channel("全体VC聞き専1", category=category)
    await guild.create_text_channel("全体VC聞き専2", category=category)

    # 戦争状況チャンネル
    war_channel = await guild.create_text_channel("戦争状況", category=category)
    await set_guild_war_status_channel_id(guild.id, war_channel.id)

    await interaction.followup.send(
        "全体チャンネルと戦争状況チャンネルを作成しました。",
        ephemeral=True,
    )


# ===================== 実行部 =====================

def main():
    token = os.getenv("DISCORD_TOKEN", "").strip()
    if not token:
        raise RuntimeError("環境変数 DISCORD_TOKEN にボットトークンを設定してください。")
    bot.run(token)


if __name__ == "__main__":
    keep_alive()
    main()
