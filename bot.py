import discord
from discord import app_commands
from discord.ext import tasks

import sqlite3
from datetime import datetime, timedelta, time
from zoneinfo import ZoneInfo


# ============================================================
# 設定
# ============================================================

TOKEN = ""

# 作業用VC
WORK_VOICE_CHANNEL_ID = 1554820775083511928

# ランキングを投稿するテキストチャンネル
REPORT_CHANNEL_ID = 1554823179480862770

# 1日に必要な作業時間
MIN_WORK_MINUTES = 30

# 自動ランキング投稿時刻（日本時間）
REPORT_HOUR = 21
REPORT_MINUTE = 0

JST = ZoneInfo("Asia/Tokyo")


# ============================================================
# Discord
# ============================================================

intents = discord.Intents.default()

# VCの入退室・ミュート状態
intents.voice_states = True

# 「取り込み中」を検出するために必要
intents.presences = True

client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)


# ============================================================
# SQLite
# ============================================================

db = sqlite3.connect(
    "work_data.db",
    check_same_thread=False
)
db.row_factory = sqlite3.Row


def init_database():
    cursor = db.cursor()

    # 日ごとの作業時間
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS daily_work (
            user_id INTEGER NOT NULL,
            date TEXT NOT NULL,
            seconds INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (user_id, date)
        )
    """)

    # 現在「作業中」と判定されているセッション
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS active_sessions (
            user_id INTEGER PRIMARY KEY,
            started_at TEXT NOT NULL,
            last_saved_at TEXT NOT NULL
        )
    """)

    # 自動ランキング投稿済み記録
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS report_log (
            report_type TEXT NOT NULL,
            period TEXT NOT NULL,
            PRIMARY KEY (report_type, period)
        )
    """)

    # Botが自動ミュートしたユーザー
    # Bot自身がミュートした人だけ、退出時に解除する
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS forced_mutes (
            user_id INTEGER PRIMARY KEY
        )
    """)

    db.commit()


# ============================================================
# 時刻
# ============================================================

def now_jst():
    return datetime.now(JST)


def datetime_to_string(dt):
    return dt.isoformat()


def string_to_datetime(value):
    return datetime.fromisoformat(value)


# ============================================================
# 作業時間保存
# ============================================================

def save_daily_seconds(user_id, date, seconds):
    if seconds <= 0:
        return

    cursor = db.cursor()

    cursor.execute("""
        INSERT INTO daily_work (
            user_id,
            date,
            seconds
        )
        VALUES (?, ?, ?)

        ON CONFLICT(user_id, date)
        DO UPDATE SET
            seconds = seconds + excluded.seconds
    """, (
        user_id,
        date,
        seconds
    ))

    db.commit()


def add_work_time(user_id, start, end):
    """start～endを日付ごとに分割して保存"""

    if end <= start:
        return

    current = start

    while current.date() < end.date():

        next_day = datetime(
            current.year,
            current.month,
            current.day,
            tzinfo=JST
        ) + timedelta(days=1)

        seconds = int(
            (next_day - current).total_seconds()
        )

        save_daily_seconds(
            user_id,
            current.date().isoformat(),
            seconds
        )

        current = next_day

    if current < end:

        seconds = int(
            (end - current).total_seconds()
        )

        save_daily_seconds(
            user_id,
            current.date().isoformat(),
            seconds
        )


# ============================================================
# 「取り込み中」判定
# ============================================================

def is_dnd(member):
    return member.status == discord.Status.dnd


def is_in_work_vc(member):
    return (
        member.voice is not None
        and member.voice.channel is not None
        and member.voice.channel.id == WORK_VOICE_CHANNEL_ID
    )


def should_count_work(member):
    """
    作業時間としてカウントする条件：

    1. 作業用VCにいる
    2. Discordステータスが「取り込み中」
    """

    return is_in_work_vc(member) and is_dnd(member)


# ============================================================
# 作業セッション
# ============================================================

def is_session_active(user_id):
    cursor = db.cursor()

    cursor.execute("""
        SELECT 1
        FROM active_sessions
        WHERE user_id = ?
    """, (user_id,))

    return cursor.fetchone() is not None


def start_session(user_id):
    if is_session_active(user_id):
        return

    now = now_jst()

    cursor = db.cursor()

    cursor.execute("""
        INSERT INTO active_sessions (
            user_id,
            started_at,
            last_saved_at
        )
        VALUES (?, ?, ?)
    """, (
        user_id,
        datetime_to_string(now),
        datetime_to_string(now)
    ))

    db.commit()

    print(f"[WORK START] user={user_id}")


