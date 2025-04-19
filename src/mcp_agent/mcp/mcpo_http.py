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
from asyncio import Queue
from contextlib import asynccontextmanager
from typing import AsyncGenerator, Dict, List, Optional, Tuple, Union, cast
from urllib.parse import urljoin, urlparse

import aiohttp
from anyio import create_memory_object_stream
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream

from mcp import ClientSession
from mcp.client.session import SyncClientSession
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


# Custom client session for MCPO endpoints
class MCPOClientSession(ClientSession):
    """A custom ClientSession that overrides the initialize method for MCPO endpoints."""
    
    async def initialize(self):
        """
        Override the initialize method to avoid sending an actual initialize message
        to the MCPO endpoint, which doesn't support it.
        
        Returns:
            A fake successful initialization response.
        """
        logger.info("MCPO HTTP: Using custom initialize method")
        
        # Return a fake successful initialization response
        return ServerCapabilities(
            tools={"supported": True},
            prompts={"supported": False},
            resources={"supported": False},
            roots={"supported": False}
        )
    
    async def call_tool(self, method: str, params: Dict, **kwargs):
        """
        Call a tool on the MCPO endpoint.
        
        Args:
            method: The name of the tool method to call
            params: The parameters to pass to the tool
            
        Returns:
            The result of the tool call
        """
        logger.debug(f"MCPO HTTP: Calling tool {method} with params {params}")
        
        # Generate a unique ID for this request
        request_id = str(uuid.uuid4())
        
        # Format as JSON-RPC request
        request = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params
        }
        
        # Send the request through the normal write stream
        await self._write_stream.send(request)
        
        # Wait for the response with matching ID
        while True:
            message = await self._read_stream.receive()
            
            if isinstance(message, Exception):
                raise message
                
            if message.get("id") == request_id:
                if "error" in message:
                    error = message["error"]
                    raise Exception(f"MCPO tool error: {error.get('message', 'Unknown error')}")
                
                return message.get("result")


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
    receive_stream_send, receive_stream_recv = create_memory_object_stream[Union[JSONRPCMessage, Exception]](
        max_buffer_size=32
    )
    send_stream_send, send_stream_recv = create_memory_object_stream[JSONRPCMessage](
        max_buffer_size=32
    )
    
    # Create a queue to receive messages
    message_queue: Queue[Union[JSONRPCMessage, Exception]] = Queue()
    
    # Create an aiohttp session for HTTP requests
    session = aiohttp.ClientSession()
    
    try:
        # Start message forwarding task
        forwarding_task = asyncio.create_task(
            receive_from_stream_into_queue(send_stream_recv, message_queue)
        )
        
        # Start a task to process the message queue
        async def process_message_queue():
            while True:
                try:
                    message = await message_queue.get()
                    
                    if isinstance(message, Exception):
                        logger.error(f"Processing error message: {message}")
                        await receive_stream_send.send(message)
                    else:
                        # It's a JSON-RPC message
                        logger.debug(f"Processing message: {message}")
                        await receive_stream_send.send(cast(JSONRPCMessage, message))
                except Exception as e:
                    logger.error(f"Error forwarding message: {e}")
                    await receive_stream_send.send(e)
                    
        queue_processing_task = asyncio.create_task(process_message_queue())
        
        # Sender function - handles sending messages via HTTP POST to MCPO
        async def sender(message: JSONRPCMessage):
            # Get key values from the message
            method = message.get("method")
            params = message.get("params", {})
            message_id = message.get("id")
            
            # Log the message we're sending
            logger.debug(f"MCPO HTTP: Sending message method={method}, id={message_id}")
            
            # Skip initialize method - it's handled by our custom session class
            if method == "initialize":
                logger.info("MCPO HTTP: Ignoring initialize request - handled by custom session")
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
            async for message in send_stream_recv:
                await sender(message)
                
        send_task = asyncio.create_task(process_send_stream())
        
        try:
            # Log ready status
            logger.info("MCPO HTTP: Client ready for communication")
            
            # Yield streams for the client to use
            yield receive_stream_recv, send_stream_send
        finally:
            # Clean up when the client is done
            # Cancel all tasks
            logger.debug("MCPO HTTP: Cleaning up client connection")
            forwarding_task.cancel()
            queue_processing_task.cancel()
            send_task.cancel()
            
            # Close the memory streams
            await receive_stream_send.aclose()
            await send_stream_send.aclose()
    finally:
        # Ensure we close the aiohttp session
        logger.debug("MCPO HTTP: Closing HTTP session")
        await session.close() 