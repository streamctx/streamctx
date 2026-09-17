from streamctx.compressor import compress_messages, get_compression_stats
import tests.test_layer1_hardening as t

for name, builder in [
    ("chatter", t._chatter_session),
    ("tool_heavy", t._tool_heavy_session),
    ("buried_constraint", t._constraint_session),
]:
    msgs = builder()
    compressed, orig, after = compress_messages(msgs, max_tokens=800, keep_last_n=4)
    stats = get_compression_stats(orig, after)
    blob = " ".join(str(m.get("content", "")) for m in compressed)
    print(
        f"{name}: orig={stats['original_tokens']} after={stats['compressed_tokens']} "
        f"saved={stats['saved_tokens']} pct={stats['compression_pct']} "
        f"msgs {len(msgs)}->{len(compressed)} ACME={'ACME-9917' in blob}"
    )
