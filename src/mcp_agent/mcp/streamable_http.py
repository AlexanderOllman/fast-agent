"""
Implementation of the MCP Streamable HTTP transport client.

Based on the Streamable HTTP transport specification from:
https://modelcontextprotocol.io/specification/2025-03-26/basic/transports#streamable-http
"""

import asyncio
import json
import logging
from asyncio import Queue
from contextlib import asynccontextmanager
from typing import AsyncGenerator, Dict, List, Optional, Tuple, Union, cast
from urllib.parse import urljoin, urlparse

import aiohttp
from anyio import create_memory_object_stream
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream

from mcp.types import JSONRPCMessage

logger = logging.getLogger(__name__)


# Implementation of the missing function from mcp.client.session
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


class StreamableHTTPError(Exception):
    """Error during Streamable HTTP connection."""

    def __init__(self, code: Optional[int] = None, message: Optional[str] = None):
        self.code = code
        super().__init__(f"Streamable HTTP error: {message}")


# Default reconnection options for StreamableHTTP connections
DEFAULT_STREAMABLE_HTTP_RECONNECTION_OPTIONS = {
    "initial_reconnection_delay": 1000,
    "max_reconnection_delay": 30000,
    "reconnection_delay_grow_factor": 1.5,
    "max_retries": 2,
}


class SSEEventProcessor:
    """Process Server-Sent Events from a response stream and emit parsed JSON-RPC messages."""

    def __init__(
        self,
        response: aiohttp.ClientResponse,
        message_queue: Queue,
        on_resumption_token=None,
        replay_message_id=None,
    ):
        self.response = response
        self.message_queue = message_queue
        self.on_resumption_token = on_resumption_token
        self.replay_message_id = replay_message_id
        self.last_event_id = None
        self.buffer = ""
        self.running = True

    async def process(self):
        """Process the SSE stream from the response."""
        try:
            async for line in self.response.content:
                line_text = line.decode("utf-8")
                
                # Handle SSE protocol parsing
                if not line_text.strip():
                    # End of event
                    if self.buffer:
                        await self._process_event()
                        self.buffer = ""
                    continue
                
                if line_text.startswith(":"):
                    # Comment line, ignore
                    continue
                
                # Add to buffer
                self.buffer += line_text
                
                # Check if this completes an event
                if line_text.strip() == "":
                    await self._process_event()
                    self.buffer = ""
        except Exception as e:
            logger.error(f"Error processing SSE stream: {e}")
            await self.message_queue.put(StreamableHTTPError(message=f"SSE stream error: {e}"))
            raise
        finally:
            self.running = False

    async def _process_event(self):
        """Process a complete SSE event from the buffer."""
        if not self.buffer:
            return
        
        data = None
        event_type = None
        event_id = None
        
        for line in self.buffer.split("\n"):
            line = line.strip()
            if not line:
                continue
                
            if line.startswith("data:"):
                data_line = line[5:].strip()
                if data is None:
                    data = data_line
                else:
                    data += "\n" + data_line
            elif line.startswith("event:"):
                event_type = line[6:].strip()
            elif line.startswith("id:"):
                event_id = line[3:].strip()
        
        # Store event ID if provided
        if event_id:
            self.last_event_id = event_id
            if self.on_resumption_token:
                self.on_resumption_token(event_id)
                
        # Only process 'message' events or events with no type
        if not event_type or event_type == "message":
            if data:
                try:
                    message = json.loads(data)
                    # Override ID if replay message ID is provided and this is a response
                    if (
                        self.replay_message_id is not None 
                        and "id" in message 
                        and "result" in message
                    ):
                        message["id"] = self.replay_message_id
                    
                    await self.message_queue.put(message)
                except json.JSONDecodeError:
                    logger.error(f"Invalid JSON in SSE event: {data}")
                    await self.message_queue.put(
                        StreamableHTTPError(message=f"Invalid JSON in SSE event: {data}")
                    )


