"""Business core: GIS, map APIs, evidence, validation. No MCP protocol."""

from .analysis import analyze_regions, calculate_geometry, check_api_status, validate_result
from .clients import close_http
from .evidence import search_project_evidence
from .gov_search import prepare_gov_web_search
from .version import SERVER_VERSION

__all__ = [
    "SERVER_VERSION",
    "analyze_regions",
    "calculate_geometry",
    "check_api_status",
    "close_http",
    "prepare_gov_web_search",
    "search_project_evidence",
    "validate_result",
]
