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

"""Web-related tools for searching and fetching web content."""

import hashlib
import logging
import os
import time
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# Maximum content length for web tools to prevent overwhelming responses
MAX_WEB_CONTENT_LENGTH = 64 * 1024  # 64KB


class SerperSearch:
    """Serper API search implementation."""

    def __init__(self, timeout: float = 30.0, max_retries: int = 3):
        api_key = os.getenv("SERPER_API_KEY")
        if not api_key:
            raise ValueError("Serper API key is required")
        self.api_key: str = api_key
        self.base_url = "https://google.serper.dev/"
        self.timeout = timeout
        self.max_retries = max_retries
        self.result_key_for_type: dict[str, str] = {
            "news": "news",
            "places": "places",
            "images": "images",
            "search": "organic",
        }

    def search(
        self,
        query: str,
        search_type: str = "search",
        num_results: int = 10,
        proxy_url: str | None = None,
    ) -> list[dict[str, Any]] | str:
        if search_type not in self.result_key_for_type.keys():
            return f"Invalid search type: {search_type}. Serper search type should be one of {self.result_key_for_type.keys()}"

        headers = {
            "X-API-KEY": self.api_key,
            "Content-Type": "application/json",
        }
        payload: dict[str, str | int] = {"q": query, "num": num_results}

        for attempt in range(self.max_retries):
            try:
                with httpx.Client(
                    timeout=httpx.Timeout(
                        connect=self.timeout,
                        read=self.timeout,
                        write=self.timeout,
                        pool=self.timeout,
                    ),
                    proxy=proxy_url,
                ) as client:
                    response = client.post(
                        self.base_url + search_type,
                        headers=headers,
                        json=payload,
                    )
                    response.raise_for_status()

                    data = response.json()
                    results = data.get(
                        self.result_key_for_type[search_type],
                        [],
                    )
                    results = results[:num_results]
                    for result in results:
                        if "imageUrl" in result and result["imageUrl"].startswith(
                            "data:",
                        ):
                            # delete base64 image url
                            del result["imageUrl"]
                    return results

            except httpx.ConnectTimeout as e:
                if attempt == self.max_retries - 1:
                    return f"Connection timeout after {self.max_retries} attempts: {str(e)}"
                time.sleep(2**attempt)  # Exponential backoff
                continue

            except httpx.TimeoutException as e:
                if attempt == self.max_retries - 1:
                    return f"Request timeout after {self.max_retries} attempts: {str(e)}"
                time.sleep(2**attempt)
                continue

            except httpx.HTTPStatusError as e:
                if attempt == self.max_retries - 1:
                    return f"HTTP error {e.response.status_code}: {str(e)}"
                time.sleep(2**attempt)
                continue

            except Exception as e:
                if attempt == self.max_retries - 1:
                    return f"Unexpected error: {str(e)}"
                time.sleep(2**attempt)
                continue

        return f"Failed to complete search after {self.max_retries} attempts"


class HtmlParser:
    """HTML parser for web content extraction."""

    def __init__(self):
        self.base_url: str | None = os.getenv("BP_HTML_PARSER_URL")
        self.api_key: str | None = os.getenv("BP_HTML_PARSER_API_KEY")
        self.secret: str | None = os.getenv("BP_HTML_PARSER_SECRET")

    def parse(self, url: str) -> tuple[bool, str]:
        if not self.base_url or not self.api_key or not self.secret:
            logger.warning("HTML parser configuration is incomplete; skipping parser request")
            return False, ""

        timestamp = str(int(time.time()))
        headers = {
            "X-API-KEY": self.api_key,
            "X-TIMESTAMP": timestamp,
            "X-SIGNATURE": (
                hashlib.sha256(
                    (self.api_key + timestamp + self.secret).encode(),
                ).hexdigest()
            ),
        }
        try:
            with httpx.Client() as client:
                response = client.post(
                    self.base_url,
                    json={"url": url},
                    headers=headers,
                    timeout=30,
                )
        except Exception as e:
            logger.warning(f"Failed to parser {url} with error: {e}")
            return False, ""
        if response.status_code == 200:
            response_data = response.json()
            page_content = response_data.get("content", "")
            return True, page_content
        else:
            logger.warning(
                f"Failed to parser {url} with status code {response.status_code}",
            )
            return False, ""


# Global instances
_serper_search: SerperSearch | None = None
_html_parser: HtmlParser | None = None