def end_session(user_id):
    cursor = db.cursor()

    cursor.execute("""
        SELECT *
        FROM active_sessions
        WHERE user_id = ?
    """, (user_id,))

    row = cursor.fetchone()

    if row is None:
        return

    start = string_to_datetime(
        row["last_saved_at"]
    )

    end = now_jst()

    add_work_time(
        user_id,
        start,
        end
    )

    cursor.execute("""
        DELETE FROM active_sessions
        WHERE user_id = ?
    """, (user_id,))

    db.commit()

    print(f"[WORK END] user={user_id}")


def save_active_sessions():
    """
    現在カウント中のセッションを60秒ごとに保存。
    """

    cursor = db.cursor()

    cursor.execute("""
        SELECT *
        FROM active_sessions
    """)

    rows = cursor.fetchall()

    now = now_jst()

    for row in rows:

        user_id = row["user_id"]

        last_saved = string_to_datetime(
            row["last_saved_at"]
        )

        if now <= last_saved:
            continue

        add_work_time(
            user_id,
            last_saved,
            now
        )

        cursor.execute("""
            UPDATE active_sessions
            SET last_saved_at = ?
            WHERE user_id = ?
        """, (
            datetime_to_string(now),
            user_id
        ))

    db.commit()


@tasks.loop(seconds=60)
async def session_save_loop():
    save_active_sessions()


# ============================================================
# 自動ミュート
# ============================================================

def was_forced_muted(user_id):
    cursor = db.cursor()

    cursor.execute("""
        SELECT 1
        FROM forced_mutes
        WHERE user_id = ?
    """, (user_id,))

    return cursor.fetchone() is not None


def mark_forced_muted(user_id):
    cursor = db.cursor()

    cursor.execute("""
        INSERT OR IGNORE INTO forced_mutes (user_id)
        VALUES (?)
    """, (user_id,))

    db.commit()


def unmark_forced_muted(user_id):
    cursor = db.cursor()

    cursor.execute("""
        DELETE FROM forced_mutes
        WHERE user_id = ?
    """, (user_id,))

    db.commit()


async def mute_for_work(member):
    """
    作業VCに入った人をサーバーミュート。
    すでにサーバーミュートされている場合は記録しない。
    """

    if member.bot:
        return

    if member.voice is None:
        return

    # すでにサーバーミュートなら、
    # Botが付けたものではない可能性があるので触らない
    if member.voice.mute:
        return

    try:
        await member.edit(
            mute=True,
            reason="作業用VCの自動ミュート"
        )

        mark_forced_muted(member.id)

        print(
            f"[AUTO MUTE] {member.display_name}"
        )

    except discord.Forbidden:
        print(
            f"[ERROR] {member.display_name} を"
            "ミュートする権限がありません"
        )

    except discord.HTTPException as e:
        print(
            f"[ERROR] ミュート失敗: {e}"
        )


async def unmute_after_work(member):
    """
    Bot自身が自動ミュートした人だけ解除。
    """

    if not was_forced_muted(member.id):
        return

    try:
        if member.voice is not None:
            await member.edit(
                mute=False,
                reason="作業用VC退出による自動ミュート解除"
            )

    except discord.Forbidden:
        print(
            f"[ERROR] {member.display_name} の"
            "ミュート解除権限がありません"
        )

    except discord.HTTPException as e:
        print(
            f"[ERROR] ミュート解除失敗: {e}"
        )

    finally:
        unmark_forced_muted(member.id)


# ============================================================
# 作業状態更新
# ============================================================

async def update_work_state(member):
    """
    VC・取り込み中の状態を見て、
    作業時間計測を開始/停止する。
    """

    if member.bot:
        return

    should_work = should_count_work(member)
    currently_working = is_session_active(member.id)

    if should_work and not currently_working:
        start_session(member.id)

    elif not should_work and currently_working:
        end_session(member.id)


# ============================================================
# VC状態変更
# ============================================================

