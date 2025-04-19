#!/usr/bin/env python
"""
Example of using Fast-Agent with MCPO endpoints.

MCPO (Model Context Protocol Orchestrator) is a proxy that exposes
individual MCP tools as RESTful HTTP endpoints. This example shows
how to configure and use MCPO endpoints with Fast-Agent.

To run this example, you need:
1. MCPO running (e.g., via `npm install -g mcpo && mcpo server`)
2. Configure the servers in fastagent.config.yaml with mcpo_http transport
   (see the configuration in that file)
"""

import asyncio
import sys
import requests
import json
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
    print("Using MCPO endpoints with Fast-Agent (mcpo_http transport)")
    
    # Manually run the example without waiting for tool discovery
    print("Directly calling MCPO endpoints...")
    
    try:
        # Get current time from MCPO time endpoint directly via HTTP request
        time_url = "http://localhost:8000/time/get_current_time"
        time_params = {"timezone": "America/New_York"}
        time_response = requests.post(time_url, json=time_params)
        if time_response.status_code == 200:
            time_result = time_response.json()
            print(f"[Direct HTTP] Current time in New York: {time_result}")
        else:
            print(f"[Direct HTTP] Error getting time: {time_response.status_code}")
            
        # Convert time via HTTP request
        convert_url = "http://localhost:8000/time/convert_time"
        convert_params = {
            "source_timezone": "America/New_York",
            "time": "2023-01-01 12:00:00",
            "target_timezone": "Europe/London"
        }
        convert_response = requests.post(convert_url, json=convert_params)
        if convert_response.status_code == 200:
            convert_result = convert_response.json()
            print(f"[Direct HTTP] Converted time: {convert_result}")
        else:
            print(f"[Direct HTTP] Error converting time: {convert_response.status_code}")
            
        # Fetch data via HTTP request
        fetch_url = "http://localhost:8000/fetch/fetch"
        fetch_params = {
            "url": "https://jsonplaceholder.typicode.com/posts/1",
            "max_length": 1000,
            "raw": False
        }
        fetch_response = requests.post(fetch_url, json=fetch_params)
        if fetch_response.status_code == 200:
            fetch_result = fetch_response.json()
            print(f"[Direct HTTP] Fetch result: {fetch_result}")
        else:
            print(f"[Direct HTTP] Error fetching: {fetch_response.status_code}")
        
        # Now try using the Fast-Agent MCP mechanism
        try:
            print("\nNow trying with Fast-Agent MCP mechanism:")
            
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
        except Exception as e:
            print(f"Error with Fast-Agent MCP mechanism: {e}")
        
        return "Successfully called MCPO tools!"
    except Exception as e:
        print(f"Error: {e}")
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