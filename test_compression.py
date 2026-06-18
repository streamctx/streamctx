import streamctx

messages = [
    {"role": "system", "content": "You are a helpful AI assistant for coding tasks."},
    {"role": "user", "content": "What is Python? Tell me everything about it in detail."},
    {"role": "assistant", "content": "Python is a high-level, interpreted programming language known for its simplicity and readability. It was created by Guido van Rossum and first released in 1991."},
    {"role": "user", "content": "What are Python libraries? Give me a comprehensive list."},
    {"role": "assistant", "content": "Python has thousands of libraries. NumPy, Pandas, Matplotlib, Scikit-learn, TensorFlow, PyTorch, Django, Flask, Requests and many more."},
    {"role": "user", "content": "Now tell me about neural networks."},
    {"role": "assistant", "content": "Neural networks are computational models inspired by biological neural networks."},
]

result = streamctx.compress(messages, max_tokens=100, keep_last_n=2)
stats = result["stats"]

print("=" * 50)
print("Compression Test")
print("=" * 50)
print(f"Original:   {stats['original_tokens']} tokens")
print(f"Compressed: {stats['compressed_tokens']} tokens")
print(f"Saved:      {stats['saved_tokens']} tokens")
print(f"Reduction:  {stats['compression_pct']}%")
print(f"Messages:   {len(messages)} -> {len(result['messages'])}")
print("=" * 50)