def web_search(
    query: str,
    num_results: int = 10,
    search_type: str = "search",
    proxy_url: str | None = None,
) -> dict[str, Any]:
    """
    Search the web using Serper API.

    Args:
        query: Search query string
        num_results: Number of results to return
        search_type: Type of search (search, news, places, images)

    Returns:
        Dict containing search results
    """
    global _serper_search

    try:
        if _serper_search is None:
            _serper_search = SerperSearch()

        results = _serper_search.search(query, search_type, num_results, proxy_url)

        if isinstance(results, str):
            # Error occurred
            return {
                "status": "error",
                "error": results,
                "query": query,
                "search_type": search_type,
            }
        else:
            # Success
            return {
                "status": "success",
                "query": query,
                "search_type": search_type,
                "results": results,
                "total_results": len(results),
            }

    except Exception as e:
        return {
            "status": "error",
            "error": str(e),
            "query": query,
            "search_type": search_type,
            "note": "Make sure SERPER_API_KEY environment variable is set",
        }


def web_read(
    url: str,
    timeout: int = 100,
    use_html_parser: bool = True,
) -> dict[str, Any]:
    """
    Fetch and read content from a web URL.

    Args:
        url: URL to fetch
        timeout: Request timeout in seconds
        use_html_parser: Whether to use HTML parser service

    Returns:
        Dict containing web page content
    """
    global _html_parser

    # Try HTML parser service first if configured
    if use_html_parser:
        try:
            if _html_parser is None:
                _html_parser = HtmlParser()

            if all([_html_parser.base_url, _html_parser.api_key, _html_parser.secret]):
                success, content = _html_parser.parse(url)
                if success:
                    # Truncate content if too long (based on byte size)
                    truncated = False
                    content_bytes = content.encode("utf-8")
                    if len(content_bytes) > MAX_WEB_CONTENT_LENGTH:
                        content = content_bytes[:MAX_WEB_CONTENT_LENGTH].decode("utf-8", errors="ignore") + "..."
                        truncated = True
                    return {
                        "status": "success",
                        "url": url,
                        "content": content,
                        "content_truncated": truncated,
                        "method": "html_parser",
                    }
        except Exception as e:
            logger.warning(f"HTML parser failed for {url}: {e}")

    # Fallback to direct HTTP request
    try:
        user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
        headers = {
            "User-Agent": user_agent,
        }

        with httpx.Client(timeout=timeout) as client:
            response = client.get(url, headers=headers)
            response.raise_for_status()

        content = response.text
        content_type = response.headers.get("content-type", "")

        result: dict[str, Any] = {
            "status": "success",
            "url": url,
            "status_code": response.status_code,
            "content_type": content_type,
            "content_length": len(content),
            "method": "direct_http",
        }

        # Extract text if content is HTML
        if "html" in content_type.lower():
            try:
                from bs4 import BeautifulSoup

                soup = BeautifulSoup(content, "html.parser")

                # Remove script and style elements
                for script in soup(["script", "style"]):
                    script.decompose()

                # Get text
                text = soup.get_text()

                # Clean up text
                lines = (line.strip() for line in text.splitlines())
                chunks = (phrase.strip() for line in lines for phrase in line.split("  "))
                text = " ".join(chunk for chunk in chunks if chunk)

                # Truncate extracted text if too long (based on byte size)
                text_bytes = text.encode("utf-8")
                if len(text_bytes) > MAX_WEB_CONTENT_LENGTH:
                    text = text_bytes[:MAX_WEB_CONTENT_LENGTH].decode("utf-8", errors="ignore") + "..."
                    result["text_truncated"] = True

                result["extracted_text"] = text
                result["title"] = soup.title.string if soup.title else ""

            except ImportError:
                result["note"] = "BeautifulSoup not available. Install with: pip install beautifulsoup4"
            except Exception as e:
                result["text_extraction_error"] = str(e)
        else:
            # Non-HTML (plain text, JSON, etc.): store raw content with truncation
            content_bytes = content.encode("utf-8")
            if len(content_bytes) > MAX_WEB_CONTENT_LENGTH:
                result["content"] = content_bytes[:MAX_WEB_CONTENT_LENGTH].decode("utf-8", errors="ignore") + "..."
                result["content_truncated"] = True
            else:
                result["content"] = content

        return result

    except httpx.TimeoutException:
        return {
            "status": "error",
            "error": f"Request timed out after {timeout} seconds",
            "url": url,
            "error_type": "timeout",
        }

    except httpx.HTTPStatusError as e:
        return {
            "status": "error",
            "error": f"HTTP {e.response.status_code}: {str(e)}",
            "url": url,
            "error_type": "http_error",
            "status_code": e.response.status_code,
        }

    except Exception as e:
        return {
            "status": "error",
            "error": str(e),
            "error_type": type(e).__name__,
            "url": url,
        }
