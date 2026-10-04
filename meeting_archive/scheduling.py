"""Local weekly windows gate automatic job starts, without waking Windows."""
import re
from datetime import datetime, timedelta


def default_schedule():
    return {"mode": "immediate", "days": list(range(7)), "start": "22:00", "end": "06:00"}


def validate_schedule(value):
    if not isinstance(value, dict) or set(value) != {"mode", "days", "start", "end"}:
        raise ValueError("Расписание должно содержать режим, дни и начало/конец окна")
    if value["mode"] not in {"immediate", "window"}:
        raise ValueError("Выберите немедленный запуск или расписание")
    days = value["days"]
    if not isinstance(days, list) or not days or any(type(day) is not int or not 0 <= day <= 6 for day in days):
        raise ValueError("Выберите хотя бы один день недели")
    for key in ("start", "end"):
        if not isinstance(value[key], str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value[key]):
            raise ValueError("Укажите время в формате ЧЧ:ММ")
    return {**value, "days": sorted(set(days))}


def window_status(schedule, at=None):
    schedule = validate_schedule(schedule)
    now = at if at is not None else datetime.now().astimezone()
    if schedule["mode"] == "immediate":
        return {"allowed": True, "next_at": "", "label": "Сразу после появления материалов", "timezone": "local"}
    start_hour, start_minute = map(int, schedule["start"].split(":"))
    end_hour, end_minute = map(int, schedule["end"].split(":"))
    next_at = None
    for shift in range(-1, 9):
        day = now.date() + timedelta(days=shift)
        if day.weekday() not in schedule["days"]:
            continue
        start = datetime.combine(day, datetime.min.time()).replace(hour=start_hour, minute=start_minute)
        end = datetime.combine(day, datetime.min.time()).replace(hour=end_hour, minute=end_minute)
        if end <= start:
            end += timedelta(days=1)
        # Native local conversion evaluates Windows timezone rules for each date.
        if at is None:
            start, end = start.astimezone(), end.astimezone()
        else:
            start, end = start.replace(tzinfo=now.tzinfo), end.replace(tzinfo=now.tzinfo)
        if start <= now < end:
            return {"allowed": True, "next_at": start.isoformat(), "ends_at": end.isoformat(),
                    "label": "Окно запуска открыто до " + end.strftime("%H:%M"), "timezone": "local"}
        if start > now and (next_at is None or start < next_at):
            next_at = start
    return {"allowed": False, "next_at": next_at.isoformat(),
            "label": "Ожидает расписания: " + next_at.strftime("%d.%m %H:%M"), "timezone": "local"}
