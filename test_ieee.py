import asyncio
from ieee_scraper import scraper_instance
import os
import json

async def main():
    print("=== Testing IEEE Scraper ===")
    
    # 1. Search
    print("\n1. Searching for 'Large Language Models' in Conferences...")
    # source_type "Conferences" should trigger checking the Conferences checkbox
    search_res = await scraper_instance.search_papers(
        query="Large Language Models",
        search_field="All",
        source_type="Conferences", 
        start_index=0,
        limit=2
    )
    
    print(f"Total Results reported: {search_res['total_results']}")
    for p in search_res['papers']:
        print(f" - {p['title']} [{p['source']}, {p['date']}]")
        
    if not search_res['papers']:
        print("No papers found. Exiting.")
        await scraper_instance.close()
        return

    # 2. Get Details
    target_link = search_res['papers'][0]['detail_link']
    print(f"\n2. Getting details for {target_link}...")
    details = await scraper_instance.get_paper_details(target_link)
    print("Abstract:", details.get('abstract', '')[:150], "...")
    print("Keywords:", details.get('keywords', []))
    print("DOI:", details.get('doi', ''))

    # 3. Download & Convert
    output_dir = os.path.join(os.getcwd(), "ieee_test_md_output")
    print(f"\n3. Downloading & Converting MD to {output_dir}")
    md_res = await scraper_instance.read_paper_content(target_link, output_dir)
    print("\nResult preview:")
    print(md_res[:500])

    await scraper_instance.close()

if __name__ == "__main__":
    asyncio.run(main())
