"""
Manages the lifecycle of multiple MCP server connections.
"""

import asyncio
import traceback
from datetime import timedelta
from typing import (
    TYPE_CHECKING,
    AsyncGenerator,
    Callable,
    Dict,
    Optional,
)

from anyio import Event, Lock, create_task_group
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
from mcp import ClientSession
from mcp.client.sse import sse_client
from mcp.client.stdio import (
    StdioServerParameters,
    get_default_environment,
    stdio_client,
)
from mcp.types import JSONRPCMessage, ServerCapabilities

from mcp_agent.config import MCPServerSettings
from mcp_agent.context_dependent import ContextDependent
from mcp_agent.core.exceptions import ServerInitializationError
from mcp_agent.event_progress import ProgressAction
from mcp_agent.logging.logger import get_logger
from mcp_agent.mcp.logger_textio import get_stderr_handler
from mcp_agent.mcp.mcp_agent_client_session import MCPAgentClientSession
from mcp_agent.mcp.streamable_http import streamable_http_client
# Import the new client
from mcp_agent.mcp.clients.http_post_client import HttpPostClient

if TYPE_CHECKING:
    from mcp_agent.context import Context
    from mcp_agent.mcp_server_registry import InitHookCallable, ServerRegistry

logger = get_logger(__name__)


class ServerConnection:
    """
    Represents a long-lived MCP server connection, including:
    - The ClientSession to the server
    - The transport streams (via stdio/sse, etc.)
    """

    def __init__(
        self,
        server_name: str,
        server_config: MCPServerSettings,
        transport_context_factory: Callable[
            [],
            AsyncGenerator[
                tuple[
                    MemoryObjectReceiveStream[JSONRPCMessage | Exception],
                    MemoryObjectSendStream[JSONRPCMessage],
                ],
                None,
            ],
        ],
        client_session_factory: Callable[
            [MemoryObjectReceiveStream, MemoryObjectSendStream, timedelta | None],
            ClientSession,
        ],
        init_hook: Optional["InitHookCallable"] = None,
    ) -> None:
        self.server_name = server_name
        self.server_config = server_config
        self.session: ClientSession | None = None
        # Add a field for direct client instances (for non-stream transports)
        self.client_instance: BaseMcpClient | None = None 
        self._client_session_factory = client_session_factory
        self._init_hook = init_hook
        self._transport_context_factory = transport_context_factory
        # Signal that session is fully up and initialized
        self._initialized_event = Event()

        # Signal we want to shut down
        self._shutdown_event = Event()

        # Track error state
        self._error_occurred = False
        self._error_message = None

    def is_healthy(self) -> bool:
        """Check if the server connection is healthy and ready to use."""
        # Check both session-based and direct client-based connections
        is_session_healthy = self.session is not None and not self._error_occurred
        is_client_healthy = self.client_instance is not None and not self._error_occurred and self.client_instance.is_connected
        return is_session_healthy or is_client_healthy

    def reset_error_state(self) -> None:
        """Reset the error state, allowing reconnection attempts."""
        self._error_occurred = False
        self._error_message = None

    def request_shutdown(self) -> None:
        """
        Request the server to shut down. Signals the server lifecycle task to exit.
        """
        self._shutdown_event.set()

    async def wait_for_shutdown_request(self) -> None:
        """
        Wait until the shutdown event is set.
        """
        await self._shutdown_event.wait()

    async def initialize_session(self) -> None:
        """
        Initializes the server connection and session.
        Must be called within an async context.
        """

        result = await self.session.initialize()

        self.server_capabilities = result.capabilities
        # If there's an init hook, run it
        if self._init_hook:
            logger.info(f"{self.server_name}: Executing init hook.")
            self._init_hook(self.session, self.server_config.auth)

        # Now the session is ready for use
        self._initialized_event.set()

    async def wait_for_initialized(self) -> None:
        """
        Wait until the session is fully initialized.
        """
        await self._initialized_event.wait()

    # Add a method to directly initialize a client instance
    async def initialize_client(self, client: BaseMcpClient) -> None:
        """Initializes a direct client connection."""
        self.client_instance = client
        try:
            await client.connect()
            # Run init hook if provided (adapt as needed for client context)
            if self._init_hook:
                logger.info(f"{self.server_name}: Executing init hook for client.")
                # The hook might need adaptation if it expects a ClientSession
                # For now, pass the client instance and auth config
                # self._init_hook(client, self.server_config.auth) 
                # TODO: Review init_hook signature and usage for non-session clients
            self._initialized_event.set()
        except Exception as e:
            logger.error(f"Failed to initialize client {self.server_name}: {e}", exc_info=True)
            self._error_occurred = True
            self._error_message = traceback.format_exception(e)
            self._initialized_event.set() # Signal completion even on error

    def create_session(
        self,
        read_stream: MemoryObjectReceiveStream,
        send_stream: MemoryObjectSendStream,
    ) -> ClientSession:
        """
        Create a new session instance for this server connection.
        """

        read_timeout = (
            timedelta(seconds=self.server_config.read_timeout_seconds)
            if self.server_config.read_timeout_seconds
            else None
        )

        session = self._client_session_factory(read_stream, send_stream, read_timeout)

        # Make the server config available to the session for initialization
        if hasattr(session, "server_config"):
            session.server_config = self.server_config

        self.session = session

        return session