@asynccontextmanager
async def streamable_http_client(
    url: str, 
    headers: Optional[Dict[str, str]] = None,
    reconnection_options: Optional[Dict] = None,
    session_id: Optional[str] = None,
) -> AsyncGenerator[Tuple[MemoryObjectReceiveStream, MemoryObjectSendStream], None]:
    """
    Create MCP client connection using Streamable HTTP transport.
    
    Args:
        url: The endpoint URL to connect to
        headers: Optional HTTP headers to include in requests
        reconnection_options: Options for handling reconnection (using defaults if not provided)
        session_id: Optional session ID for resuming a previous session
        
    Returns:
        A tuple of (receive_stream, send_stream) for bidirectional communication
    """
    # Initialize reconnection options with defaults
    recon_options = DEFAULT_STREAMABLE_HTTP_RECONNECTION_OPTIONS.copy()
    if reconnection_options:
        recon_options.update(reconnection_options)
    
    # Initialize headers
    request_headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }
    if headers:
        request_headers.update(headers)
    
    # Add session ID if provided
    if session_id:
        request_headers["Mcp-Session-Id"] = session_id
    
    # Create memory streams for sending/receiving JSON-RPC messages
    receive_stream_send, receive_stream_recv = create_memory_object_stream[Union[JSONRPCMessage, Exception]](
        max_buffer_size=32
    )
    send_stream_send, send_stream_recv = create_memory_object_stream[JSONRPCMessage](
        max_buffer_size=32
    )
    
    # Create a queue to receive messages from SSE before forwarding to stream
    message_queue: Queue[Union[JSONRPCMessage, Exception]] = Queue()
    
    # Track if we should attempt reconnection
    should_reconnect = True
    reconnect_attempt = 0
    last_event_id = None
    
    # Create an aiohttp session for HTTP requests
    session = aiohttp.ClientSession()
    
    try:
        # Start message forwarding task
        forwarding_task = asyncio.create_task(
            receive_from_stream_into_queue(send_stream_recv, message_queue)
        )
        
        # Start a task to process the message queue
        async def process_message_queue():
            while should_reconnect:
                try:
                    message = await message_queue.get()
                    
                    if isinstance(message, Exception):
                        await receive_stream_send.send(message)
                    else:
                        # It's a JSON-RPC message
                        await receive_stream_send.send(cast(JSONRPCMessage, message))
                except Exception as e:
                    logger.error(f"Error forwarding message: {e}")
                    await receive_stream_send.send(e)
                    
        queue_processing_task = asyncio.create_task(process_message_queue())
        
        # Function to try opening an SSE stream
        async def open_sse_stream(
            resumption_token=None, 
            replay_message_id=None
        ):
            nonlocal session_id
            
            current_headers = request_headers.copy()
            if resumption_token:
                current_headers["Last-Event-ID"] = resumption_token
                
            try:
                response = await session.get(
                    url, 
                    headers=current_headers,
                    timeout=aiohttp.ClientTimeout(total=None)  # No timeout
                )
                
                # Handle error responses
                if response.status != 200:
                    if response.status == 405:
                        # Server doesn't support GET (expected case)
                        return None
                    elif response.status == 401:
                        # Unauthorized - would need auth handling
                        raise StreamableHTTPError(
                            code=response.status, 
                            message="Authentication required"
                        )
                    else:
                        raise StreamableHTTPError(
                            code=response.status,
                            message=f"Failed to open SSE stream: {response.reason}"
                        )
                
                # Check if we got a session ID
                if "mcp-session-id" in response.headers:
                    session_id = response.headers.get("mcp-session-id")
                    # Update request headers with the session ID
                    request_headers["Mcp-Session-Id"] = session_id
                
                # Only process if we got text/event-stream content type
                content_type = response.headers.get("content-type", "")
                if "text/event-stream" not in content_type:
                    raise StreamableHTTPError(
                        message=f"Expected text/event-stream, got {content_type}"
                    )
                
                # Process the SSE stream
                processor = SSEEventProcessor(
                    response,
                    message_queue,
                    on_resumption_token=lambda token: setattr(open_sse_stream, "last_event_id", token),
                    replay_message_id=replay_message_id
                )
                
                return asyncio.create_task(processor.process())
            except Exception as e:
                if not isinstance(e, StreamableHTTPError):
                    logger.error(f"Error opening SSE stream: {e}")
                    e = StreamableHTTPError(message=f"Failed to open SSE stream: {e}")
                await message_queue.put(e)
                return None
        
        # Try to open an initial SSE stream (this is not required by spec)
        sse_task = await open_sse_stream()
        
        # Sender function - handles sending messages via HTTP POST
        async def sender(message: JSONRPCMessage):
            nonlocal session_id, should_reconnect, reconnect_attempt
            
            current_headers = request_headers.copy()
            if session_id:
                current_headers["Mcp-Session-Id"] = session_id
            
            try:
                response = await session.post(
                    url,
                    headers=current_headers,
                    json=message,
                )
                
                # Handle session ID if received during initialization
                if "mcp-session-id" in response.headers:
                    session_id = response.headers.get("mcp-session-id")
                    # Update request headers with the session ID
                    request_headers["Mcp-Session-Id"] = session_id
                
                # Check response status
                if not response.ok:
                    if response.status == 401:
                        # Authentication needed - would need auth handling
                        raise StreamableHTTPError(
                            code=response.status,
                            message="Authentication required"
                        )
                    elif response.status == 404 and session_id:
                        # Session expired or not found
                        raise StreamableHTTPError(
                            code=response.status,
                            message="Session expired or not found"
                        )
                    
                    # For other errors, get response text if available
                    error_text = await response.text()
                    raise StreamableHTTPError(
                        code=response.status,
                        message=f"Error from server: {error_text}"
                    )
                
                # If we got 202 Accepted, there's no content to process
                if response.status == 202:
                    return
                
                # Check if we need to process response content
                content_type = response.headers.get("content-type", "")
                
                # Check if this message has an ID (is a request expecting response)
                is_request = "id" in message and "method" in message
                
                if is_request:
                    if "text/event-stream" in content_type:
                        # This is a streaming response
                        # Cancel existing SSE stream if any
                        if sse_task and not sse_task.done():
                            sse_task.cancel()
                            
                        # Start a new SSE processor for this response
                        def on_resumption_token(token):
                            nonlocal last_event_id
                            last_event_id = token
                            
                        processor = SSEEventProcessor(
                            response,
                            message_queue,
                            on_resumption_token=on_resumption_token
                        )
                        await processor.process()
                    elif "application/json" in content_type:
                        # For non-streaming servers, we get direct JSON responses
                        data = await response.json()
                        
                        # Handle both single message and arrays of messages
                        if isinstance(data, list):
                            for msg in data:
                                await message_queue.put(msg)
                        else:
                            await message_queue.put(data)
                    else:
                        raise StreamableHTTPError(
                            message=f"Unexpected content type: {content_type}"
                        )
            except Exception as e:
                if not isinstance(e, StreamableHTTPError):
                    e = StreamableHTTPError(message=f"Error sending message: {e}")
                
                # Try to reconnect if we have a last event ID
                if last_event_id and should_reconnect:
                    # Use exponential backoff for reconnection
                    if reconnect_attempt < recon_options["max_retries"]:
                        delay = min(
                            recon_options["initial_reconnection_delay"] * 
                            (recon_options["reconnection_delay_grow_factor"] ** reconnect_attempt),
                            recon_options["max_reconnection_delay"]
                        )
                        
                        logger.info(f"Connection lost, attempting reconnection in {delay}ms...")
                        await asyncio.sleep(delay / 1000)  # Convert ms to seconds
                        
                        # Try to reconnect with last event ID
                        sse_task = await open_sse_stream(
                            resumption_token=last_event_id,
                            replay_message_id=message.get("id") if is_request else None
                        )
                        
                        reconnect_attempt += 1
                    else:
                        # Max retries exceeded
                        logger.error(f"Max reconnection attempts exceeded")
                        should_reconnect = False
                        
                # Forward the error
                await message_queue.put(e)
        
        # Start a task to process outgoing messages
        async def process_send_stream():
            async for message in send_stream_recv:
                await sender(message)
                
        send_task = asyncio.create_task(process_send_stream())
        
        try:
            # Yield streams for the client to use
            yield receive_stream_recv, send_stream_send
        finally:
            # Clean up when the client is done
            should_reconnect = False
            
            # Cancel all tasks
            if sse_task and not sse_task.done():
                sse_task.cancel()
                
            forwarding_task.cancel()
            queue_processing_task.cancel()
            send_task.cancel()
            
            # Try to terminate the session if we have a session ID
            if session_id:
                try:
                    current_headers = request_headers.copy()
                    current_headers["Mcp-Session-Id"] = session_id
                    
                    response = await session.delete(
                        url,
                        headers=current_headers,
                    )
                    
                    # We specifically handle 405 as valid (server doesn't support session termination)
                    if not response.ok and response.status != 405:
                        logger.error(f"Failed to terminate session: {response.status} {response.reason}")
                except Exception as e:
                    logger.error(f"Error terminating session: {e}")
            
            # Close the memory streams
            await receive_stream_send.aclose()
            await send_stream_send.aclose()
    finally:
        # Ensure we close the aiohttp session
        await session.close() 