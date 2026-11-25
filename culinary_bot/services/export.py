"""
Excel-файл для шефа (сценарий А8) — готовая таблица для печати.

Лист 1 «Производство» повторяет бумажную таблицу шефа:

    №  | Блюдо        | Цена | Всего порций | Ева     | Ирина  | ...
       |              |      |              | (центр) | (Котор)|
    01 | Уха 500 г    | 12   | 11           | 1       | 1      |
    ...
       | От шефа, €   |      |              |         | 8      |
       | Итого, €     |      |              | 94      | 42     |
       | Оплачено     |      |              |         |        |   ← шеф отмечает ручкой

Имена клиентов повёрнуты вертикально, как у шефа на бумаге, — так
в ширину листа помещается много клиентов.

Лист 2 «Доставка» — контакты и адреса для развоза.

В клетках записаны готовые ЧИСЛА, а не формулы Excel: таблица собирается
для печати и одинаково выглядит в любой программе и в превью на телефоне.
(openpyxl не умеет сохранять формулу вместе с посчитанным значением —
формулу считает только сам Excel при открытии.)

Файл собирается в памяти и отправляется в Telegram — на диск сервера
не пишется (в нём телефоны и адреса клиентов).
"""

from io import BytesIO

from openpyxl import Workbook
from openpyxl.comments import Comment
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

from culinary_bot.db.models import DeliveryType, Issue, Order
from culinary_bot.services.orders import CustomerSummary, summarize_by_customer

BOLD = Font(bold=True)
BIG_BOLD = Font(bold=True, size=12)
GREY = PatternFill("solid", fgColor="EEEEEE")
THIN = Side(style="thin", color="555555")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
CENTER = Alignment(horizontal="center", vertical="center", wrap_text=True)
LEFT = Alignment(horizontal="left", vertical="center", wrap_text=True)
# textRotation=90 — текст повёрнут снизу вверх, как имена на бумаге шефа.
VERTICAL = Alignment(horizontal="center", vertical="bottom", text_rotation=90, wrap_text=True)
# Клиент с комментарием: жёлтая заливка (на ч/б печати — серая), курсив и звёздочка.
# Звёздочка видна в любом случае — даже на ч/б принтере без заливки.
HAS_COMMENT_FILL = PatternFill("solid", fgColor="FFE699")
HAS_COMMENT_FONT = Font(bold=True, italic=True, underline="single")

FIRST_CLIENT_COL = 5  # колонки: A №, B Блюдо, C Цена, D Всего порций, E... клиенты


def euros(cents: int) -> int | float:
    """Центы → число евро для клетки: 1200 → 12, 1250 → 12.5 (без лишнего «.0»)."""
    return cents // 100 if cents % 100 == 0 else cents / 100


def short_place(summary: CustomerSummary) -> str:
    """Подпись под именем клиента: «самовывоз» или начало адреса."""
    if summary.delivery_type == DeliveryType.PICKUP:
        return "самовывоз"
    address = summary.address or ""
    if summary.map_url:
        address = f"📍 {address}".strip()
    return address if len(address) <= 22 else address[:21] + "…"


def build_issue_workbook(issue: Issue, orders: list[Order]) -> bytes:
    """
    Собрать .xlsx по выпуску. Отменённые заказы не учитываются.
    Возвращает содержимое файла в байтах.
    """
    summaries = summarize_by_customer(orders)
    # Блюда вне лавки, добавленные к заказам этой лавки (решение от 26.11):
    # их нет в issue.items, но готовить/«докинуть» в пакет их надо.
    # {id позиции: (название, порция, цена)} — берём из позиций заказов.
    own_ids = {item.id for item in issue.items}
    extras: dict[int, tuple[str, str | None, int]] = {}
    for order in orders:
        for item in order.items:
            if item.issue_item_id is not None and not item.is_extra and item.issue_item_id not in own_ids:
                extras[item.issue_item_id] = (item.name, item.portion, item.price_cents)
    wb = Workbook()
    _production_sheet(wb.active, issue, summaries, extras)
    _delivery_sheet(wb.create_sheet("Доставка"), summaries)
    buffer = BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


