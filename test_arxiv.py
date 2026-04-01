import asyncio
from arxiv_scraper import scraper_instance
import os
import json

async def main():
    print("=== Testing ArXiv Scraper (Fast API Mode) ===")
    
    # 1. Search
    print("\n1. Searching for 'Large Language Models' in Computer Science (cat:cs)...")
    search_res = await scraper_instance.search_papers(
        query="Large Language Models",
        search_field="all",
        source_type="cs", 
        start_index=0,
        limit=2
    )
    
    print(f"Total Results reported: {search_res.get('total_results')}")
    for p in search_res.get('papers', []):
        print(f" - [{p['id']}] {p['title']} [{p['source']}, {p['date']}]\n   Link: {p['detail_link']}")
        
    if not search_res.get('papers'):
        print("No papers found. Exiting.")
        return

    # 2. Get Details
    target_link = search_res['papers'][0]['detail_link']
    print(f"\n2. Getting details for {target_link}...")
    details = await scraper_instance.get_paper_details(target_link)
    abstract = details.get('abstract', '')
    print("Abstract:", abstract[:150], "..." if len(abstract)>150 else "")
    print("Keywords/Categories:", details.get('keywords', []))
    print("ArXiv ID:", details.get('doi', ''))

    # 3. Download & Convert
    output_dir = os.path.join(os.getcwd(), "arxiv_test_md_output")
    print(f"\n3. Downloading PDF from ArXiv & Converting MD to {output_dir}")
    md_res = await scraper_instance.read_paper_content(target_link, output_dir)
    print("\nResult preview:")
    print(md_res[:500])

if __name__ == "__main__":
    asyncio.run(main())
