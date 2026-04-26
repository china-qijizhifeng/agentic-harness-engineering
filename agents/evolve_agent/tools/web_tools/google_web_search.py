# Copyright (c) Nex-AGI. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
google_web_search - Wraps web_tool.web_search, output as gemini-cli format.
"""

from typing import Any

from .web_tool import web_search as _web_search


def _results_to_llm_content(query: str, results: list[dict[str, Any]]) -> str:
    """Format Serper results for LLM (gemini-cli style)."""
    lines: list[str] = []
    for idx, item in enumerate(results, 1):
        title = item.get("title", "Untitled")
        link = item.get("link", item.get("url", "No URL"))
        lines.append(f"[{idx}] {title} ({link})")
        if "snippet" in item and item["snippet"]:
            lines.append(f"    {item['snippet']}")
    return "\n".join(lines) if lines else "No results found."


def google_web_search(
    query: str,
    num_results: int = 10,
    search_type: str = "search",
    proxy_url: str | None = None,
) -> dict[str, Any]:
    """
    Search using web_tool.web_search (Serper API).
    Returns gemini-cli format: content, returnDisplay.
    """
    try:
        if not query or not query.strip():
            return {
                "content": "The 'query' parameter cannot be empty.",
                "returnDisplay": "Error: Empty search query.",
                "error": {
                    "message": "The 'query' parameter cannot be empty.",
                    "type": "INVALID_QUERY",
                },
            }

        raw = _web_search(
            query=query,
            num_results=num_results,
            search_type=search_type,
            proxy_url=proxy_url,
        )

        if raw.get("status") == "error":
            error_msg = raw.get("error", "Unknown error")
            return {
                "content": f"Error: {error_msg}",
                "returnDisplay": "Error performing web search.",
                "error": {"message": str(error_msg), "type": "WEB_SEARCH_FAILED"},
            }

        results = raw.get("results", [])
        formatted = _results_to_llm_content(query, results)
        return {
            "content": f'Web search results for "{query}":\n\n{formatted}',
            "returnDisplay": f'Search results for "{query}" returned ({len(results)} results).',
            "sources": results,
        }

    except Exception as e:
        error_msg = str(e)
        return {
            "content": f"Error: {error_msg}",
            "returnDisplay": "Error performing web search.",
            "error": {"message": error_msg, "type": "WEB_SEARCH_FAILED"},
        }
