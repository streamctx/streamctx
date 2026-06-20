"""Pytest suite to verify Context Compression works."""

import streamctx


messages = [
    {"role": "system", "content": "You are a helpful AI assistant for coding tasks."},
    {"role": "user", "content": "What is Python? Tell me everything about it in detail."},
    {
        "role": "assistant",
        "content": (
            "Python is a high-level, interpreted programming language known for "
            "its simplicity and readability. It was created by Guido van Rossum."
        ),
    },
    {"role": "user", "content": "What are Python libraries? Give me a comprehensive list."},
    {
        "role": "assistant",
        "content": (
            "Python has thousands of libraries. NumPy, Pandas, Matplotlib, "
            "Scikit-learn, TensorFlow, PyTorch, Django, Flask, Requests and many more."
        ),
    },
    {"role": "user", "content": "Now tell me about neural networks."},
    {
        "role": "assistant",
        "content": "Neural networks are computational models inspired by biological neural networks.",
    },
]


def test_compress_returns_expected_keys():
    """Compression result should include messages and a stats block."""
    result = streamctx.compress(messages, max_tokens=100, keep_last_n=2)

    assert "messages" in result
    assert "stats" in result

    stats = result["stats"]
    assert "original_tokens" in stats
    assert "compressed_tokens" in stats
    assert "saved_tokens" in stats
    assert "compression_pct" in stats


def test_compress_reduces_token_count():
    """Compressed token count should be less than or equal to the original."""
    result = streamctx.compress(messages, max_tokens=100, keep_last_n=2)
    stats = result["stats"]

    assert stats["compressed_tokens"] <= stats["original_tokens"]
    assert stats["saved_tokens"] >= 0


def test_compress_keeps_last_n_messages():
    """Compression should not drop more messages than keep_last_n allows from the end."""
    result = streamctx.compress(messages, max_tokens=100, keep_last_n=2)

    assert len(result["messages"]) >= 2
    assert len(result["messages"]) <= len(messages)