@client.event
async def on_voice_state_update(member, before, after):

    if member.bot:
        return

    before_channel_id = (
        before.channel.id
        if before.channel is not None
        else None
    )

    after_channel_id = (
        after.channel.id
        if after.channel is not None
        else None
    )

    # --------------------------------------------------------
    # 作業VCに入った
    # --------------------------------------------------------

    if (
        after_channel_id == WORK_VOICE_CHANNEL_ID
        and before_channel_id != WORK_VOICE_CHANNEL_ID
    ):
        await mute_for_work(member)

    # --------------------------------------------------------
    # 作業VCから出た
    # --------------------------------------------------------

    if (
        before_channel_id == WORK_VOICE_CHANNEL_ID
        and after_channel_id != WORK_VOICE_CHANNEL_ID
    ):
        await unmute_after_work(member)

    # --------------------------------------------------------
    # VC移動・入退室によって作業状態が変わった
    # --------------------------------------------------------

    await update_work_state(member)


# ============================================================
# Discordステータス変更
# ============================================================

@client.event
async def on_presence_update(before, after):

    if after.bot:
        return

    # 取り込み中になった / 解除された
    if before.status != after.status:

        await update_work_state(after)


# ============================================================
# Bot起動時の復旧
# ============================================================

async def recover_voice_state():

    for guild in client.guilds:

        channel = guild.get_channel(
            WORK_VOICE_CHANNEL_ID
        )

        if channel is None:
            continue

        for member in channel.members:

            if member.bot:
                continue

            # 作業VCにいる人は自動ミュート
            await mute_for_work(member)

            # VC + 取り込み中なら計測再開
            await update_work_state(member)

            print(
                f"[RECOVER] {member.display_name}"
            )


# ============================================================
# 週/月の期間
# ============================================================

def get_week_start(date):
    # 月曜日
    return date - timedelta(
        days=date.weekday()
    )


def get_month_start(date):
    return date.replace(day=1)


def get_month_end(date):

    if date.month == 12:

        next_month = date.replace(
            year=date.year + 1,
            month=1,
            day=1
        )

    else:

        next_month = date.replace(
            month=date.month + 1,
            day=1
        )

    return next_month - timedelta(days=1)


# ============================================================
# 統計
# ============================================================

def get_user_stats(user_id):

    today = now_jst().date()

    week_start = get_week_start(today)
    month_start = get_month_start(today)

    cursor = db.cursor()

    # 今週
    cursor.execute("""
        SELECT COALESCE(SUM(seconds), 0) AS seconds
        FROM daily_work
        WHERE user_id = ?
        AND date >= ?
        AND date <= ?
    """, (
        user_id,
        week_start.isoformat(),
        today.isoformat()
    ))

    week_seconds = cursor.fetchone()["seconds"]

    # 今月
    cursor.execute("""
        SELECT COALESCE(SUM(seconds), 0) AS seconds
        FROM daily_work
        WHERE user_id = ?
        AND date >= ?
        AND date <= ?
    """, (
        user_id,
        month_start.isoformat(),
        today.isoformat()
    ))

    month_seconds = cursor.fetchone()["seconds"]

    # 今月の作業日数
    cursor.execute("""
        SELECT COUNT(*) AS days
        FROM daily_work
        WHERE user_id = ?
        AND date >= ?
        AND date <= ?
        AND seconds >= ?
    """, (
        user_id,
        month_start.isoformat(),
        today.isoformat(),
        MIN_WORK_MINUTES * 60
    ))

    month_days = cursor.fetchone()["days"]

    # 累計
    cursor.execute("""
        SELECT COALESCE(SUM(seconds), 0) AS seconds
        FROM daily_work
        WHERE user_id = ?
    """, (user_id,))

    total_seconds = cursor.fetchone()["seconds"]

    return (
        week_seconds,
        month_seconds,
        month_days,
        total_seconds
    )


def format_duration(seconds):

    hours = seconds // 3600
    minutes = (seconds % 3600) // 60

    return f"{hours}時間 {minutes}分"


# ============================================================
# /stats
# ============================================================

@tree.command(
    name="stats",
    description="自分の作業統計を表示します"
)
async def stats(interaction):

    (
        week_seconds,
        month_seconds,
        month_days,
        total_seconds
    ) = get_user_stats(
        interaction.user.id
    )

    today = now_jst().date()
    week_start = get_week_start(today)

    embed = discord.Embed(
        title=f"📊 {interaction.user.display_name} の作業統計",
        color=discord.Color.blue()
    )

    embed.add_field(
        name="今週",
        value=(
            f"作業時間："
            f"**{format_duration(week_seconds)}**\n"
            f"期間：{week_start} ～ {today}"
        ),
        inline=False
    )

    embed.add_field(
        name="今月",
        value=(
            f"作業時間："
            f"**{format_duration(month_seconds)}**\n"
            f"作業日：**{month_days}日**"
        ),
        inline=False
    )

    embed.add_field(
        name="累計",
        value=(
            f"作業時間："
            f"**{format_duration(total_seconds)}**"
        ),
        inline=False
    )

    embed.set_footer(
        text=(
            f"作業VC + 取り込み中で計測 / "
            f"1日{MIN_WORK_MINUTES}分以上で作業日"
        )
    )

    await interaction.response.send_message(
        embed=embed,
        ephemeral=True
    )