def _production_sheet(
    ws: Worksheet, issue: Issue, summaries: list[CustomerSummary], extras: dict[int, tuple[str, str | None, int]]
) -> None:
    """Лист «Производство»: что готовить и кто сколько платит."""
    ws.title = "Производство"
    last_col = FIRST_CLIENT_COL + len(summaries) - 1

    # --- Шапка ---
    for col, title in enumerate(["№", "Блюдо", "Цена, €", "Всего\nпорций"], start=1):
        cell = ws.cell(row=1, column=col, value=title)
        cell.font, cell.alignment, cell.fill = BOLD, CENTER, GREY
    for offset, s in enumerate(summaries):
        star = " *" if s.comments else ""
        # Заказ другому получателю: «Ева → Маша 🎁» — стоит сразу после колонки Евы.
        name = f"{s.orderer_name} → {s.name} 🎁" if s.recipient_id else s.name
        cell = ws.cell(row=1, column=FIRST_CLIENT_COL + offset, value=f"{name}{star}\n{short_place(s)}")
        cell.font, cell.alignment, cell.fill = BOLD, VERTICAL, GREY
        if s.comments:
            # У клиента есть комментарий: выделяем колонку, чтобы шеф заметил его
            # и на распечатке. Сам текст — всплывающей заметкой в Excel
            # и полностью на листе «Доставка».
            cell.font, cell.fill = HAS_COMMENT_FONT, HAS_COMMENT_FILL
            cell.comment = Comment("\n".join(s.comments), "бот")
    ws.row_dimensions[1].height = 120  # высота под вертикальные имена

    # --- Блюда ---
    row = 2
    for item in issue.items:
        per_customer = [s.menu_quantities.get(item.id, 0) for s in summaries]
        total = sum(per_customer)
        if total == 0 and not item.is_active:
            continue  # снятое с продажи и никем не заказанное — не печатаем
        name = f"{item.name} {item.portion}" if item.portion else item.name
        ws.cell(row=row, column=1, value=f"{item.position:02d}").alignment = CENTER
        ws.cell(row=row, column=2, value=name).alignment = LEFT
        ws.cell(row=row, column=3, value=euros(item.price_cents)).alignment = CENTER
        total_cell = ws.cell(row=row, column=4, value=total)
        total_cell.font, total_cell.alignment = BIG_BOLD, CENTER
        for offset, qty in enumerate(per_customer):
            # 0 оставляем пустой клеткой — так таблицу легче читать глазами.
            cell = ws.cell(row=row, column=FIRST_CLIENT_COL + offset, value=qty or None)
            cell.alignment, cell.font = CENTER, BIG_BOLD
        ws.row_dimensions[row].height = 28
        row += 1

    # --- Блюда вне лавки, добавленные к заказам («🧺 докинуть») ---
    for item_id, (name, portion, price) in extras.items():
        per_customer = [s.menu_quantities.get(item_id, 0) for s in summaries]
        title = f"🧺 {name} {portion}" if portion else f"🧺 {name}"
        ws.cell(row=row, column=1, value="🧺").alignment = CENTER
        ws.cell(row=row, column=2, value=title).alignment = LEFT
        ws.cell(row=row, column=3, value=euros(price)).alignment = CENTER
        total_cell = ws.cell(row=row, column=4, value=sum(per_customer))
        total_cell.font, total_cell.alignment = BIG_BOLD, CENTER
        for offset, qty in enumerate(per_customer):
            cell = ws.cell(row=row, column=FIRST_CLIENT_COL + offset, value=qty or None)
            cell.alignment, cell.font = CENTER, BIG_BOLD
        ws.row_dimensions[row].height = 28
        row += 1

    # --- Позиции от шефа (если есть) ---
    extras = [sum(price * qty for (_, _, price, is_extra), qty in s.lines.items() if is_extra) for s in summaries]
    if any(extras):
        ws.cell(row=row, column=2, value="От шефа, €").alignment = LEFT
        for offset, cents in enumerate(extras):
            ws.cell(row=row, column=FIRST_CLIENT_COL + offset, value=euros(cents) if cents else None).alignment = CENTER
        row += 1

    # --- Итого к оплате ---
    ws.cell(row=row, column=2, value="Итого к оплате, €").font = BOLD
    for offset, s in enumerate(summaries):
        cell = ws.cell(row=row, column=FIRST_CLIENT_COL + offset, value=euros(s.total_cents))
        cell.font, cell.alignment, cell.fill = BIG_BOLD, CENTER, GREY
    row += 1

    # --- Оплачено (пустая строка — для отметок ручкой) ---
    ws.cell(row=row, column=2, value="Оплачено").font = BOLD
    ws.row_dimensions[row].height = 28
    table_end = row

    # Пояснение под таблицей (вне рамок), если есть клиенты с комментариями.
    if any(s.comments for s in summaries):
        note = ws.cell(row=row + 2, column=2, value="* — у клиента есть комментарий к заказу, см. лист «Доставка»")
        note.font = Font(italic=True)

    # Рамки для всей таблицы.
    for r in ws.iter_rows(min_row=1, max_row=table_end, min_col=1, max_col=max(4, last_col)):
        for cell in r:
            cell.border = BORDER

    ws.column_dimensions["A"].width = 4
    ws.column_dimensions["B"].width = 30
    ws.column_dimensions["C"].width = 7
    ws.column_dimensions["D"].width = 9
    for offset in range(len(summaries)):
        ws.column_dimensions[get_column_letter(FIRST_CLIENT_COL + offset)].width = 6
    _print_setup(ws)


