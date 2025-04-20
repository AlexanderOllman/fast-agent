"""
Implementation of the MCP client for MCPO HTTP transport.

This transport is specifically designed to work with MCPO (Model Context Protocol Orchestrator),
which exposes MCP tools as RESTful OpenAPI endpoints.

MCPO (https://github.com/open-webui/mcpo) is a proxy that takes MCP server commands
and makes them accessible via standard RESTful HTTP, allowing tools to work with
agents and apps expecting OpenAPI servers.
"""

import asyncio
import json
import logging
import uuid
import re
from asyncio import Queue
from contextlib import asynccontextmanager
from typing import AsyncGenerator, Dict, List, Optional, Tuple, Union, cast
from urllib.parse import urljoin, urlparse

import aiohttp
from anyio import create_memory_object_stream
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream

from mcp import ClientSession
from mcp.types import JSONRPCMessage, ServerCapabilities

logger = logging.getLogger(__name__)


# Implementation of receiving messages from a stream into a queue
async def receive_from_stream_into_queue(stream, queue):
    """
    Receives messages from a stream and puts them into a queue.
    
    Args:
        stream: The stream to receive messages from
        queue: The queue to put messages into
    """
    try:
        async for message in stream:
            await queue.put(message)
    except Exception as e:
        logger.error(f"Error receiving from stream: {e}")
        await queue.put(e)


class MCPOHTTPError(Exception):
    """Error during MCPO HTTP connection."""

    def __init__(self, code: Optional[int] = None, message: Optional[str] = None):
        self.code = code
        super().__init__(f"MCPO HTTP error: {message}")


# Default reconnection options
DEFAULT_MCPO_HTTP_RECONNECTION_OPTIONS = {
    "initial_reconnection_delay": 1000,
    "max_reconnection_delay": 30000,
    "reconnection_delay_grow_factor": 1.5,
    "max_retries": 2,
}


# Function to extract tools from MCPO OpenAPI spec
async def extract_mcpo_tools(session: aiohttp.ClientSession, base_url: str) -> List[Dict]:
    """
    Extract tools from MCPO OpenAPI specification.
    
    Args:
        session: aiohttp client session
        base_url: Base URL of the MCPO server
        
    Returns:
        List of tool definitions in MCP format
    """
    openapi_url = f"{base_url.rstrip('/')}/openapi.json"
    logger.debug(f"Fetching OpenAPI spec from {openapi_url}")
    
    try:
        response = await session.get(openapi_url)
        if not response.ok:
            logger.error(f"Failed to fetch OpenAPI spec: {response.status}")
            return []
            
        data = await response.json()
        description = data.get("info", {}).get("description", "")
        
        # Regex to find tool links in the markdown-style list
        tool_pattern = re.compile(r"- \[(.*?)\]\((.*?)\)")
        tool_matches = tool_pattern.findall(description)
        
        tools = []
        for tool_name, docs_url in tool_matches:
            # Get the actual endpoint path from the docs URL
            endpoint_path = docs_url.replace("/docs", "")
            
            # For each tool, get its OpenAPI spec
            tool_spec_url = f"{base_url.rstrip('/')}{endpoint_path}/openapi.json"
            tool_resp = await session.get(tool_spec_url)
            
            if not tool_resp.ok:
                logger.warning(f"Failed to fetch spec for {tool_name}: {tool_resp.status}")
                continue
                
            tool_spec = await tool_resp.json()
            
            # Extract paths (endpoints) from the tool spec
            paths = tool_spec.get("paths", {})
            
            # Convert to MCP tool format
            for path, methods in paths.items():
                for method, details in methods.items():
                    if method.lower() == "post":  # MCPO tools are typically POST endpoints
                        endpoint_name = path.strip("/")
                        description = details.get("description", "")
                        
                        # Extract parameters from the OpenAPI spec
                        parameters = []
                        request_body = details.get("requestBody", {})
                        schema = request_body.get("content", {}).get("application/json", {}).get("schema", {})
                        
                        if "properties" in schema:
                            for param_name, param_details in schema.get("properties", {}).items():
                                param_type = param_details.get("type", "string")
                                param_desc = param_details.get("description", "")
                                
                                parameters.append({
                                    "name": param_name,
                                    "type": param_type,
                                    "description": param_desc,
                                    "required": param_name in schema.get("required", [])
                                })
                        
                        # Create MCP-compatible tool definition
                        tool_def = {
                            "name": endpoint_name,
                            "description": description,
                            "parameters": parameters
                        }
                        tools.append(tool_def)
            
        return tools
    except Exception as e:
        logger.error(f"Error extracting MCPO tools: {e}")
        return []


