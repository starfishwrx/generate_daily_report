from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any, Dict, List

from PIL import Image, ImageDraw, ImageFont


COLOR_BORDER = (0, 0, 0)
COLOR_TITLE_BG = (151, 203, 108)
COLOR_HEADER_BG = (230, 230, 230)
COLOR_BODY_BG = (242, 242, 242)
COLOR_POS_BG = (247, 225, 225)
COLOR_NEG_BG = (223, 236, 210)
COLOR_TOTAL_BG = (245, 182, 79)
COLOR_COMPARE_LABEL_BG = (153, 217, 234)
COLOR_TEXT = (20, 20, 20)
COLOR_RED = (220, 40, 40)
COLOR_GREEN = (0, 146, 76)


def render_extra_metrics_block(extra_metrics: Dict[str, Any]) -> str:
    notes = extra_metrics.get("notes", {}) if isinstance(extra_metrics, dict) else {}
    top_games = extra_metrics.get("top_games", []) if isinstance(extra_metrics, dict) else []
    warnings = extra_metrics.get("warnings", []) if isinstance(extra_metrics, dict) else []
    payment_images = extra_metrics.get("payment_images", {}) if isinstance(extra_metrics, dict) else {}

    lines: List[str] = []
    lines.append("备注：")

    new_users = notes.get("new_users", {})
    active_users = notes.get("active_users", {})
    if new_users:
        lines.append(
            "1、新增用户为：%s，与昨日同期环比%s，与上周同期对比%s。"
            % (
                _fmt_num(new_users.get("value")),
                _trend_ratio_text(str(new_users.get("day_ratio") or "")),
                _trend_ratio_text(str(new_users.get("week_ratio") or "")),
            )
        )
    else:
        lines.append("1、新增用户：暂未获取。")

    if active_users:
        lines.append(
            "2、活跃用户为：%s，与昨日同期环比%s，与上周同期对比%s。"
            % (
                _fmt_num(active_users.get("value")),
                _trend_ratio_text(str(active_users.get("day_ratio") or "")),
                _trend_ratio_text(str(active_users.get("week_ratio") or "")),
            )
        )
    else:
        lines.append("2、活跃用户：暂未获取。")

    lines.append("3、会员充值明细：")
    lines.append(
        "①、会员付费率为：%s，会员充值总金额为：%s元，与上周同期对比%s。"
        % (
            str(notes.get("member_pay_rate") or "暂未获取"),
            _fmt_num(notes.get("member_recharge_amount")),
            _trend_ratio_text(str(notes.get("member_recharge_week_ratio") or "")),
        )
    )
    lines.append(
        "②、今日云玩会员开通人数为：%s人，有效期内会员数为：%s人。"
        % (
            _fmt_num(notes.get("member_open_count")),
            _fmt_num(notes.get("member_valid_count")),
        )
    )

    lines.append("4、充值明细：")
    lines.append(
        "①、页游充值为：%s元，与上周同期对比%s。"
        % (
            _fmt_num(notes.get("web_night_recharge")),
            _trend_delta_text(notes.get("web_night_recharge_week_delta")),
        )
    )
    lines.append(
        "②、手游充值为：%s元，与上周同期对比%s。"
        % (
            _fmt_num(notes.get("mobile_recharge")),
            _trend_delta_text(notes.get("mobile_recharge_week_delta")),
        )
    )

    if warnings:
        lines.append("备注：部分外部接口未取到数据 -> %s" % "；".join(str(w) for w in warnings))

    if isinstance(payment_images, dict) and payment_images:
        lines.append("")
        lines.append("具体：")
        page_img = str(payment_images.get("page") or "").strip()
        mobile_img = str(payment_images.get("mobile") or "").strip()
        if page_img:
            lines.append(f"页游付费表图片：{page_img}")
        if mobile_img:
            lines.append(f"手游付费表图片：{mobile_img}")

    if top_games:
        lines.append("")
        lines.append("—————————————————————")
        lines.append("二、云游戏活跃用户top(去重)")
        lines.append("")
        lines.append("| 游戏 | 活跃用户数 |")
        lines.append("| :---: | :---: |")
        for item in top_games:
            lines.append("| %s | %s |" % (str(item.get("name") or "-"), _fmt_num(item.get("active_users"))))

    return "\n".join(lines)


