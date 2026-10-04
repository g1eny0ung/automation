import argparse
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import StrEnum
import json
import sys
from types import MappingProxyType
from typing import Mapping
from zoneinfo import ZoneInfo

import exchange_calendars
import pandas as pd


class Market(StrEnum):
    US = "en"
    CHINA = "zh"


class RelativeDate(StrEnum):
    TODAY = "today"
    YESTERDAY = "yesterday"


CALENDARS: Mapping[Market, str] = MappingProxyType({
    Market.US: "XNYS",
    Market.CHINA: "XSHG",
})
BEIJING = ZoneInfo("Asia/Shanghai")


class SessionStatus(StrEnum):
    OPEN = "open"
    CLOSED = "closed"


@dataclass(frozen=True)
class SessionDecision:
    market: Market
    date: date
    status: SessionStatus


def evaluate(market: Market, target_date: date) -> SessionDecision:
    calendar = exchange_calendars.get_calendar(
        CALENDARS[market],
        start=f"{target_date.year}-01-01",
        end=f"{target_date.year}-12-31",
    )
    status = (
        SessionStatus.OPEN
        if pd.Timestamp(target_date) in calendar.sessions
        else SessionStatus.CLOSED
    )
    return SessionDecision(market, target_date, status)


def main() -> int:
    parser = argparse.ArgumentParser(description="Check the selected markets' target exchange sessions.")
    parser.add_argument("markets", help="Comma-separated market codes: en, zh, or en,zh.")
    parser.add_argument("--date", type=RelativeDate, choices=RelativeDate, required=True,
                        help="Target date relative to the current Beijing date.")
    parser.add_argument("--now", type=datetime.fromisoformat, default=None)
    parser.add_argument("--github-output", help="Append should_run to a GitHub Actions output file.")
    args = parser.parse_args()
    try:
        markets = tuple(Market(market) for market in args.markets.split(","))
        now = args.now or datetime.now(BEIJING)
        if now.utcoffset() is None:
            raise ValueError("now must include a timezone")
        target_date = now.astimezone(BEIJING).date()
        if args.date == RelativeDate.YESTERDAY:
            target_date -= timedelta(days=1)
        decisions = tuple(evaluate(market, target_date) for market in markets)
        payload = [
            {"market": decision.market, "date": decision.date.isoformat(), "status": decision.status}
            for decision in decisions
        ]
        diagnostic = json.dumps(payload[0] if len(payload) == 1 else payload)
        if args.github_output is not None:
            should_run = any(decision.status == SessionStatus.OPEN for decision in decisions)
            with open(args.github_output, "a") as output:
                output.write(f"should_run={str(should_run).lower()}\n")
    except Exception as error:
        print(f"Market calendar check failed: {error}", file=sys.stderr)
        return 1
    print(diagnostic)
    return 0


if __name__ == "__main__":
    sys.exit(main())