@asynccontextmanager
async def mcpo_http_client(
    url: str, 
    headers: Optional[Dict[str, str]] = None,
    reconnection_options: Optional[Dict] = None,
    session_id: Optional[str] = None,
) -> AsyncGenerator[Tuple[MemoryObjectReceiveStream, MemoryObjectSendStream], None]:
    """
    Create MCP client connection using MCPO HTTP transport.
    
    This client is specifically designed to work with MCPO endpoints which expose
    MCP tools via RESTful HTTP endpoints. It follows the OpenAPI approach rather
    than the MCP Streamable HTTP specification.
    
    Args:
        url: The MCPO endpoint URL to connect to
        headers: Optional HTTP headers to include in requests
        reconnection_options: Options for handling reconnection (using defaults if not provided)
        session_id: Optional session ID (not typically used with MCPO)
        
    Returns:
        A tuple of (receive_stream, send_stream) for bidirectional communication
    """
    # Initialize reconnection options with defaults
    recon_options = DEFAULT_MCPO_HTTP_RECONNECTION_OPTIONS.copy()
    if reconnection_options:
        recon_options.update(reconnection_options)
    
    # Initialize headers
    request_headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    if headers:
        request_headers.update(headers)
    
    # Extract the endpoint type (e.g., "time", "fetch") from the URL for tool discovery
    url_path = url.rstrip('/').split('/')[-1]
    logger.info(f"MCPO HTTP client: Identified endpoint type from URL: {url_path}")
    
    # Create memory streams for sending/receiving JSON-RPC messages
    # We'll wrap these with our filtering streams
    raw_receive_stream_send, raw_receive_stream_recv = create_memory_object_stream[Union[JSONRPCMessage, Exception]](
        max_buffer_size=32
    )
    raw_send_stream_send, raw_send_stream_recv = create_memory_object_stream[JSONRPCMessage](
        max_buffer_size=32
    )
    
    # Create filtered streams that intercept initialize messages
    filtered_send_stream_send, filtered_send_stream_recv = create_memory_object_stream[JSONRPCMessage](
        max_buffer_size=32
    )
    
    # Create a queue to receive messages
    message_queue: Queue[Union[JSONRPCMessage, Exception]] = Queue()
    
    # Create an aiohttp session for HTTP requests
    session = aiohttp.ClientSession()
    
    try:
        # Define tool definitions for different endpoint types
        endpoint_tools = {
            "time": [
                {
                    "name": "get_current_time",
                    "description": "Get current time in a specific timezone",
                    "parameters": [
                        {
                            "name": "timezone",
                            "type": "string",
                            "description": "Timezone to get the current time for (e.g., 'America/New_York', 'UTC')",
                            "required": True
                        }
                    ]
                },
                {
                    "name": "convert_time",
                    "description": "Convert time between timezones",
                    "parameters": [
                        {
                            "name": "source_timezone",
                            "type": "string",
                            "description": "Source timezone",
                            "required": True
                        },
                        {
                            "name": "time",
                            "type": "string",
                            "description": "Time to convert (format: YYYY-MM-DD HH:MM:SS)",
                            "required": True
                        },
                        {
                            "name": "target_timezone",
                            "type": "string",
                            "description": "Target timezone",
                            "required": True
                        }
                    ]
                }
            ],
            "fetch": [
                {
                    "name": "fetch",
                    "description": "Fetches a URL from the internet and optionally extracts its contents as markdown",
                    "parameters": [
                        {
                            "name": "url",
                            "type": "string",
                            "description": "URL to fetch",
                            "required": True
                        },
                        {
                            "name": "max_length",
                            "type": "integer",
                            "description": "Maximum length to return",
                            "required": False
                        },
                        {
                            "name": "raw",
                            "type": "boolean",
                            "description": "Whether to return raw content",
                            "required": False
                        }
                    ]
                }
            ],
            "arxiv-latex": [
                {
                    "name": "get_paper_prompt",
                    "description": "Get a flattened LaTeX code of a paper from arXiv ID",
                    "parameters": [
                        {
                            "name": "arxiv_id",
                            "type": "string",
                            "description": "The arXiv ID of the paper (e.g., '2403.12345')",
                            "required": True
                        }
                    ]
                }
            ]
        }
        
        # Start task to filter outgoing messages
        async def filter_outgoing_messages():
            async for message in filtered_send_stream_recv:
                method = message.get("method")
                message_id = message.get("id")
                
                # Log all messages for debugging
                logger.debug(f"MCPO HTTP filter: Processing message method={method}, id={message_id}")
                
                # Block initialize messages
                if method == "initialize":
                    logger.info(f"MCPO HTTP filter: Blocking initialize message at transport level, id={message_id}")
                    # Send a fake success response immediately
                    fake_response = {
                        "jsonrpc": "2.0",
                        "id": message_id,
                        "result": {
                            "capabilities": {
                                "tools": {"supported": True},
                                "prompts": {"supported": False},
                                "resources": {"supported": False},
                                "roots": {"supported": False}
                            }
                        }
                    }
                    await message_queue.put(fake_response)
                    continue
                    
                # Handle tools/list messages
                if method == "tools/list":
                    logger.info(f"MCPO HTTP filter: Intercepting tools/list message at transport level, id={message_id}")
                    
                    # Get tools for this endpoint type
                    tools = endpoint_tools.get(url_path, [])
                    logger.debug(f"MCPO HTTP filter: Found {len(tools)} tools for endpoint {url_path}")
                    
                    # Create tools response
                    tools_response = {
                        "jsonrpc": "2.0",
                        "id": message_id,
                        "result": {
                            "items": tools,
                            "isIncomplete": False
                        }
                    }
                    
                    # Send the response
                    logger.info(f"MCPO HTTP filter: Sending tools/list response with {len(tools)} tools")
                    await message_queue.put(tools_response)
                    continue
                
                # All other messages go to the raw transport stream
                await raw_send_stream_send.send(message)
        
        # Start the filter task
        logger.debug("MCPO HTTP: Starting message filter task")
        filter_task = asyncio.create_task(filter_outgoing_messages())
        
        # Start message forwarding task from raw transport to queue
        logger.debug("MCPO HTTP: Starting message forwarding task")
        forwarding_task = asyncio.create_task(
            receive_from_stream_into_queue(raw_send_stream_recv, message_queue)
        )
        
        # Start a task to process the message queue to the receive stream
        async def process_message_queue():
            while True:
                try:
                    message = await message_queue.get()
                    
                    if isinstance(message, Exception):
                        logger.error(f"Processing error message: {message}")
                        await raw_receive_stream_send.send(message)
                    else:
                        # It's a JSON-RPC message
                        message_id = message.get("id", "unknown") 
                        logger.debug(f"Processing message id={message_id}")
                        await raw_receive_stream_send.send(cast(JSONRPCMessage, message))
                except Exception as e:
                    logger.error(f"Error forwarding message: {e}")
                    await raw_receive_stream_send.send(e)
                    
        logger.debug("MCPO HTTP: Starting queue processing task")
        queue_processing_task = asyncio.create_task(process_message_queue())
        
        # Sender function - handles sending messages via HTTP POST to MCPO
        async def sender(message: JSONRPCMessage):
            # Get key values from the message
            method = message.get("method")
            params = message.get("params", {})
            message_id = message.get("id")
            
            # Log the message we're sending
            logger.debug(f"MCPO HTTP sender: Processing message method={method}, id={message_id}")
            
            # Skip initialize and tools/list methods - these should have been handled at the filter level
            # But just in case they got here, handle them
            if method == "initialize" or method == "tools/list":
                logger.warning(f"MCPO HTTP sender: {method} message reached sender function! This should have been handled at filter level. id={message_id}")
                return
            
            # If this isn't an MCPO-compatible method, return appropriate error
            if not method or method.startswith("sampling/") or method.startswith("resources/") or method.startswith("roots/"):
                logger.warning(f"MCPO HTTP: Unsupported method '{method}' - MCPO only supports direct tool calls")
                # These are MCP methods not supported by MCPO - return appropriate error
                error_response = {
                    "jsonrpc": "2.0",
                    "id": message_id,
                    "error": {
                        "code": -32601,
                        "message": f"Method '{method}' not supported by MCPO endpoint"
                    }
                }
                await message_queue.put(error_response)
                return
            
            try:
                # Make the actual HTTP POST request to the MCPO endpoint
                logger.debug(f"MCPO HTTP: Making POST request to {url} with params: {params}")
                response = await session.post(
                    url,
                    headers=request_headers,
                    json=params,  # MCPO expects just the params, not the full JSON-RPC message
                )
                
                # Check response status
                if not response.ok:
                    error_text = await response.text()
                    logger.error(f"MCPO HTTP error: {response.status} - {error_text}")
                    
                    error_response = {
                        "jsonrpc": "2.0",
                        "id": message_id,
                        "error": {
                            "code": response.status,
                            "message": f"MCPO endpoint error: {error_text}"
                        }
                    }
                    await message_queue.put(error_response)
                    return
                
                # Parse the response and map it back to MCP format
                response_data = await response.json()
                logger.debug(f"MCPO HTTP: Received response: {response_data}")
                
                # MCPO returns the result directly, not wrapped in a JSON-RPC envelope
                mcp_response = {
                    "jsonrpc": "2.0",
                    "id": message_id,
                    "result": response_data
                }
                
                logger.debug(f"MCPO HTTP: Mapped response to MCP format: {mcp_response}")
                await message_queue.put(mcp_response)
                
            except Exception as e:
                logger.error(f"Error calling MCPO endpoint: {e}")
                error_response = {
                    "jsonrpc": "2.0",
                    "id": message_id,
                    "error": {
                        "code": -32000,
                        "message": f"Error calling MCPO endpoint: {str(e)}"
                    }
                }
                await message_queue.put(error_response)
        
        # Start a task to process outgoing messages
        async def process_send_stream():
            async for message in raw_send_stream_recv:
                await sender(message)
                
        logger.debug("MCPO HTTP: Starting message sending task")
        send_task = asyncio.create_task(process_send_stream())
        
        try:
            # Log ready status
            logger.info("MCPO HTTP: Client ready for communication")
            
            # Yield filtered streams for the client to use
            yield raw_receive_stream_recv, filtered_send_stream_send
        finally:
            # Clean up when the client is done
            # Cancel all tasks
            logger.debug("MCPO HTTP: Cleaning up client connection")
            filter_task.cancel()
            forwarding_task.cancel()
            queue_processing_task.cancel()
            send_task.cancel()
            
            # Close the memory streams
            await raw_receive_stream_send.aclose()
            await raw_send_stream_send.aclose()
            await filtered_send_stream_send.aclose()
    finally:
        # Ensure we close the aiohttp session
        logger.debug("MCPO HTTP: Closing HTTP session")
        await session.close() 