from datetime import datetime


def _kot_beep_raw():
    """Best-effort loud buzzer sequence for ESC/POS printers.

    Uses multiple common buzzer triggers so printers that support beeping are
    more likely to sound an alert. Unsupported printers usually ignore these.
    """
    esc = b"\x1b"
    # Rongta RP3xx/RP32x command set includes: ESC B n t (beep prompt).
    # Keep it to exactly 2 beeps for kitchen KOT printing.
    rongta = esc + b"B" + bytes([2, 9])
    return rongta


def build_kot_raw(order_id, location, items, reprint=False, kot_comment=None, token=None, kot_printed_at=None):
    """Build ESC/POS raw bytes for a KOT ticket matching the browser KOT format.

    If kot_printed_at is provided, that timestamp is used for the date/time on
    the ticket (useful for reprints). Otherwise the current time is used.
    """
    esc = b"\x1b"
    gs = b"\x1d"
    if kot_printed_at is not None:
        if isinstance(kot_printed_at, str):
            try:
                kot_printed_at = datetime.fromisoformat(kot_printed_at.replace("Z", "+00:00"))
            except Exception:
                kot_printed_at = datetime.now()
        elif not isinstance(kot_printed_at, datetime):
            kot_printed_at = datetime.now()
    else:
        kot_printed_at = datetime.now()
    if kot_printed_at.tzinfo is not None:
        kot_printed_at = kot_printed_at.astimezone()
    months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    date_str = f"{kot_printed_at.day}-{months[kot_printed_at.month - 1]}-{kot_printed_at.year}"
    hour = kot_printed_at.hour % 12 or 12
    ampm = "PM" if kot_printed_at.hour >= 12 else "AM"
    time_str = f"{hour}:{kot_printed_at.minute:02d} {ampm}"
    solid = b"=" * 48 + b"\n"
    dash = b"-" * 48 + b"\n"

    out = []
    out.append(esc + b"@")
    out.append(_kot_beep_raw())
    out.append(esc + b"@")
    out.append(solid)
    out.append(esc + b"a\x01")
    out.append(esc + b"E\x01")
    out.append(b"KOT\n")
    if reprint:
        out.append(b"** REPRINT **\n")
    out.append(esc + b"E\x00")
    out.append(gs + b"!\x11")
    out.append((f"* {location} *\n").encode("cp437", errors="replace"))
    out.append(gs + b"!\x00")
    out.append(esc + b"a\x00")
    out.append(solid)
    out.append((f"KOT #{order_id} | {date_str} | {time_str}\n").encode("cp437", errors="replace"))
    if kot_comment:
        out.append(dash)
        out.append(esc + b"a\x01")      # Center align
        out.append(esc + b"M\x01")      # Font B
        out.append(gs + b"!\x11")       # Double height/width (if desired)
        out.append((f"* {kot_comment} *\n").encode("cp437", errors="replace"))
        out.append(gs + b"!\x00")       # Normal size
        out.append(esc + b"M\x00")      # Back to Font A
        out.append(esc + b"a\x00")      # Left align
    out.append(esc + b"M\x00")
    out.append(esc + b"E\x00")
    out.append(gs + b"!\x00")
    out.append(dash)
    out.append(b"QTY  ITEM\n")
    out.append(dash)
    for idx, item in enumerate(items or []):
        name = str(item.get("name", ""))
        qty = item.get("qty") if item.get("qty") is not None else item.get("quantity", 1)
        config = str(item.get("config", ""))
        notes = str(item.get("notes", ""))

        if idx > 0:
            out.append(esc + b"J\x1c")

        out.append(esc + b"M\x01")
        out.append(gs + b"!\x11")
        out.append((f"{qty}x {name}\n").encode("cp437", errors="replace"))
        out.append(gs + b"!\x00")
        out.append(esc + b"M\x00")

        if config:
            out.append(esc + b"J\x10")
            out.append((f"      [{config}]\n").encode("cp437", errors="replace"))
        if notes:
            out.append(esc + b"J\x10")
            note_text = notes.upper()
            if "JAIN" in note_text:
                out.append((f"      *** JAIN {note_text.replace('JAIN', '').strip()}\n").encode("cp437", errors="replace"))
            else:
                out.append((f"      {note_text}\n").encode("cp437", errors="replace"))

    out.append(dash)
    out.append(esc + b"a\x01")
    # Get identifier = ""
    if token:
        short = str(token)[-4:] if len(str(token)) > 4 else str(token)
        identifier = f"Parcel {short}"
    else:
        # Try to get table number from location (location is like "Table X" etc.)
        identifier = location
    out.append((f"END OF KOT - {identifier}\n").encode("cp437", errors="replace"))
    out.append(esc + b"a\x00")
    out.append(b"\n\n\n")
    out.append(gs + b"V\x01")
    return b"".join(out)