# ============================================================
# 週次ランキング
# ============================================================

def get_weekly_ranking(start_date, end_date):

    cursor = db.cursor()

    cursor.execute("""
        SELECT
            user_id,
            SUM(seconds) AS seconds
        FROM daily_work
        WHERE date >= ?
        AND date <= ?
        GROUP BY user_id
        ORDER BY seconds DESC
        LIMIT 10
    """, (
        start_date.isoformat(),
        end_date.isoformat()
    ))

    return cursor.fetchall()


# ============================================================
# 月次ランキング
# ============================================================

def get_monthly_ranking(start_date, end_date):

    cursor = db.cursor()

    cursor.execute("""
        SELECT
            user_id,
            COUNT(*) AS days
        FROM daily_work
        WHERE date >= ?
        AND date <= ?
        AND seconds >= ?
        GROUP BY user_id
        ORDER BY days DESC
        LIMIT 10
    """, (
        start_date.isoformat(),
        end_date.isoformat(),
        MIN_WORK_MINUTES * 60
    ))

    return cursor.fetchall()


# ============================================================
# ユーザー名
# ============================================================

async def get_member_name(guild, user_id):

    member = guild.get_member(user_id)

    if member:
        return member.display_name

    try:
        user = await client.fetch_user(user_id)
        return user.display_name

    except Exception:
        return f"User {user_id}"


# ============================================================
# 週次Embed
# ============================================================

async def create_weekly_embed(
    guild,
    start_date,
    end_date
):

    rows = get_weekly_ranking(
        start_date,
        end_date
    )

    embed = discord.Embed(
        title="🏆 今週の作業時間ランキング",
        description=(
            f"{start_date} ～ {end_date}\n"
            f"条件：作業VC + 取り込み中"
        ),
        color=discord.Color.gold()
    )

    if not rows:

        embed.add_field(
            name="結果",
            value="まだ作業記録がありません。",
            inline=False
        )

        return embed

    text = ""

    medals = [
        "🥇",
        "🥈",
        "🥉"
    ]

    for index, row in enumerate(rows):

        name = await get_member_name(
            guild,
            row["user_id"]
        )

        if index < 3:
            prefix = medals[index]
        else:
            prefix = f"**{index + 1}位**"

        text += (
            f"{prefix} "
            f"**{name}** "
            f"— {format_duration(row['seconds'])}\n"
        )

    embed.add_field(
        name="作業時間",
        value=text,
        inline=False
    )

    return embed


# ============================================================
# 月次Embed
# ============================================================

async def create_monthly_embed(
    guild,
    start_date,
    end_date
):

    rows = get_monthly_ranking(
        start_date,
        end_date
    )

    embed = discord.Embed(
        title="📅 月間作業日数ランキング",
        description=(
            f"{start_date} ～ {end_date}\n"
            f"作業VC + 取り込み中で計測\n"
            f"1日{MIN_WORK_MINUTES}分以上を作業日として集計"
        ),
        color=discord.Color.purple()
    )

    if not rows:

        embed.add_field(
            name="結果",
            value="まだ作業記録がありません。",
            inline=False
        )

        return embed

    text = ""

    medals = [
        "🥇",
        "🥈",
        "🥉"
    ]

    for index, row in enumerate(rows):

        name = await get_member_name(
            guild,
            row["user_id"]
        )

        if index < 3:
            prefix = medals[index]
        else:
            prefix = f"**{index + 1}位**"

        text += (
            f"{prefix} "
            f"**{name}** "
            f"— {row['days']}日\n"
        )

    embed.add_field(
        name="作業日数",
        value=text,
        inline=False
    )

    return embed


# ============================================================
# /ranking
# ============================================================

@tree.command(
    name="ranking",
    description="今週の作業時間ランキングを表示します"
)
async def ranking(interaction):

    today = now_jst().date()

    week_start = get_week_start(today)

    # 月～金
    week_end = week_start + timedelta(days=4)

    if today < week_end:
        week_end = today

    embed = await create_weekly_embed(
        interaction.guild,
        week_start,
        week_end
    )

    await interaction.response.send_message(
        embed=embed
    )


