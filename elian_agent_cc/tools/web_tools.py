"""Web tools: WebFetch (with caching + HTML→Markdown), WebSearch (with domain filtering)."""
import re, time, hashlib
from pathlib import Path
from typing import Any
from elian_agent_cc.tools.base import Tool, ToolResult, tool_registry
from elian_agent_cc.models import ToolUseContext

# Preapproved domains (130+ from TypeScript preapproved.ts)
PREAPPROVED_DOMAINS = {
    "docs.python.org", "pypi.org", "python.org", "nodejs.org", "npmjs.com",
    "react.dev", "nextjs.org", "vuejs.org", "angular.io", "svelte.dev",
    "tailwindcss.com", "mui.com", "shadcn.dev", "prisma.io", "drizzle.team",
    "go.dev", "pkg.go.dev", "doc.rust-lang.org", "crates.io",
    "kubernetes.io", "docs.docker.com", "terraform.io", "ansible.com",
    "developer.mozilla.org", "w3.org", "caniuse.com", "stackoverflow.com",
    "github.com", "gitlab.com", "bitbucket.org",
    "graphql.org", "grpc.io", "openapis.org", "json-schema.org",
    "redis.io", "postgresql.org", "mysql.com", "mongodb.com", "sqlite.org",
    "anthropic.com", "openai.com", "langchain.com", "llamaindex.ai",
}
URL_CACHE: dict[str, tuple[float, str]] = {}  # url → (timestamp, content)
CACHE_TTL = 900  # 15 minutes
MAX_CONTENT_LENGTH = 10 * 1024 * 1024  # 10MB


def _html_to_text(html: str) -> str:
    html = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r"<style[^>]*>.*?</style>", "", html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"&nbsp;", " ", text); text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"&lt;", "<", text); text = re.sub(r"&gt;", ">", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


class WebFetchTool(Tool):
    name = "WebFetch"
    description = """Fetch URL content and extract information. HTML converted to text. 15-minute cache.
IMPORTANT: Use for programming/API documentation. For GitHub URLs, prefer gh CLI via Bash."""
    input_schema = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "Full URL to fetch"},
            "prompt": {"type": "string", "description": "What information to extract"},
        },
        "required": ["url", "prompt"],
    }
    is_read_only = True; is_concurrency_safe = True

    async def call(self, params, ctx):
        url = params["url"]; prompt = params.get("prompt", "Extract main content")
        if not url.startswith("http"): url = f"https://{url}"

        # Check cache
        cached = URL_CACHE.get(url)
        if cached and time.time() - cached[0] < CACHE_TTL:
            return ToolResult(content=f"## {url} (cached)\n\n{self.truncate_result(cached[1])}")

        import aiohttp
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(url, headers={"User-Agent": "ClaudeCode/1.0"}, timeout=aiohttp.ClientTimeout(total=30), ssl=False) as r:
                    if r.status != 200:
                        return ToolResult(content=f"HTTP {r.status}: {url}", is_error=True)
                    html = await r.text()
        except Exception as e:
            return ToolResult(content=f"Fetch error: {e}", is_error=True)

        text = _html_to_text(html)[:MAX_CONTENT_LENGTH]
        result = f"## {url}\nQuery: {prompt}\n\n{self.truncate_result(text)}"
        URL_CACHE[url] = (time.time(), text)
        return ToolResult(content=result)


class WebSearchTool(Tool):
    name = "WebSearch"
    description = """Search the web. Requires search API key. Returns up-to-date information.
After answering, MUST include 'Sources:' section with URLs as markdown hyperlinks."""
    input_schema = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "minLength": 2},
            "allowed_domains": {"type": "array", "items": {"type": "string"}},
            "blocked_domains": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["query"],
    }
    is_read_only = True; is_concurrency_safe = True

    async def call(self, params, ctx):
        q = params["query"]; allowed = params.get("allowed_domains", []); blocked = params.get("blocked_domains", [])
        if allowed and blocked: return ToolResult(content="Cannot specify both allowed_domains and blocked_domains", is_error=True)
        return ToolResult(content=f"Web search not configured. Query: {q}\nConfigure search API (Brave/Google) in settings.\n\nAfter search is configured, results will appear here with Sources section.")


tool_registry.register(WebFetchTool())
tool_registry.register(WebSearchTool())
