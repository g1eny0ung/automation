from __future__ import annotations

from pydantic import BaseModel, Field


class ErrorCode:
    financial_unsupported_type = 1001
    financial_empty_type = 1002
    financial_year_range_conflict = 1003
    financial_invalid_year_range = 1004
    financial_invalid_request = 1005
    financial_fetch_failed = 1006
    market_cache_warming = 1007
    market_fetch_failed = 1008
    china_market_cache_warming = 1009
    china_market_fetch_failed = 1010
    report_invalid_request = 1011
    api_unauthorized = 1012
    report_not_found = 1013
    report_cache_failed = 1014
    api_auth_not_configured = 1015
    route_not_found = 1017
    method_not_allowed = 1018


class BadRequest(ValueError):
    def __init__(self, message: str, code: int = ErrorCode.financial_invalid_request):
        super().__init__(message)
        self.code = code


class CacheWarmingError(RuntimeError):
    pass


class ErrorResponse(BaseModel):
    code: int = Field(..., description="Error code")
    error: str = Field(..., description="Error message")
