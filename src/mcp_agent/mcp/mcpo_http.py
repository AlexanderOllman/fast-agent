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
import aiohttp
from asyncio import Queue
from contextlib import asynccontextmanager
from typing import AsyncGenerator, Dict, List, Optional, Tuple, Union, cast
from urllib.parse import urljoin, urlparse

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
async def fetch_mcpo_tools(base_url: str, session: aiohttp.ClientSession) -> Dict:
    """
    Fetches tool information from the MCPO OpenAPI spec.
    
    Args:
        base_url: The base URL of the MCPO server
        session: The aiohttp client session
        
    Returns:
        A dictionary containing the tool definitions in MCP format
    """
    try:
        # First try the specific endpoint's OpenAPI spec
        endpoint_url = f"{base_url}/openapi.json"
        response = await session.get(endpoint_url)
        
        if response.status != 200:
            # If that fails, try the main MCPO OpenAPI spec
            root_url = "/".join(base_url.split("/")[:-1])
            if not root_url:
                root_url = base_url
            
            main_spec_url = f"{root_url}/openapi.json"
            response = await session.get(main_spec_url)
            
            if response.status != 200:
                logger.error(f"Failed to fetch OpenAPI spec from {endpoint_url} or {main_spec_url}")
                return {"tools": []}
        
        data = await response.json()
        
        # Parse the endpoint path to get the tool name
        endpoint_path = urlparse(base_url).path
        tool_name = endpoint_path.split("/")[-1] if endpoint_path else "default"
        
        # Find operations in the spec
        paths = data.get("paths", {})
        operations = []
        
        if len(paths) > 0:
            # Extract operations from the first path
            for path, methods in paths.items():
                for method, operation in methods.items():
                    if method.lower() == "post":
                        # Extract operation name from the path
                        operation_name = path.split("/")[-1]
                        if operation_name == tool_name:
                            # If the path matches the tool name, use the tool name directly
                            operations.append({
                                "name": tool_name,
                                "description": operation.get("description", f"MCPO {tool_name} tool"),
                                "parameters": operation.get("requestBody", {}).get("content", {}).get("application/json", {}).get("schema", {}).get("properties", {})
                            })
                        else:
                            # Otherwise use the operation name from the path
                            operations.append({
                                "name": operation_name,
                                "description": operation.get("description", f"MCPO {operation_name} operation"),
                                "parameters": operation.get("requestBody", {}).get("content", {}).get("application/json", {}).get("schema", {}).get("properties", {})
                            })
        
        # If no operations found and there's a description, try to extract tool info from it
        if not operations and "description" in data.get("info", {}):
            description = data["info"]["description"]
            # Try to find links to tools in the description using a regex
            tool_pattern = re.compile(r"- \[(.*?)\]\((.*?)\)")
            tool_matches = tool_pattern.findall(description)
            
            if tool_matches:
                logger.info(f"Found {len(tool_matches)} tools in the OpenAPI description")
                operations.append({
                    "name": tool_name,
                    "description": f"MCPO {tool_name} tool",
                    "parameters": {}
                })
        
        # If still no operations, add a default one based on the endpoint
        if not operations:
            operations.append({
                "name": tool_name,
                "description": f"MCPO {tool_name} tool",
                "parameters": {}
            })
        
        # Convert to MCP format
        mcp_tools = []
        for op in operations:
            param_schema = {}
            for param_name, param_details in op.get("parameters", {}).items():
                param_schema[param_name] = {
                    "type": param_details.get("type", "string"),
                    "description": param_details.get("description", f"{param_name} parameter"),
                    "required": param_details.get("required", False)
                }
            
            mcp_tools.append({
                "name": op["name"],
                "description": op["description"],
                "parameters": {
                    "type": "object",
                    "properties": param_schema,
                    "required": [name for name, details in param_schema.items() if details.get("required", False)]
                }
            })
        
        logger.info(f"Converted {len(mcp_tools)} MCPO tools to MCP format for {base_url}")
        return {"tools": mcp_tools}
    
    except Exception as e:
        logger.error(f"Error fetching MCPO tools: {e}")
        return {"tools": []}


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
        # Cache for tools list to avoid repeated fetching
        tools_cache = None
        
        # Start task to filter outgoing messages
        async def filter_outgoing_messages():
            async for message in filtered_send_stream_recv:
                # Completely block all initialize messages
                if message.get("method") == "initialize":
                    logger.info("MCPO HTTP: Blocking initialize message at transport level")
                    # Just drop the message - don't forward it
                    continue
                    
                # Forward all other messages
                await raw_send_stream_send.send(message)
        
        filter_task = asyncio.create_task(filter_outgoing_messages())
        
        # Start message forwarding task from raw transport to queue
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
                        logger.debug(f"Processing message: {message}")
                        await raw_receive_stream_send.send(cast(JSONRPCMessage, message))
                except Exception as e:
                    logger.error(f"Error forwarding message: {e}")
                    await raw_receive_stream_send.send(e)
                    
        queue_processing_task = asyncio.create_task(process_message_queue())
        
        # Sender function - handles sending messages via HTTP POST to MCPO
        async def sender(message: JSONRPCMessage):
            nonlocal tools_cache
            
            # Get key values from the message
            method = message.get("method")
            params = message.get("params", {})
            message_id = message.get("id")
            
            # Log the message we're sending
            logger.debug(f"MCPO HTTP: Sending message method={method}, id={message_id}")
            
            # Skip initialize method - completely ignore it at this level
            if method == "initialize":
                logger.info("MCPO HTTP: Sending fake successful response for initialize request")
                # Manually send a successful response to the initialize request
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
                return
            
            # Handle tools/list method by fetching the OpenAPI spec
            if method == "tools/list":
                logger.info("MCPO HTTP: Handling tools/list request by fetching OpenAPI spec")
                
                # Use cached tools if available
                if tools_cache is None:
                    tools_cache = await fetch_mcpo_tools(url, session)
                
                # Send tools list response
                tools_response = {
                    "jsonrpc": "2.0",
                    "id": message_id,
                    "result": tools_cache
                }
                
                logger.debug(f"MCPO HTTP: Returning tools list: {tools_response}")
                await message_queue.put(tools_response)
                return
            
            # If this isn't an MCPO-compatible method, return appropriate error
            if method and (method.startswith("sampling/") or method.startswith("resources/") or method.startswith("roots/")):
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
                # For tool calls, the method is the tool name
                # Strip the tools/ prefix if present
                tool_name = method
                if tool_name.startswith("tools/"):
                    tool_name = tool_name[6:]  # Remove 'tools/' prefix
                
                # Make the actual HTTP POST request to the MCPO endpoint
                logger.debug(f"MCPO HTTP: Making POST request to {url} with params: {params}")
                
                # The actual endpoint might be the base URL or have an additional path component
                # Prefer the full endpoint with tool name if we have a direct tool call
                endpoint_url = url
                if "/" in url and not url.endswith(tool_name):
                    # If the URL doesn't end with the tool name, append it
                    endpoint_url = f"{url}/{tool_name}"
                
                logger.debug(f"MCPO HTTP: Final endpoint URL: {endpoint_url}")
                response = await session.post(
                    endpoint_url,
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