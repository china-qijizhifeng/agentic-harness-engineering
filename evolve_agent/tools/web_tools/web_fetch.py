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
web_fetch - Wraps web_tool.web_read, output as gemini-cli format.
"""

from typing import Any
from urllib.parse import urlparse

from .web_tool import web_read as _web_read


def _parse_url_from_prompt(text: str) -> tuple[list[str], list[str]]:
    """Extract valid http(s) URLs from prompt text."""
    tokens = text.split()
    valid_urls: list[str] = []
    errors: list[str] = []
    for token in tokens:
        if not token or "://" not in token:
            continue
        try:
            parsed = urlparse(token)
            if parsed.scheme in ("http", "https"):
                valid_urls.append(token)
            else:
                errors.append(f'Unsupported protocol: "{token}". Only http and https supported.')
        except Exception:
            errors.append(f'Malformed URL: "{token}".')
    return valid_urls, errors


def _convert_github_url(url: str) -> str:
    """Convert GitHub blob URL to raw URL."""
    if "github.com" in url and "/blob/" in url:
        return url.replace("github.com", "raw.githubusercontent.com").replace("/blob/", "/")
    return url


def web_fetch(
    prompt: str | None = None,
    url: str | None = None,
    timeout: int = 100,
    use_html_parser: bool = True,
) -> dict[str, Any]:
    """
    Fetch content using web_tool.web_read (HtmlParser + direct HTTP).
    Returns gemini-cli format: content, returnDisplay.
    """
    try:
        target_url: str | None = None
        prompt_text = prompt or ""

        if not url and not (prompt or "").strip():
            return {
                "content": "Prompt or URL is required and must be non-empty.",
                "returnDisplay": "Error: Empty prompt.",
                "error": {
                    "message": "Prompt or URL is required and must be non-empty.",
                    "type": "INVALID_PROMPT",
                },
            }

        if url and url.strip():
            target_url = url.strip()
        elif prompt and prompt.strip():
            valid_urls, errors = _parse_url_from_prompt(prompt)
            if errors:
                msg = "Error(s) in prompt URLs:\n- " + "\n- ".join(errors)
                return {
                    "content": msg,
                    "returnDisplay": "Error: Invalid URLs.",
                    "error": {"message": msg, "type": "INVALID_URL"},
                }
            if valid_urls:
                target_url = valid_urls[0]
                prompt_text = prompt

        if not target_url:
            return {
                "content": "No valid URL provided. Use 'url' or 'prompt' with http(s) URL.",
                "returnDisplay": "Error: No valid URL.",
                "error": {"message": "No valid URL.", "type": "NO_URLS_FOUND"},
            }

        target_url = _convert_github_url(target_url)

        raw = _web_read(
            url=target_url,
            timeout=timeout,
            use_html_parser=use_html_parser,
        )

        if raw.get("status") == "error":
            error_msg = raw.get("error", "Unknown error")
            return {
                "content": f"Error: {error_msg}",
                "returnDisplay": f"Error: {error_msg}",
                "error": {
                    "message": str(error_msg),
                    "type": raw.get("error_type", "WEB_FETCH_ERROR"),
                },
            }

        content = raw.get("content") or raw.get("extracted_text", "")
        if not content and "note" in raw:
            content = raw["note"]
        if not content:
            content = "(No content extracted)"

        req_note = ""
        if prompt_text and prompt_text.strip():
            preview = prompt_text[:80] + "..." if len(prompt_text) > 80 else prompt_text
            req_note = f' Please use it to respond to: "{preview}"'
        else:
            req_note = " Please use it to respond to the original request."

        llm_content = f"""Content fetched from {target_url}:

---
{content}
---

This content was fetched from the URL.{req_note}
"""

        return {
            "content": llm_content,
            "returnDisplay": f"Content for {target_url} processed.",
        }

    except Exception as e:
        full = str(url or prompt or "")
        url_preview = full[:50] + "..." if len(full) > 50 else full
        error_msg = f'Error processing web content for "{url_preview}": {str(e)}'
        return {
            "content": f"Error: {error_msg}",
            "returnDisplay": f"Error: {error_msg}",
            "error": {
                "message": error_msg,
                "type": "WEB_FETCH_PROCESSING_ERROR",
            },
        }
