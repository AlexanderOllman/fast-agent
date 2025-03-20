"""
Tests for the mcp_content module.
"""

import tempfile
import os
from pathlib import Path
import base64

import pytest
from mcp.types import TextContent, ImageContent, EmbeddedResource

from mcp_agent.core.mcp_content import (
    MCPText,
    MCPImage,
    MCPFile,
    MCPPrompt,
    User,
    Assistant,
)


def test_text_content():
    """Test creating text content."""
    # Test basic text content
    message = MCPText("Hello, world!")

    assert message["role"] == "user"
    assert isinstance(message["content"], TextContent)
    assert message["content"].type == "text"
    assert message["content"].text == "Hello, world!"

    # Test with custom role
    message = MCPText("Hello, world!", role="assistant")
    assert message["role"] == "assistant"


def test_image_content():
    """Test creating image content."""
    # Create a temporary image file
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        f.write(b"fake image data")
        temp_path = f.name

    try:
        # Test with file path
        message = MCPImage(temp_path)

        assert message["role"] == "user"
        assert isinstance(message["content"], ImageContent)
        assert message["content"].type == "image"
        assert message["content"].mimeType == "image/png"

        # Decode the base64 data
        decoded = base64.b64decode(message["content"].data)
        assert decoded == b"fake image data"

        # Test with raw data
        message = MCPImage(
            data=b"fake image data", mime_type="image/jpeg", role="assistant"
        )
        assert message["role"] == "assistant"
        assert message["content"].mimeType == "image/jpeg"
        decoded = base64.b64decode(message["content"].data)
        assert decoded == b"fake image data"

        # Test error cases
        with pytest.raises(ValueError):
            MCPImage()  # No path or data

        with pytest.raises(ValueError):
            MCPImage(path=temp_path, data=b"data")  # Both path and data

    finally:
        # Clean up
        os.unlink(temp_path)


def test_resource_content():
    """Test creating embedded resource content."""
    # Create temporary text and binary files
    with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as f:
        f.write(b"Hello, world!")
        text_path = f.name

    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
        f.write(b"%PDF-1.0 fake pdf data")
        binary_path = f.name

    try:
        # Test with text file
        message = MCPFile(text_path)

        assert message["role"] == "user"
        assert isinstance(message["content"], EmbeddedResource)
        assert message["content"].type == "resource"
        assert message["content"].resource.mimeType == "text/plain"
        assert message["content"].resource.text == "Hello, world!"

        # Test with binary file
        message = MCPFile(binary_path, role="assistant")

        assert message["role"] == "assistant"
        assert isinstance(message["content"], EmbeddedResource)
        assert message["content"].type == "resource"
        assert message["content"].resource.mimeType == "application/pdf"

        # Decode the base64 data
        decoded = base64.b64decode(message["content"].resource.blob)
        assert decoded == b"%PDF-1.0 fake pdf data"

    finally:
        # Clean up
        os.unlink(text_path)
        os.unlink(binary_path)


def test_prompt_function():
    """Test the MCPPrompt function."""
    # Create a temporary file
    with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as f:
        f.write(b"Hello, world!")
        temp_path = f.name

    try:
        # Test with mixed content
        messages = MCPPrompt(
            "Hello",
            Path(temp_path),
            {"role": "assistant", "content": TextContent(type="text", text="Hi there")},
        )

        assert len(messages) == 3

        # Check first message
        assert messages[0]["role"] == "user"
        assert isinstance(messages[0]["content"], TextContent)
        assert messages[0]["content"].text == "Hello"

        # Check second message
        assert messages[1]["role"] == "user"
        assert isinstance(messages[1]["content"], EmbeddedResource)
        assert messages[1]["content"].resource.text == "Hello, world!"

        # Check third message
        assert messages[2]["role"] == "assistant"
        assert isinstance(messages[2]["content"], TextContent)
        assert messages[2]["content"].text == "Hi there"

        # Test with custom role
        messages = MCPPrompt("Hello", role="assistant")
        assert messages[0]["role"] == "assistant"

    finally:
        # Clean up
        os.unlink(temp_path)


def test_user_assistant_functions():
    """Test the User and Assistant helper functions."""
    # Test User function
    messages = User("Hello", "How are you?")

    assert len(messages) == 2
    assert messages[0]["role"] == "user"
    assert messages[1]["role"] == "user"
    assert messages[0]["content"].text == "Hello"
    assert messages[1]["content"].text == "How are you?"

    # Test Assistant function
    messages = Assistant("I'm fine, thanks!", "How can I help?")

    assert len(messages) == 2
    assert messages[0]["role"] == "assistant"
    assert messages[1]["role"] == "assistant"
    assert messages[0]["content"].text == "I'm fine, thanks!"
    assert messages[1]["content"].text == "How can I help?"