async def _server_lifecycle_task(server_conn: ServerConnection) -> None:
    """
    Manage the lifecycle of a single server connection.
    Runs inside the MCPConnectionManager's shared TaskGroup.
    """
    server_name = server_conn.server_name
    try:
        transport_context = server_conn._transport_context_factory()

        async with transport_context as (read_stream, write_stream):
            #      try:
            server_conn.create_session(read_stream, write_stream)

            async with server_conn.session:
                await server_conn.initialize_session()

                await server_conn.wait_for_shutdown_request()

    except Exception as exc:
        logger.error(
            f"{server_name}: Lifecycle task encountered an error: {exc}",
            exc_info=True,
            data={
                "progress_action": ProgressAction.FATAL_ERROR,
                "server_name": server_name,
            },
        )
        server_conn._error_occurred = True
        server_conn._error_message = traceback.format_exception(exc)
        # If there's an error, we should also set the event so that
        # 'get_server' won't hang
        server_conn._initialized_event.set()
        # No raise - allow graceful exit


# Add a separate lifecycle task for non-streaming clients like http_post
async def _client_lifecycle_task(server_conn: ServerConnection) -> None:
    """
    Manage the lifecycle of a single server connection using a direct client instance.
    Runs inside the MCPConnectionManager's shared TaskGroup.
    """
    server_name = server_conn.server_name
    config = server_conn.server_config
    client: BaseMcpClient | None = None

    try:
        if config.transport == "http_post":
            client = HttpPostClient(server_name, config)
        else:
            # Should not happen if routed correctly, but handle defensively
            raise NotImplementedError(f"Unsupported transport for direct client lifecycle: {config.transport}")

        await server_conn.initialize_client(client)

        # Wait for shutdown signal
        await server_conn.wait_for_shutdown_request()

    except Exception as exc:
        logger.error(
            f"{server_name}: Client lifecycle task encountered an error: {exc}",
            exc_info=True,
            data={
                "progress_action": ProgressAction.FATAL_ERROR,
                "server_name": server_name,
            },
        )
        server_conn._error_occurred = True
        server_conn._error_message = traceback.format_exception(exc)
        server_conn._initialized_event.set() # Ensure waiters are unblocked
    finally:
        if client and client.is_connected:
            try:
                await client.disconnect()
            except Exception as disc_exc:
                 logger.warning(f"{server_name}: Error during client disconnect: {disc_exc}")

