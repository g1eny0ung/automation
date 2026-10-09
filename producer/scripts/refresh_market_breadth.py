from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
from collections.abc import Sequence

from lib.breadth_trace import RedactedFormatter, RefreshTrace, error_details, summarize


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Refresh a market breadth snapshot in Redis"
    )
    parser.add_argument("market", choices=("en", "zh"))
    parser.add_argument("--events", required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--result", help="Save the confirmed snapshot as JSON")
    args = parser.parse_args(argv)
    handler = logging.StreamHandler()
    handler.setFormatter(RedactedFormatter("%(levelname)s %(name)s %(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)
    trace = RefreshTrace(args.events, args.market, args.revision)
    status = "failure"
    code = 1
    details = {}
    try:
        from server_error import CacheWarmingError

        if args.market == "en":
            from lib.market_breadth_en import refresh_en_market_breadth as refresh
        else:
            from lib.market_breadth_zh import refresh_zh_market_breadth as refresh
        try:
            payload = refresh(trace=trace)
        except CacheWarmingError:
            status = "skipped"
            details = {"reason": "locked"}
            code = 3
        else:
            status = "success"
            code = 0
            encoded = json.dumps(payload, ensure_ascii=False)
            if args.result:
                Path(args.result).write_text(encoded + "\n", encoding="utf-8")
            print(encoded)
    except Exception as exc:
        status = "failure"
        code = 1
        details = error_details(exc)
    finally:
        trace.emit("run_end", status=status, **details)
        trace.close()
        print(json.dumps(summarize(args.events), ensure_ascii=False))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
