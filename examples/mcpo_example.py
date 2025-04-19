#!/usr/bin/env python
"""
Example of using Fast-Agent with MCPO endpoints.

MCPO (Model Context Protocol Orchestrator) is a proxy that exposes
individual MCP tools as RESTful HTTP endpoints. This example shows
how to configure and use MCPO endpoints with Fast-Agent.

To run this example, you need:
1. MCPO running (e.g., via `npm install -g mcpo && mcpo server`)
2. Configure the servers in fastagent.config.yaml (see comments in that file)
"""

import asyncio
import sys
from typing import Optional, Any

from mcp_agent.core.fastagent import FastAgent

# Create the FastAgent app
fast = FastAgent("MCPO-Example")


@fast.agent(
    name="mcpo_example",
    instruction="You are an agent that provides time information and can fetch data from the web.",
    servers=["mcpo-time", "mcpo-fetch"]
)
async def main(message: str) -> str:
    """
    Example agent that uses MCPO time tools.
    
    Args:
        message: The input message (ignored in this example)
    
    Returns:
        The result of the tool calls
    """
    # This is just a demonstration - normally you'd use a proper conversation flow
    print("Using MCPO endpoints with Fast-Agent")
    
    try:
        # Call the get_current_time tool from MCPO
        # This gets the current time in a specific timezone
        time_result = await fast.context.executor.call_tool(
            "get_current_time", 
            {"timezone": "America/New_York"},  # Optional timezone parameter
            server="mcpo-time"
        )
        print(f"Current time in New York: {time_result}")
        
        # Call the convert_time tool from MCPO
        # This converts time between timezones
        convert_result = await fast.context.executor.call_tool(
            "convert_time", 
            {
                "source_timezone": "America/New_York",
                "time": "2023-01-01 12:00:00",
                "target_timezone": "Europe/London"
            },
            server="mcpo-time"
        )
        print(f"Converted time: {convert_result}")
        
        # Call the fetch tool from MCPO for comparison
        fetch_result = await fast.context.executor.call_tool(
            "fetch", 
            {
                "url": "https://jsonplaceholder.typicode.com/posts/1",
                "max_length": 1000,
                "raw": False
            }, 
            server="mcpo-fetch"
        )
        print(f"Fetch result: {fetch_result}")
        
        return "Successfully called MCPO tools!"
    except Exception as e:
        print(f"Error calling MCPO tool: {e}")
        return f"Error: {str(e)}"


if __name__ == "__main__":
    # If you provide a message, it will be passed to the agent
    msg = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else "Hello"
    
    # Run the agent
    async def run_agent():
        async with fast.run() as agent:
            result = await agent.mcpo_example.send(msg)
            print(f"\nResult: {result}")
    
    asyncio.run(run_agent()) 