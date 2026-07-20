import concurrent.futures
import time
from streamctx.tracker import get_tracker

def worker_task(worker_id):
    tracker = get_tracker(agent_id=f"dogfood_worker_{worker_id}")
    tracker.start()
    tracker.checkpoint()
    tracker.stop()
    return worker_id

start_time = time.time()
errors = []

with concurrent.futures.ThreadPoolExecutor(max_workers=50) as executor:
    futures = [executor.submit(worker_task, i) for i in range(50)]
    for future in concurrent.futures.as_completed(futures):
        try:
            future.result()
        except Exception as e:
            errors.append(str(e))

elapsed = time.time() - start_time

print(f"Total time: {elapsed:.2f} seconds")
print(f"Errors: {len(errors)}")
if errors:
    print("First error:", errors[0])
else:
    print("Zero errors — concurrency test passed!")
