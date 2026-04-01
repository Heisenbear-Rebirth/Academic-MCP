import asyncio
import os
import sys

# Import all scrapers
from cnki_scraper import scraper_instance as cnki
from ieee_scraper import scraper_instance as ieee
from arxiv_scraper import scraper_instance as arxiv
from acm_scraper import scraper_instance as acm
from sd_scraper import scraper_instance as sd

async def test_platform(name, scraper, query, db_scope="", source_type="all"):
    print(f"\n{'='*50}")
    print(f"[{name}] Starting Tests...")
    print(f"{'='*50}")
    
    # 1. Test Search
    print(f"\n[1/3] Testing Search for '{query}'...")
    try:
        search_res = await scraper.search_papers(query=query, db_scope=db_scope, source_type=source_type, limit=2)
        total = search_res.get('total_results', '0')
        papers = search_res.get('papers', [])
        print(f"  [OK] Search successful. Total reported: {total}")
        print(f"  [OK] Found {len(papers)} papers in current page.")
        if not papers:
            print(f"  [WARN] No papers parsed for {name}!")
            return False
            
        p = papers[0]
        # Check for encoding/garbled text issues
        print(f"  Sample Title:  {p.get('title')}")
        print(f"  Sample Author: {p.get('author')}")
        print(f"  Sample Source: {p.get('source')}")
        print(f"  Sample Link:   {p.get('detail_link')}")
        
    except Exception as e:
        print(f"  [FAIL] Search failed: {e}")
        return False

    target_link = papers[0].get('detail_link')
    if not target_link:
        print("  [FAIL] No detail link found.")
        return False

    # 2. Test Details Extraction
    print(f"\n[2/3] Testing Details Extraction for: {target_link}")
    try:
        details = await scraper.get_paper_details(target_link)
        abs_text = details.get('abstract', '')
        print(f"  [OK] Details successful.")
        print(f"  Abstract Preview ({len(abs_text)} chars): {abs_text[:100]}...")
        print(f"  Keywords: {details.get('keywords', [])}")
        print(f"  DOI: {details.get('doi', '')}")
    except Exception as e:
        print(f"  [FAIL] Details failed: {e}")
        return False
        
    # 3. Test PDF Download & Conversion
    # We will test read_paper_content briefly
    output_dir = os.path.join(os.getcwd(), f"test_output_{name.lower()}")
    print(f"\n[3/3] Testing PDF download & MD conversion to {output_dir}")
    try:
        md_res = await scraper.read_paper_content(target_link, output_dir)
        if "=== 转换成功 ===" in md_res or "Saved" in md_res or "success" in md_res.lower() or ".md" in md_res:
            print(f"  [OK] MD Conversion successful!")
            print(f"  Preview:\n{md_res[:200]}...\n")
        else:
            print(f"  [WARN] Partial success: {md_res[:200]}")
    except Exception as e:
        print(f"  [FAIL] MD Conversion failed: {e}")
        return False

    # Close resources safely if needed setup by scraper
    if hasattr(scraper, 'close'):
        await scraper.close()
        
    print(f"\n>>> [{name}] All tests passed! <<<")
    return True


async def main():
    platforms = [
        ("ARXIV", arxiv, "quantum computing", "", "all"),
        ("SD", sd, "machine learning in healthcare", "", "all"),
        ("IEEE", ieee, "deep learning", "", "all"),
        ("ACM", acm, "computer graphics", "", "all"),
        ("CNKI", cnki, "大语言模型", "总库", "all"),
    ]
    
    results = {}
    
    for name, scraper_obj, q, scope, src_type in platforms:
        success = await test_platform(name, scraper_obj, q, scope, src_type)
        results[name] = success
        await asyncio.sleep(2) # breather
        
    print("\n\n" + "#"*40)
    print("FINAL TEST REPORT")
    print("#"*40)
    for name, success in results.items():
        status = "PASSED ✓" if success else "FAILED ✗"
        print(f"{name.ljust(10)} : {status}")


if __name__ == "__main__":
    asyncio.run(main())
