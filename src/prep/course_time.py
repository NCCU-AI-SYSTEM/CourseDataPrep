"""NCCU course time-string parser (e.g. 三234, 五D56).

Copied from CourseLangChain's utils/time.py — the two repos deliberately share no
code. This is the hardened version: unknown characters are skipped, and a weekday
with no periods produces no phantom session. Those behaviours are pinned by
tests/test_regression_fixes.py on the other side, so change both together.
"""
from __future__ import annotations

timeChar = ["A", "B", "1", "2", "3", "4", "C", "D", "5", "6", "7", "8", "E", "F", "G", "H"]
weekdayCode = ["一", "二", "三", "四", "五", "六", "日"]


def getSessionArray(time_str: str) -> list[dict]:
    if time_str == "未定或彈性" or time_str == "" or time_str is None:
        return []
    current_weekday = 1
    current_start_time = 0
    current_end_time = 0
    last_time = 0
    recording = False
    res: list[dict] = []

    def get_start_time(char: str) -> int:
        index = timeChar.index(char)
        time = 6
        if index:
            time += index
        return time

    def get_end_time(char: str) -> int:
        index = timeChar.index(char)
        time = 7
        if index:
            time += index
        return time

    def push_into_res() -> None:
        tmp = {"week_code": -1, "start_time": -1, "end_time": -1, "origin_str": ""}
        tmp["week_code"] = current_weekday + 1
        tmp["start_time"] = get_start_time(timeChar[current_start_time])
        tmp["end_time"] = get_end_time(timeChar[current_end_time])
        origin_str = weekdayCode[current_weekday]
        for i in range(current_start_time, current_end_time + 1):
            origin_str += timeChar[i]
        tmp["origin_str"] = origin_str
        res.append(tmp)

    for char in time_str:
        if char in weekdayCode:
            if recording:
                push_into_res()
                recording = False
            current_weekday = weekdayCode.index(char)
        elif char in timeChar:
            idx = timeChar.index(char)
            if not recording:
                current_start_time = idx
                recording = True
            elif idx != last_time + 1:
                push_into_res()
                current_start_time = idx
            current_end_time = idx
            last_time = idx

    # Only close a run that actually recorded periods — a weekday on its own
    # would otherwise emit a phantom session.
    if recording:
        push_into_res()
    return res


def parse_sessions(time_str: str) -> list[dict]:
    """getSessionArray output mapped to the DB sessions column format."""
    return [
        {
            "weekday": s["week_code"],
            "start_hour": s["start_time"],
            "end_hour": s["end_time"],
        }
        for s in getSessionArray(time_str or "")
    ]