def _delivery_sheet(ws: Worksheet, summaries: list[CustomerSummary]) -> None:
    """Лист «Доставка»: одна строка на клиента — всё, что нужно при развозе."""
    headers = ["Заказы", "Имя", "Телефон", "Telegram", "Получение", "Адрес", "Карта", "Позиции", "К оплате, €", "Оплата", "Комментарий"]
    ws.append(headers)
    for cell in ws[1]:
        cell.font, cell.fill, cell.alignment = BOLD, GREY, CENTER
    for s in summaries:
        name = f"{s.name} 🎁 (от {s.orderer_name})" if s.recipient_id else s.name
        if not s.recipient_id:
            payment = ""
        elif s.payer == "recipient":
            payment = "получатель"
        else:
            payment = f"заказчик ({s.orderer_name})"
        lines = "\n".join(
            f"{name}{' (от шефа)' if extra else ''} × {qty}" for (name, _portion, _price, extra), qty in s.lines.items()
        )
        ws.append(
            [
                ", ".join(f"№{n}" for n in s.order_ids),
                name,
                s.phone,
                f"@{s.username}" if s.username else "",
                "самовывоз" if s.delivery_type == DeliveryType.PICKUP else "доставка",
                s.address or "",
                "📍 открыть" if s.map_url else "",
                lines,
                euros(s.total_cents),
                payment,
                "\n".join(s.comments),
            ]
        )
        if s.map_url:
            # Клетка-ссылка: нажал — открылась точка в Google Maps.
            map_cell = ws.cell(row=ws.max_row, column=7)
            map_cell.hyperlink = s.map_url
            map_cell.font = Font(color="0563C1", underline="single")
    for r in ws.iter_rows(min_row=2):
        for cell in r:
            cell.alignment = Alignment(vertical="top", wrap_text=True)
    for r in ws.iter_rows():
        for cell in r:
            cell.border = BORDER
    for col, width in enumerate([10, 18, 16, 14, 11, 30, 10, 30, 9, 14, 30], start=1):
        ws.column_dimensions[get_column_letter(col)].width = width
    _print_setup(ws)


def _print_setup(ws: Worksheet) -> None:
    """Печать: альбомный A4, всё по ширине одного листа, шапка повторяется на каждой странице."""
    ws.page_setup.orientation = "landscape"
    ws.page_setup.paperSize = ws.PAPERSIZE_A4
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0  # 0 — высота не ограничена (сколько понадобится страниц)
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.print_title_rows = "1:1"
    ws.page_margins.left = ws.page_margins.right = 0.4
    ws.freeze_panes = "E2" if ws.title == "Производство" else "B2"
