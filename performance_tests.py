import asyncio
import time
import logging
import random
import os
import sys

from dotenv import load_dotenv
load_dotenv()

# Add app to path
sys.path.append(os.path.join(os.path.dirname(__file__), 'app'))

from app.airtable_client import init_cache, init_cache_async, fuzzy_match_project, CACHE_PROJECTS
from app.database import init_db, close_db, save_extraction, pool

logging.basicConfig(level=logging.INFO, format='%(levelname)s - %(message)s')
logger = logging.getLogger("Benchmark")

async def test_1_cache_initialization():
    logger.info("=== Test 1: Cache Initialization (Async vs Sync) ===")
    
    start = time.perf_counter()
    init_cache()  # Sync
    sync_time = time.perf_counter() - start
    logger.info(f"Synchronous Cache Load: {sync_time:.4f} seconds")
    
    # reset cache
    import app.airtable_client as ac
    ac.CACHE_INITIALIZED = False
    
    start = time.perf_counter()
    await init_cache_async()
    async_time = time.perf_counter() - start
    logger.info(f"Asynchronous Cache Load: {async_time:.4f} seconds")

async def test_2_fuzzy_matching():
    logger.info("=== Test 2: Fuzzy Matching Benchmark ===")
    if not CACHE_PROJECTS:
        await init_cache_async()
        
    start = time.perf_counter()
    matches = 0
    # Run 100 queries against the loaded cache
    for _ in range(100):
        # Generate some random string or pick a known one with a typo
        name = "Kuta Resort " + str(random.randint(1, 100))
        match, score = fuzzy_match_project(name, CACHE_PROJECTS)
        if match:
            matches += 1
            
    elapsed = time.perf_counter() - start
    logger.info(f"100 Fuzzy Matches took {elapsed:.4f} seconds. ({(elapsed/100)*1000:.2f} ms per query)")

async def test_3_database_concurrency():
    logger.info("=== Test 3: Database Concurrency Benchmark ===")
    await init_db()
    
    from app.database import pool
    if not pool:
        logger.warning("No DB connection available for Test 3. Skipping.")
        return
        
    start = time.perf_counter()
    
    # Save 100 extractions concurrently
    tasks = []
    for i in range(100):
        tasks.append(
            save_extraction(
                message_id=9999000+i, chat_id=-1001, project_recid="TestProj", 
                object_guess="Villa", confidence=0.99, slot="test", url_status="none", 
                why="Benchmark", needs_human=False, raw_json={"test": True}
            )
        )
    
    await asyncio.gather(*tasks)
    elapsed = time.perf_counter() - start
    logger.info(f"100 Concurrent DB Inserts took {elapsed:.4f} seconds.")
    
    # Cleanup dummy records
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM extractions WHERE chat_id = -1001")
    
    await close_db()

async def test_4_event_loop_blocking():
    logger.info("=== Test 4: Thread-Blocking Benchmark ===")
    
    import app.airtable_client as ac
    ac.CACHE_INITIALIZED = False
    
    # We will run a counter task that increments every 10ms. 
    # If the event loop is blocked, the counter will jump/stall.
    counter = 0
    running = True
    
    async def ticker():
        nonlocal counter
        while running:
            await asyncio.sleep(0.01)
            counter += 1
            
    ticker_task = asyncio.create_task(ticker())
    
    start = time.perf_counter()
    # This should NOT block the ticker!
    await init_cache_async()
    elapsed = time.perf_counter() - start
    
    running = False
    await ticker_task
    
    expected_ticks = int(elapsed / 0.01)
    logger.info(f"Async Cache Load took {elapsed:.4f}s. Expected ticks: ~{expected_ticks}, Actual ticks: {counter}")
    if counter < expected_ticks * 0.1:
        logger.error("Event loop was SEVERELY blocked! asyncio.to_thread didn't work.")
    else:
        logger.info("Event loop remained responsive during I/O.")

async def test_5_e2e_pipeline():
    logger.info("=== Test 5: End-to-End Latency Profile ===")
    start = time.perf_counter()
    
    import app.airtable_client as ac
    ac.CACHE_INITIALIZED = False
    await init_cache_async()
    
    # Mock upsert_project
    proj_data = {
        "Project Name": "Benchmark Villa",
        "Price From (USD)": 150000,
        "Total Units": 10,
        "District": "Ubud"
    }
    
    try:
        from app.airtable_client import upsert_project
        proj_id = await upsert_project(proj_data, dev_id=None, gaps=[])
        elapsed = time.perf_counter() - start
        logger.info(f"End-to-End Upsert took {elapsed:.4f} seconds. (Returned ID: {proj_id})")
    except Exception as e:
        logger.error(f"E2E test failed: {e}")

async def run_all():
    logger.info("Starting Benchmark Suite...")
    await test_1_cache_initialization()
    print("-" * 50)
    await test_2_fuzzy_matching()
    print("-" * 50)
    await test_3_database_concurrency()
    print("-" * 50)
    await test_4_event_loop_blocking()
    print("-" * 50)
    await test_5_e2e_pipeline()
    logger.info("Benchmarks completed.")

if __name__ == "__main__":
    asyncio.run(run_all())
