"""
Excel-файл для шефа: таблица производства и лист доставки (сценарий А8).

Файл собирается в памяти (BytesIO) и отправляется в Telegram документом —
на диск сервера ничего не пишется (там были бы телефоны и адреса клиентов).

Формат пока стандартный; когда будет пример таблицы шефа — подгоняем здесь.
"""

from io import BytesIO

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

from culinary_bot.db.models import DeliveryType, Issue, Order
from culinary_bot.services.orders import summarize_by_customer

BOLD = Font(bold=True)
HEADER_FILL = PatternFill("solid", fgColor="EEEEEE")
THIN = Side(style="thin", color="999999")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
WRAP = Alignment(wrap_text=True, vertical="top")


def build_issue_workbook(issue: Issue, orders: list[Order]) -> bytes:
    """
    Собрать .xlsx по выпуску. Отменённые заказы не учитываются.

    Возвращает содержимое файла в байтах.
    """
    summaries = summarize_by_customer(orders)
    wb = Workbook()

    # ---------------- Лист 1: Производство ----------------
    # Блюда по строкам, клиенты по столбцам, итог — в последнем столбце.
    ws = wb.active
    ws.title = "Производство"
    header = ["№", "Блюдо", "Порция"] + [s.name for s in summaries] + ["Итого"]
    ws.append(header)
    for item in issue.items:
        per_customer = [s.menu_quantities.get(item.id, 0) for s in summaries]
        total = sum(per_customer)
        if total == 0 and not item.is_active:
            continue  # снятое с продажи и никем не заказанное — не нужно в таблице
        # 0 показываем пустой клеткой — так таблицу легче читать глазами.
        ws.append([f"{item.position:02d}", item.name, item.portion or ""] + [q or None for q in per_customer] + [total])
    totals = [sum(s.menu_quantities.values()) for s in summaries]
    ws.append(["", "Всего порций", ""] + totals + [sum(totals)])
    _style_table(ws, wide_columns={2: 34, 3: 12})
    for cell in ws[ws.max_row]:
        cell.font = BOLD

    # ---------------- Лист 2: Доставка ----------------
    ws2 = wb.create_sheet("Доставка")
    ws2.append(["Заказы", "Имя", "Телефон", "Telegram", "Получение", "Адрес", "Позиции", "Сумма, €", "Комментарий"])
    for s in summaries:
        lines = "\n".join(
            f"{name}{' (от шефа)' if extra else ''} × {qty}" for (name, _portion, _price, extra), qty in s.lines.items()
        )
        ws2.append(
            [
                ", ".join(f"№{n}" for n in s.order_ids),
                s.name,
                s.phone,
                f"@{s.username}" if s.username else "",
                "самовывоз" if s.delivery_type == DeliveryType.PICKUP else "доставка",
                s.address or "",
                lines,
                s.total_cents / 100,
                "\n".join(s.comments),
            ]
        )
    _style_table(ws2, wide_columns={2: 16, 3: 16, 4: 14, 5: 11, 6: 30, 7: 34, 8: 9, 9: 30})

    buffer = BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


def _style_table(ws: Worksheet, wide_columns: dict[int, int]) -> None:
    """
    Оформить лист для печати: жирная шапка, рамки, перенос текста,
    альбомная ориентация, «вписать по ширине страницы».
    """
    for cell in ws[1]:
        cell.font = BOLD
        cell.fill = HEADER_FILL
    for row in ws.iter_rows():
        for cell in row:
            cell.border = BORDER
            cell.alignment = WRAP
    for col in range(1, ws.max_column + 1):
        ws.column_dimensions[get_column_letter(col)].width = wide_columns.get(col, 10)
    ws.freeze_panes = "B2"  # шапка и первый столбец не уезжают при прокрутке
    ws.page_setup.orientation = "landscape"
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.print_options.gridLines = True
