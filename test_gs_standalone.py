import asyncio
from gs_scraper import scraper_instance as gs

async def main():
    q = "reinforcement learning precision agriculture drone"
    print(f"Testing GS with query: {q}")
    res = await gs.search_papers(q, limit=5)
    print(f"Total GS matches: {res.get('total_results')}")
    papers = res.get('papers', [])
    for p in papers:
        print(f" - {p.get('title')} ({p.get('date')}) -> {p.get('source')}")
    await gs.close()

if __name__ == "__main__":
    asyncio.run(main())
