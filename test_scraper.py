import asyncio
import os
from cnki_scraper import scraper_instance

async def main():
    print("Testing Phase 2 Features...")
    
    # 1. Test slicing/pagination: Fetch items 25 to 29 (5 items) from "外文" db_scope or "总库"
    print("\n--- Testing Search (start_index=25, limit=3, source_type='学术期刊') ---")
    results = await scraper_instance.search_papers(query="大型语言模型", db_scope="总库", source_type="学术期刊", start_index=25, limit=3)
    
    for i, r in enumerate(results):
        print(f"[{i+1}] Title: {r['title']} | Date: {r['date']} | Source: {r['source']}")
        print(f"ID: {r['id']} | Link: {r['detail_link']}")
        print("-" * 40)
    
    if len(results) > 0:
        first_paper = results[0]
        output_path = os.path.join(os.getcwd(), "test_outputs")
        
        print(f"\n--- Testing Read Paper Content (ID: {first_paper['id']}) ---")
        print(f"Output Dir: {output_path}")
        
        md_text = await scraper_instance.read_paper_content(first_paper['detail_link'], output_path)
        print(md_text)
        
    await scraper_instance.close()

if __name__ == "__main__":
    asyncio.run(main())
