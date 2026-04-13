import asyncio
from ieee_scraper import scraper_instance

async def main():
    q = "reinforcement learning residual control quadrotor agile"
    print(f"Testing IEEE with query: {q}")
    res = await scraper_instance.search_papers(q, limit=2)
    print(f"Total: {res.get('total_results')}")
    print(f"Papers found: {len(res.get('papers', []))}")
    if not res.get('papers'):
        print("No papers. Checking page title to see if blocked or really 0 results.")
        print(await scraper_instance.page.title())

if __name__ == "__main__":
    asyncio.run(main())
