"""Генератор графиков для бота"""

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from matplotlib.patches import FancyBboxPatch, Rectangle
from matplotlib.ticker import FuncFormatter
import io
from datetime import datetime
from database import get_weight_history, get_monthly_report_data, get_user, _days_word as days_word


def generate_weight_chart(user_id: int) -> io.BytesIO:
    history = get_weight_history(user_id, limit=30)
    if len(history) < 2:
        return None
    history = list(reversed(history))
    dates = [datetime.fromisoformat(h['recorded_at']).strftime("%d.%m") for h in history]
    weights = [h['weight'] for h in history]
    fig, ax = plt.subplots(figsize=(8, 4))
    ax.plot(dates, weights, marker='o', color='#2ecc71', linewidth=2.5, markersize=6)
    ax.fill_between(dates, weights, alpha=0.25, color='#2ecc71')
    ax.set_title('Динамика веса', fontsize=14, fontweight='bold', pad=15)
    ax.set_xlabel('Дата', fontsize=10)
    ax.set_ylabel('Вес, кг', fontsize=10)
    ax.grid(True, alpha=0.3, linestyle='--')
    plt.xticks(rotation=45, ha='right')
    plt.tight_layout()
    buf = io.BytesIO()
    plt.savefig(buf, format='png', dpi=150, bbox_inches='tight')
    buf.seek(0)
    plt.close(fig)
    return buf


# === Отчёт за месяц (одна картинка: итоги + график веса) ===
GREEN = "#2ecc71"
DARK_GREEN = "#1f6f43"
TEXT = "#1f3d2b"
MUTED = "#6b7f72"
BACKGROUND = "#f6faf7"
CARD_BORDER = "#dfeae2"

MIN_LOG_DAYS = 3  # меньше дней с записями — привычки в отчёте не показываем


def _num(x: float, digits: int = 1) -> str:
    return f"{x:.{digits}f}".replace(".", ",")


def _signed(x: float) -> str:
    sign = "\u2212" if x < 0 else "+" if x > 0 else ""
    return f"{sign}{_num(abs(x))}"


def _draw_tile(fig, x, y, w, h, title, value, sub):
    fig.add_artist(FancyBboxPatch(
        (x, y), w, h, boxstyle="round,pad=0,rounding_size=0.018", mutation_aspect=0.8,
        transform=fig.transFigure, facecolor="white", edgecolor=CARD_BORDER, linewidth=1.4))
    fig.text(x + 0.03, y + h - 0.022, title, fontsize=11, color=MUTED, va="top")
    fig.text(x + 0.03, y + h * 0.47, value, fontsize=27, fontweight="bold", color=TEXT, va="center")
    fig.text(x + 0.03, y + 0.018, sub, fontsize=9.5, color=MUTED, va="bottom")


def generate_monthly_report(user_id: int):
    """Картинка-отчёт за 30 дней или None, если данных пока слишком мало."""
    data = get_monthly_report_data(user_id, days=30)
    weights = data["weights"]
    enough_logs = data["days_logged"] >= MIN_LOG_DAYS
    if len(weights) < 2 and not enough_logs:
        return None

    tiles = []
    if len(weights) >= 2:
        first, last = weights[0][1], weights[-1][1]
        tiles.append(("Вес за период", f"{_signed(last - first)} кг", f"с {_num(first)} до {_num(last)} кг"))
    if enough_logs:
        if data["habits_percent"] is not None:
            tiles.append(("Привычки", f"{data['habits_percent']}%", f"выполнено, дней с записью: {data['days_logged']}"))
        if data["best_streak"] > 0:
            tiles.append(("Лучшая серия", f"{data['best_streak']} {days_word(data['best_streak'])}",
                          f"сейчас: {data['current_streak']}"))

    user = get_user(user_id) or {}
    name = user.get("name") or ""
    period = f"{data['start'].strftime('%d.%m')} \u2014 {data['end'].strftime('%d.%m.%Y')}"

    fig = plt.figure(figsize=(8, 10), facecolor=BACKGROUND)

    # шапка
    fig.add_artist(Rectangle((0, 0.90), 1, 0.10, transform=fig.transFigure, facecolor=GREEN, edgecolor="none"))
    fig.text(0.05, 0.958, "Отчёт за 30 дней", fontsize=25, fontweight="bold", color="white", va="center")
    fig.text(0.05, 0.918, f"{name} \u00b7 {period}" if name else period, fontsize=12, color="white", va="center")

    # плитки с итогами: по две в ряд, одинокая последняя — на всю ширину
    left, total_w, gap, tile_h = 0.05, 0.90, 0.025, 0.115
    tile_w = (total_w - gap) / 2
    y_top = 0.875
    for index, (title, value, sub) in enumerate(tiles):
        row, col = divmod(index, 2)
        y = y_top - tile_h - row * (tile_h + gap)
        alone_in_row = index == len(tiles) - 1 and len(tiles) % 2 == 1
        if alone_in_row:
            _draw_tile(fig, left, y, total_w, tile_h, title, value, sub)
        else:
            _draw_tile(fig, left + col * (tile_w + gap), y, tile_w, tile_h, title, value, sub)
    rows = (len(tiles) + 1) // 2
    tiles_bottom = y_top - rows * tile_h - max(rows - 1, 0) * gap if tiles else y_top

    # график веса
    chart_top = tiles_bottom - 0.05
    chart_bottom = 0.10
    ax = fig.add_axes([0.11, chart_bottom, 0.82, chart_top - chart_bottom], facecolor="white")
    if len(weights) >= 2:
        dates = [w[0] for w in weights]
        values = [w[1] for w in weights]
        ax.plot(dates, values, marker="o", color=GREEN, linewidth=2.8, markersize=7)
        ax.fill_between(dates, values, min(values) - 0.5, alpha=0.18, color=GREEN)
        ax.annotate(_num(values[0]), (dates[0], values[0]), textcoords="offset points", xytext=(0, 10),
                    ha="center", fontsize=10, color=DARK_GREEN, fontweight="bold")
        ax.annotate(_num(values[-1]), (dates[-1], values[-1]), textcoords="offset points", xytext=(0, 10),
                    ha="center", fontsize=10, color=DARK_GREEN, fontweight="bold")
        ax.set_ylim(min(values) - 0.8, max(values) + 0.8)
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%d.%m"))
        ax.xaxis.set_major_locator(mdates.AutoDateLocator(minticks=3, maxticks=7))
        ax.grid(True, alpha=0.3, linestyle="--")
        for side in ("top", "right"):
            ax.spines[side].set_visible(False)
        for side in ("left", "bottom"):
            ax.spines[side].set_color(CARD_BORDER)
        ax.yaxis.set_major_formatter(FuncFormatter(lambda value, _: _num(value).replace(",0", "")))
        ax.set_ylabel("Вес, кг", fontsize=10, color=MUTED)
        ax.tick_params(colors=MUTED, labelsize=9)
    else:
        ax.axis("off")
        ax.text(0.5, 0.5, "Для графика веса нужно минимум 2 взвешивания за месяц",
                ha="center", va="center", fontsize=12, color=MUTED, transform=ax.transAxes)
    ax.set_title("Динамика веса", fontsize=14, fontweight="bold", color=TEXT, loc="left", pad=14)

    fig.text(0.5, 0.04, f"ПолезноБот \u00b7 отчёт сформирован {data['end'].strftime('%d.%m.%Y')}",
             ha="center", fontsize=9, color=MUTED)

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=130, facecolor=fig.get_facecolor())
    buf.seek(0)
    plt.close(fig)
    return buf
