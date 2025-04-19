import httpx
import json
from typing import Any, Dict, Optional

from mcp_agent.mcp.base_client import BaseMcpClient
from mcp_agent.mcp.mcp_server_config import McpServerConfig
from mcp_agent.mcp.tool_execution_result import ToolExecutionResult


class HttpPostClient(BaseMcpClient):
    """
    MCP Client for servers that expect simple HTTP POST requests with JSON payloads,
    like those exposed by MCPO (https://github.com/open-webui/mcpo).
    """

    def __init__(self, server_name: str, config: McpServerConfig):
        super().__init__(server_name, config)
        if not config.url:
            raise ValueError(f"URL must be configured for http_post transport on server '{server_name}'")
        self._url = config.url
        self._client = httpx.AsyncClient(timeout=config.timeout) # Use configured timeout

    async def connect(self) -> None:
        # No explicit connect step needed for simple HTTP POST
        # We can potentially add a HEAD or OPTIONS request here for validation if needed
        self._logger.info(f"[{self._server_name}] HTTP POST client initialized for URL: {self._url}")
        pass

    async def disconnect(self) -> None:
        await self._client.aclose()
        self._logger.info(f"[{self._server_name}] HTTP POST client disconnected.")

    async def call_tool(
        self,
        tool_name: str,
        params: Optional[Dict[str, Any]],
        correlation_id: Optional[str] = None, # Added correlation_id for potential future use
    ) -> ToolExecutionResult:
        """
        Calls a tool on the remote server via HTTP POST.

        Assumes the server expects the parameters directly as the JSON body.
        MCPO servers typically map the tool name implicitly via the URL path.
        """
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            # Add API key header if configured (using a standard like Authorization: Bearer)
            # This needs alignment with how MCPO expects the API key, if used.
            # "Authorization": f"Bearer {self._config.api_key}" # Example
        }
        if correlation_id:
             headers["X-Correlation-ID"] = correlation_id # Example header

        json_payload = params if params else {}

        self._logger.debug(f"[{self._server_name}] Calling tool '{tool_name}' at {self._url} with params: {json_payload}")

        try:
            response = await self._client.post(
                self._url, # MCPO uses the path for the tool endpoint
                json=json_payload,
                headers=headers,
            )
            response.raise_for_status()  # Raise an exception for bad status codes (4xx or 5xx)

            # Attempt to parse JSON, handle potential errors
            try:
                result_data = response.json()
                self._logger.debug(f"[{self._server_name}] Received response: {result_data}")
                # Assuming the response body directly contains the tool result
                return ToolExecutionResult(success=True, result=result_data)
            except json.JSONDecodeError:
                self._logger.error(f"[{self._server_name}] Failed to decode JSON response: {response.text}")
                return ToolExecutionResult(success=False, error="Failed to decode JSON response", stderr=response.text)

        except httpx.RequestError as e:
            self._logger.error(f"[{self._server_name}] HTTP request error calling tool '{tool_name}': {e}")
            return ToolExecutionResult(success=False, error=f"HTTP request error: {e}")
        except httpx.HTTPStatusError as e:
            self._logger.error(f"[{self._server_name}] HTTP status error calling tool '{tool_name}': {e.response.status_code} - {e.response.text}")
            return ToolExecutionResult(success=False, error=f"HTTP status error: {e.response.status_code}", stderr=e.response.text)
        except Exception as e:
             self._logger.error(f"[{self._server_name}] Unexpected error calling tool '{tool_name}': {e}")
             return ToolExecutionResult(success=False, error=f"Unexpected error: {str(e)}")

    # Implement other required methods from BaseMcpClient if any (e.g., properties)
    @property
    def is_connected(self) -> bool:
        # For simple HTTP POST, we can consider it "connected" if initialized,
        # as connections are per-request.
        # A better check might involve ensuring the client isn't closed.
        return not self._client.is_closed

    # Add dummy implementations or raise NotImplementedError for methods
    # specific to streaming/process transports if they exist in BaseMcpClient
    # and are not applicable here.

