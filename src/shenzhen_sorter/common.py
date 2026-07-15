"""Small helpers shared across pipeline.py, receipt.py, and integrity.py -
previously duplicated three times each; consolidated here."""

import html

MONTH_NAMES = ["01-January", "02-February", "03-March", "04-April", "05-May", "06-June",
               "07-July", "08-August", "09-September", "10-October", "11-November", "12-December"]


def esc(s) -> str:
    return html.escape(str(s if s is not None else ""))


def month_folder_name(month: int) -> str:
    return MONTH_NAMES[month - 1]