def render_payment_table_images(payment_tables: Dict[str, Any], charts_dir: Path) -> Dict[str, Path]:
    out: Dict[str, Path] = {}
    charts_dir.mkdir(parents=True, exist_ok=True)

    page_data = payment_tables.get("page")
    if isinstance(page_data, dict):
        page_path = charts_dir / _build_page_filename(page_data)
        _render_page_table(page_data, page_path)
        out["page"] = page_path

    mobile_data = payment_tables.get("mobile")
    if isinstance(mobile_data, dict):
        mobile_path = charts_dir / _build_mobile_filename(mobile_data)
        _render_mobile_table(mobile_data, mobile_path)
        out["mobile"] = mobile_path

    return out


def _fmt_num(value: Any) -> str:
    if isinstance(value, int):
        return f"{value:,}"
    if isinstance(value, float):
        return f"{int(round(value)):,}"
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return "暂未获取"
        try:
            return f"{int(round(float(text.replace(',', '')))):,}"
        except ValueError:
            return text
    return "暂未获取"


def _trend_ratio_text(ratio: str) -> str:
    text = ratio.strip()
    if not text:
        return "暂未获取"
    if text in {"0.00%", "+0.00%", "-0.00%"}:
        return "持平"
    if text.startswith("+"):
        return f"上涨{text[1:]}"
    if text.startswith("-"):
        return f"下降{text[1:]}"
    if text.endswith("%"):
        try:
            value = float(text[:-1].replace(",", "").strip())
        except ValueError:
            return text
        if abs(value) < 0.005:
            return "持平"
        if value > 0:
            return f"上涨{text}"
        return f"下降{abs(value):.2f}%"
    return text


def _trend_delta_text(delta: Any) -> str:
    try:
        iv = int(delta)
    except (TypeError, ValueError):
        return "暂未获取"
    if iv > 0:
        return f"上涨{iv:,}元"
    if iv < 0:
        return f"下降{abs(iv):,}元"
    return "持平0元"


def _build_page_filename(data: Dict[str, Any]) -> str:
    day = str(data.get("today_date") or "").replace("-", "")
    suffix = day if day else "unknown"
    return f"505_page_payment_table_{suffix}.png"


def _build_mobile_filename(data: Dict[str, Any]) -> str:
    day = str(data.get("today_date") or "").replace("-", "")
    suffix = day if day else "unknown"
    return f"505_mobile_payment_table_{suffix}.png"