# ============================================================
# 自動投稿管理
# ============================================================

def already_posted(report_type, period):

    cursor = db.cursor()

    cursor.execute("""
        SELECT 1
        FROM report_log
        WHERE report_type = ?
        AND period = ?
    """, (
        report_type,
        period
    ))

    return cursor.fetchone() is not None


def mark_posted(report_type, period):

    cursor = db.cursor()

    cursor.execute("""
        INSERT OR IGNORE INTO report_log (
            report_type,
            period
        )
        VALUES (?, ?)
    """, (
        report_type,
        period
    ))

    db.commit()


async def get_report_channel():

    try:
        # キャッシュではなくDiscord APIから取得
        return await client.fetch_channel(
            REPORT_CHANNEL_ID
        )

    except discord.NotFound:
        print(
            "[ERROR] REPORT_CHANNEL_ID の"
            "チャンネルが存在しません"
        )

    except discord.Forbidden:
        print(
            "[ERROR] Botに報告チャンネルを見る"
            "権限がありません"
        )

    except discord.HTTPException as e:
        print(
            f"[ERROR] 報告チャンネル取得失敗: {e}"
        )

    return None


# ============================================================
# 金曜日ランキング
# ============================================================

async def post_weekly_report():

    today = now_jst().date()

    week_start = get_week_start(today)
    friday = week_start + timedelta(days=4)

    period = friday.strftime("%Y-W%W")

    if already_posted(
        "weekly",
        period
    ):
        return

    channel = await get_report_channel()

    if channel is None:
        return

    if not isinstance(
        channel,
        discord.TextChannel
    ):
        print(
            "[ERROR] REPORT_CHANNEL_ID は"
            "テキストチャンネルではありません"
        )
        return

    embed = await create_weekly_embed(
        channel.guild,
        week_start,
        friday
    )

    try:

        await channel.send(
            embed=embed
        )

    except discord.Forbidden:
        print(
            "[ERROR] 報告チャンネルへの"
            "メッセージ送信権限がありません"
        )
        return

    mark_posted(
        "weekly",
        period
    )

    print(
        f"[REPORT] weekly {period}"
    )


# ============================================================
# 月末ランキング
# ============================================================

async def post_monthly_report():

    today = now_jst().date()

    month_start = get_month_start(today)
    month_end = get_month_end(today)

    period = month_start.strftime("%Y-%m")

    if already_posted(
        "monthly",
        period
    ):
        return

    channel = await get_report_channel()

    if channel is None:
        return

    if not isinstance(
        channel,
        discord.TextChannel
    ):
        print(
            "[ERROR] REPORT_CHANNEL_ID は"
            "テキストチャンネルではありません"
        )
        return

    embed = await create_monthly_embed(
        channel.guild,
        month_start,
        month_end
    )

    try:

        await channel.send(
            embed=embed
        )

    except discord.Forbidden:
        print(
            "[ERROR] 報告チャンネルへの"
            "メッセージ送信権限がありません"
        )
        return

    mark_posted(
        "monthly",
        period
    )

    print(
        f"[REPORT] monthly {period}"
    )


# ============================================================
# 自動ランキング判定
# ============================================================

@tasks.loop(seconds=60)
async def report_scheduler():

    now = now_jst()

    report_time = time(
        REPORT_HOUR,
        REPORT_MINUTE
    )

    # 金曜日 21:00以降
    if (
        now.weekday() == 4
        and now.time() >= report_time
    ):
        await post_weekly_report()

    # 月末 21:00以降
    if (
        now.date() == get_month_end(now.date())
        and now.time() >= report_time
    ):
        await post_monthly_report()


# ============================================================
# Bot起動
# ============================================================

@client.event
async def on_ready():

    init_database()

    await tree.sync()

    # 起動時点ですでに作業VCにいる人を復旧
    await recover_voice_state()

    if not session_save_loop.is_running():
        session_save_loop.start()

    if not report_scheduler.is_running():
        report_scheduler.start()

    print(
        f"ログインしました: {client.user}"
    )

    print(
        "作業時間の記録を開始しました。"
    )

    print(
        "作業時間条件：作業VC + 取り込み中"
    )


# ============================================================
# 実行
# ============================================================

client.run(TOKEN)
