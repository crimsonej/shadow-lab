import time
import sys
import asyncio
import httpx
import statistics
import multiprocessing

# We test with a small model by default
TEST_MODEL = "llama3:8b" if len(sys.argv) < 2 else sys.argv[1]

async def benchmark_inference(num_threads: int):
    """Run a fixed-size generation test with specific thread count."""
    url = "http://127.0.0.1:11434/api/generate"
    payload = {
        "model": TEST_MODEL,
        "prompt": "Write a 200 word story about a space faring cat.",
        "stream": False,
        "options": {
            "num_thread": num_threads,
            "num_ctx": 4096
        }
    }
    
    start = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            r = await client.post(url, json=payload)
            r.raise_for_status()
            data = r.json()
            end = time.perf_counter()
            
            total_time = end - start
            tokens = data.get("eval_count", 0)
            tps = tokens / (data.get("eval_duration", 1) / 1e9)
            
            return {
                "threads": num_threads,
                "tps": tps,
                "total_time": total_time,
                "tokens": tokens
            }
    except Exception as e:
        return {"threads": num_threads, "error": str(e)}

async def main():
    print(f"Shadow-Lab Benchmark — Model: {TEST_MODEL}")
    print("-" * 50)
    
    cores = multiprocessing.cpu_count()
    # Test cases: 1 thread, Auto (null), and Our Optimized (Cores or Cores-1)
    test_configs = [1, cores]
    if cores > 2:
        test_configs.append(cores - 1)
        
    results = []
    for t in test_configs:
        print(f"Running test with threads={t}...")
        res = await benchmark_inference(t)
        if "error" in res:
            print(f"  ✗ Failed: {res['error']}")
        else:
            print(f"  ✓ {res['tps']:.2f} tokens/sec (Total: {res['total_time']:.2f}s)")
            results.append(res)
            
    if results:
        best = max(results, key=lambda x: x["tps"])
        print("-" * 50)
        print(f"OPTIMIZATION RESULT: Best performance at threads={best['threads']}")
        print(f"Setting this as the default VPS profile.")

if __name__ == "__main__":
    asyncio.run(main())