def _render_page_table(data: Dict[str, Any], output_path: Path) -> None:
    rows = data.get("rows") if isinstance(data.get("rows"), list) else []
    today_label = _to_date_label(str(data.get("today_date") or ""))
    week_label = _to_date_label(str(data.get("week_date") or ""))
    total_today = int(data.get("total_today") or 0)
    total_week = int(data.get("total_week") or 0)
    total_delta = int(data.get("total_delta") or 0)
    title = str(data.get("title") or "页游付费数据")

    col_widths = [380, 380, 380, 380]
    title_h = 42
    head_h = 34
    row_h = 30
    total_h = 34
    width = sum(col_widths) + 1
    height = title_h + head_h + len(rows) * row_h + total_h + 1

    img = Image.new("RGB", (width, height), (255, 255, 255))
    draw = ImageDraw.Draw(img)

    title_font = _load_font(24, bold=True)
    head_font = _load_font(22, bold=True)
    body_font = _load_font(20, bold=False)
    num_font = _load_font(20, bold=False)

    y = 0
    _draw_cell(draw, 0, y, width - 1, title_h, COLOR_TITLE_BG, title, title_font, COLOR_TEXT, align="center")
    y += title_h

    headers = ["游戏名称", today_label, week_label, "对比"]
    x = 0
    for idx, head in enumerate(headers):
        w = col_widths[idx]
        _draw_cell(draw, x, y, w, head_h, COLOR_HEADER_BG, head, head_font, COLOR_TEXT, align="center")
        x += w
    y += head_h

    for row in rows:
        game = str(row.get("game") or "")
        today_val = int(row.get("today") or 0)
        week_val = int(row.get("week") or 0)
        delta = int(row.get("delta") or 0)

        x = 0
        _draw_cell(draw, x, y, col_widths[0], row_h, COLOR_BODY_BG, game, body_font, COLOR_TEXT, align="center")
        x += col_widths[0]
        _draw_cell(draw, x, y, col_widths[1], row_h, COLOR_BODY_BG, _fmt_int(today_val), num_font, COLOR_TEXT, align="center")
        x += col_widths[1]
        _draw_cell(draw, x, y, col_widths[2], row_h, COLOR_BODY_BG, _fmt_int(week_val), num_font, COLOR_TEXT, align="center")
        x += col_widths[2]

        compare_bg = COLOR_BODY_BG
        compare_color = COLOR_RED
        if delta > 0:
            compare_bg = COLOR_POS_BG
            compare_color = COLOR_RED
        elif delta < 0:
            compare_bg = COLOR_NEG_BG
            compare_color = COLOR_GREEN
        _draw_cell(draw, x, y, col_widths[3], row_h, compare_bg, _fmt_int(delta), num_font, compare_color, align="center")
        y += row_h

    x = 0
    _draw_cell(draw, x, y, col_widths[0], total_h, COLOR_TOTAL_BG, "总计", head_font, COLOR_TEXT, align="center")
    x += col_widths[0]
    _draw_cell(draw, x, y, col_widths[1], total_h, COLOR_TOTAL_BG, _fmt_int(total_today), head_font, COLOR_TEXT, align="center")
    x += col_widths[1]
    _draw_cell(draw, x, y, col_widths[2], total_h, COLOR_TOTAL_BG, _fmt_int(total_week), head_font, COLOR_TEXT, align="center")
    x += col_widths[2]
    _draw_cell(draw, x, y, col_widths[3], total_h, COLOR_TOTAL_BG, _fmt_int(total_delta), head_font, COLOR_TEXT, align="center")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(output_path)


def _render_mobile_table(data: Dict[str, Any], output_path: Path) -> None:
    today_rows = data.get("today_rows") if isinstance(data.get("today_rows"), list) else []
    week_rows = data.get("week_rows") if isinstance(data.get("week_rows"), list) else []
    today_label = _to_date_label(str(data.get("today_date") or ""))
    week_label = _to_date_label(str(data.get("week_date") or ""))
    total_today = int(data.get("total_today") or 0)
    total_week = int(data.get("total_week") or 0)
    total_delta = int(data.get("total_delta") or 0)
    title = str(data.get("title") or "手游付费数据")

    col_widths = [350, 350, 350, 350]
    title_h = 42
    head_h = 34
    row_h = 30
    total_h = 34
    compare_h = 32
    body_rows = max(len(today_rows), len(week_rows))
    width = sum(col_widths) + 1
    height = title_h + head_h + body_rows * row_h + total_h + compare_h + 1

    img = Image.new("RGB", (width, height), (255, 255, 255))
    draw = ImageDraw.Draw(img)

    title_font = _load_font(24, bold=True)
    head_font = _load_font(22, bold=True)
    body_font = _load_font(20, bold=False)
    num_font = _load_font(20, bold=False)

    y = 0
    _draw_cell(draw, 0, y, width - 1, title_h, COLOR_TITLE_BG, title, title_font, COLOR_TEXT, align="center")
    y += title_h

    headers = ["游戏名称", today_label, "游戏名称", week_label]
    x = 0
    for idx, head in enumerate(headers):
        w = col_widths[idx]
        _draw_cell(draw, x, y, w, head_h, COLOR_HEADER_BG, head, head_font, COLOR_TEXT, align="center")
        x += w
    y += head_h

    for idx in range(body_rows):
        left_game = ""
        left_amt = ""
        right_game = ""
        right_amt = ""
        if idx < len(today_rows):
            left_game = str(today_rows[idx].get("game") or "")
            left_amt = _fmt_int(int(today_rows[idx].get("amount") or 0))
        if idx < len(week_rows):
            right_game = str(week_rows[idx].get("game") or "")
            right_amt = _fmt_int(int(week_rows[idx].get("amount") or 0))

        x = 0
        _draw_cell(draw, x, y, col_widths[0], row_h, COLOR_BODY_BG, left_game, body_font, COLOR_TEXT, align="center")
        x += col_widths[0]
        _draw_cell(draw, x, y, col_widths[1], row_h, COLOR_BODY_BG, left_amt, num_font, COLOR_TEXT, align="center")
        x += col_widths[1]
        _draw_cell(draw, x, y, col_widths[2], row_h, COLOR_BODY_BG, right_game, body_font, COLOR_TEXT, align="center")
        x += col_widths[2]
        _draw_cell(draw, x, y, col_widths[3], row_h, COLOR_BODY_BG, right_amt, num_font, COLOR_TEXT, align="center")
        y += row_h

    x = 0
    _draw_cell(draw, x, y, col_widths[0], total_h, COLOR_TOTAL_BG, "合计（元）", head_font, COLOR_TEXT, align="center")
    x += col_widths[0]
    _draw_cell(draw, x, y, col_widths[1], total_h, COLOR_TOTAL_BG, _fmt_int(total_today), head_font, COLOR_TEXT, align="center")
    x += col_widths[1]
    _draw_cell(draw, x, y, col_widths[2], total_h, COLOR_TOTAL_BG, "合计（元）", head_font, COLOR_TEXT, align="center")
    x += col_widths[2]
    _draw_cell(draw, x, y, col_widths[3], total_h, COLOR_TOTAL_BG, _fmt_int(total_week), head_font, COLOR_TEXT, align="center")
    y += total_h

    compare_color = COLOR_RED if total_delta >= 0 else COLOR_GREEN
    x = 0
    _draw_cell(draw, x, y, col_widths[0], compare_h, COLOR_COMPARE_LABEL_BG, "对比", head_font, COLOR_TEXT, align="center")
    x += col_widths[0]
    _draw_cell(draw, x, y, col_widths[1], compare_h, (255, 255, 255), _fmt_int(total_delta), head_font, compare_color, align="center")
    x += col_widths[1]
    _draw_cell(draw, x, y, col_widths[2], compare_h, (255, 255, 255), "", head_font, COLOR_TEXT, align="center")
    x += col_widths[2]
    _draw_cell(draw, x, y, col_widths[3], compare_h, (255, 255, 255), "", head_font, COLOR_TEXT, align="center")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(output_path)