class MCPConnectionManager(ContextDependent):
    """
    Manages the lifecycle of multiple MCP server connections.
    Integrates with the application context system for proper resource management.
    """

    def __init__(
        self, server_registry: "ServerRegistry", context: Optional["Context"] = None
    ) -> None:
        super().__init__(context=context)
        self.server_registry = server_registry
        self.running_servers: Dict[str, ServerConnection] = {}
        self._lock = Lock()
        # Manage our own task group - independent of task context
        self._task_group = None
        self._task_group_active = False

    async def __aenter__(self):
        # Create a task group that isn't tied to a specific task
        self._task_group = create_task_group()
        # Enter the task group context
        await self._task_group.__aenter__()
        self._task_group_active = True
        self._tg = self._task_group
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Ensure clean shutdown of all connections before exiting."""
        try:
            # First request all servers to shutdown
            await self.disconnect_all()

            # Add a small delay to allow for clean shutdown
            await asyncio.sleep(0.5)

            # Then close the task group if it's active
            if self._task_group_active:
                await self._task_group.__aexit__(exc_type, exc_val, exc_tb)
                self._task_group_active = False
                self._task_group = None
                self._tg = None
        except Exception as e:
            logger.error(f"Error during connection manager shutdown: {e}")

    async def launch_server(
        self,
        server_name: str,
        client_session_factory: Callable[
            [MemoryObjectReceiveStream, MemoryObjectSendStream, timedelta | None],
            ClientSession,
        ],
        init_hook: Optional["InitHookCallable"] = None,
    ) -> ServerConnection:
        """Launch a specific MCP server connection."""
        if server_name in self.running_servers:
            logger.warning(f"{server_name}: Server already launched. Returning existing connection.")
            return self.running_servers[server_name]

        server_config = self.server_registry.registry.get(server_name)
        if server_config is None:
            raise ValueError(f"Server '{server_name}' not found in configuration.")

        # --- Decide lifecycle task based on transport --- 
        if server_config.transport == "http_post":
            # Create ServerConnection without transport/session factories for http_post
            server_conn = ServerConnection(
                server_name=server_name,
                server_config=server_config,
                transport_context_factory=None, # Not used
                client_session_factory=None, # Not used
                init_hook=init_hook, 
            )
            self.running_servers[server_name] = server_conn
            self._tg.start_soon(_client_lifecycle_task, server_conn)
            logger.info(f"{server_name}: Launching with http_post client lifecycle.")
        elif server_config.transport in ["stdio", "sse", "streamable_http"]:
            # Existing logic for stream-based transports
            def transport_context_factory():
                stderr_handler = get_stderr_handler(logger, server_name)
                if server_config.transport == "stdio":
                    env = get_default_environment() | (server_config.env or {})
                    params = StdioServerParameters(
                        command=server_config.command,
                        arguments=server_config.args,
                        environment=env,
                        working_directory=server_config.cwd,
                    )
                    return stdio_client(params, stderr_handler)
                elif server_config.transport == "sse":
                    if not server_config.url:
                        raise ValueError(
                            f"URL must be configured for SSE transport on server '{server_name}'"
                        )
                    return sse_client(server_config.url, stderr_handler)
                elif server_config.transport == "streamable_http":
                     if not server_config.url:
                        raise ValueError(
                            f"URL must be configured for streamable_http transport on server '{server_name}'"
                        )
                     # Pass config for potential use in client (e.g., api_key, timeout)
                     return streamable_http_client(server_config, stderr_handler)
                else:
                    # Should be unreachable if config validation is proper
                    raise NotImplementedError(
                        f"Unsupported transport type: {server_config.transport}"
                    )
            
            server_conn = ServerConnection(
                server_name=server_name,
                server_config=server_config,
                transport_context_factory=transport_context_factory,
                client_session_factory=client_session_factory,
                init_hook=init_hook,
            )
            self.running_servers[server_name] = server_conn
            self._tg.start_soon(_server_lifecycle_task, server_conn)
            logger.info(f"{server_name}: Launching with {server_config.transport} session lifecycle.")
        else:
             raise NotImplementedError(f"Transport type '{server_config.transport}' is not supported.")
        

        return server_conn

    async def get_server(
        self,
        server_name: str,
        # Make factory optional as it's not needed for http_post
        client_session_factory: Optional[Callable] = None, 
        init_hook: Optional["InitHookCallable"] = None,
    ) -> ServerConnection:
        """
        Get a server connection, launching it if necessary.
        Ensures the server is initialized before returning.
        """
        async with self._lock: # Protect against concurrent launch attempts
            if server_name not in self.running_servers:
                logger.info(f"{server_name}: Server not running, launching now.")
                # Check if factory is provided for transports that need it
                server_config = self.server_registry.registry.get(server_name)
                if server_config and server_config.transport != "http_post" and not client_session_factory:
                     raise ValueError(f"client_session_factory is required for transport type '{server_config.transport}'")
                
                # Pass the factory only if it's needed by launch_server's logic
                factory_to_pass = client_session_factory if server_config and server_config.transport != "http_post" else None
                server_conn = await self.launch_server(server_name, factory_to_pass, init_hook)
            else:
                server_conn = self.running_servers[server_name]

        # Wait for initialization regardless of whether it was just launched or existed
        await server_conn.wait_for_initialized()

        if server_conn._error_occurred:
            raise ServerInitializationError(
                f"MCP Server: '{server_name}': Failed to initialize - see details.",
                details=server_conn._error_message,
            )

        return server_conn

    # Potentially adapt get_server_capabilities if needed for http_post
    # MCPO doesn't have a standard capabilities endpoint, so this might
    # return None or fixed capabilities based on config/assumptions.
    async def get_server_capabilities(self, server_name: str) -> ServerCapabilities | None:
        """Get the capabilities of a server connection."""
        if server_name not in self.running_servers:
            logger.warning(f"Attempted to get capabilities for unknown server: {server_name}")
            return None

        server_conn = self.running_servers[server_name]
        if server_conn.session: # Session-based
             return server_conn.session.server_capabilities
        elif server_conn.client_instance: # Direct client-based
            # http_post clients don't typically report capabilities via MCP standard initialize
            # We could return None or construct a default/placeholder if needed.
            logger.warning(f"Capabilities not available via standard MCP initialize for http_post server: {server_name}")
            return None 
        else:
             logger.warning(f"Server connection for {server_name} has neither session nor client instance.")
             return None

    async def disconnect_server(self, server_name: str) -> None:
        """Disconnect a specific server connection."""
        async with self._lock:
            if server_name in self.running_servers:
                server_conn = self.running_servers.pop(server_name)
                # Signal shutdown for both types of lifecycle tasks
                server_conn.request_shutdown() 
                logger.info(f"{server_name}: Disconnect requested.")
            else:
                logger.warning(f"{server_name}: Server not found for disconnection.")

    async def disconnect_all(self) -> None:
        """Disconnect all servers that are running under this connection manager."""
        # Get a copy of servers to shutdown
        servers_to_shutdown = []

        async with self._lock:
            if not self.running_servers:
                return

            # Make a copy of the servers to shut down
            servers_to_shutdown = list(self.running_servers.items())
            # Clear the dict immediately to prevent any new access
            self.running_servers.clear()

        # Release the lock before waiting for servers to shut down
        for name, conn in servers_to_shutdown:
            logger.info(f"{name}: Requesting shutdown...")
            conn.request_shutdown()