def _draw_cell(
    draw: ImageDraw.ImageDraw,
    x: int,
    y: int,
    w: int,
    h: int,
    fill: tuple[int, int, int],
    text: str,
    font: ImageFont.ImageFont,
    text_color: tuple[int, int, int],
    align: str = "center",
) -> None:
    draw.rectangle([x, y, x + w, y + h], fill=fill, outline=COLOR_BORDER, width=1)
    if not text:
        return
    text = str(text)
    bbox = draw.textbbox((0, 0), text, font=font)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    tx = x + (w - tw) // 2
    if align == "left":
        tx = x + 8
    elif align == "right":
        tx = x + w - tw - 8
    ty = y + (h - th) // 2
    draw.text((tx, ty), text, font=font, fill=text_color)


def _fmt_int(value: int) -> str:
    return str(int(value))


def _to_date_label(value: str) -> str:
    try:
        d = date.fromisoformat(value)
        return f"{d.month}月{d.day}日"
    except ValueError:
        return value or "-"


def _load_font(size: int, bold: bool = False) -> ImageFont.ImageFont:
    candidates = [
        "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/msyhl.ttc",
        "C:/Windows/Fonts/simhei.ttf",
        "C:/Windows/Fonts/simsun.ttc",
        "/System/Library/Fonts/PingFang.ttc",
        "/System/Library/Fonts/STHeiti Medium.ttc",
        "/Library/Fonts/Arial Unicode.ttf",
    ]
    if bold:
        candidates = [
            "C:/Windows/Fonts/msyhbd.ttc",
            "C:/Windows/Fonts/msyh.ttc",
            "C:/Windows/Fonts/simhei.ttf",
            "C:/Windows/Fonts/simsunb.ttf",
            "/System/Library/Fonts/PingFang.ttc",
            "/System/Library/Fonts/STHeiti Medium.ttc",
            "/Library/Fonts/Arial Unicode.ttf",
        ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size=size)
        except OSError:
            continue
    return ImageFont.load_default()
